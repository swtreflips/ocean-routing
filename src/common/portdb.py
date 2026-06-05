"""
Shared port-name resolver backed by ``portdbCanonical.json`` (repo root).

Carriers emit transshipment / route ports as raw, inconsistent strings
(``"Ramp bangkok, TH"``, ``"busan, korea, KR"``, ``"kaohsiung, TAIWAN , CHINA"``).
``normalize_port`` rewrites these to the single canonical name used as the key in
``portdbCanonical.json`` (e.g. ``"Bangkok, Thailand"``, ``"Busan, Republic Of Korea"``,
``"Long Beach, CA"``).

Resolution is principled, not a hardcoded table: match on the city *name*
(disambiguated by a country hint) against the port db. Genuine exceptions live in
``port_overrides.json``. Unresolved strings are kept verbatim and recorded for
later curation via :func:`get_unresolved`.
"""

import json
import re
from collections import defaultdict
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parents[2] / "portdbCanonical.json"
_OVERRIDES_PATH = Path(__file__).resolve().parent / "port_overrides.json"

_RAMP_RE = re.compile(r"^(?:ramp|rail)\s+", re.IGNORECASE)
_PAREN_RE = re.compile(r"\([^)]*\)")                  # terminal qualifiers, e.g. (NORTH HARBOUR)
_GENERIC_SUFFIXES = (" PORT", " TERMINAL", " PT")     # tried only as a relaxation fallback

# Lazy-built singletons (populated by _load()).
_by_name_cc = None   # (NAME, CC)      -> [canonical_key, ...]
_by_name = None      # NAME / ALT NAME -> [canonical_key, ...]
_country_word = None # COUNTRY NAME    -> {CC, ...}
_key_cc = None       # canonical_key   -> CC
_overrides = None    # lowercased raw  -> canonical_key

_unresolved = set()  # raw strings normalize_port could not resolve


def _load():
    """Build the lookup indexes from portdbCanonical.json once."""
    global _by_name_cc, _by_name, _country_word, _key_cc, _overrides
    if _by_name_cc is not None:
        return

    db = json.loads(_DB_PATH.read_text(encoding="utf-8"))

    _by_name_cc = defaultdict(list)
    _by_name = defaultdict(list)
    _country_word = defaultdict(set)
    _key_cc = {}

    for key, v in db.items():
        name = (v.get("name") or "").strip().upper()
        country = v.get("country") or {}
        cc = (country.get("code") or "").strip().upper()
        _key_cc[key] = cc
        if name:
            _by_name_cc[(name, cc)].append(key)
            _by_name[name].append(key)
        for alt in v.get("alternativeNames") or []:
            alt = (alt or "").strip().upper()
            if alt:
                _by_name[alt].append(key)
        cname = (country.get("name") or "").strip().upper()
        if cname and cc:
            _country_word[cname].add(cc)

    _overrides = {}
    if _OVERRIDES_PATH.exists():
        raw = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
        _overrides = {k.strip().lower(): val for k, val in raw.items()}


def _city_forms(raw):
    """Raw string -> (ordered candidate city forms, country hints).

    Handles both shapes seen across carriers:
      - CMA:  'Ramp bangkok, TH'           -> (['BANGKOK'], ['TH'])
      - YML:  'MANILA (NORTH HARBOUR)'      -> (['MANILA'], [])
              'PIPAVAV (VICTOR) PORT'       -> (['PIPAVAV PORT', 'PIPAVAV'], [])

    The full form is always tried first; the suffix-stripped form is only a
    fallback, so a legitimate name ending in a generic word (e.g. 'BUSAN NEW
    PORT') still matches before it gets relaxed.
    """
    s = _RAMP_RE.sub("", raw.strip())
    s = _PAREN_RE.sub(" ", s)                          # drop (NORTH HARBOUR) etc.
    parts = [p.strip() for p in s.split(",") if p.strip()]
    city = " ".join(parts[0].split()).upper() if parts else ""
    hints = [p.upper() for p in parts[1:]]
    forms = [city] if city else []
    for suf in _GENERIC_SUFFIXES:
        if city.endswith(suf):
            base = city[: -len(suf)].strip()
            if base:
                forms.append(base)
    return forms, hints


def _country_codes(hints):
    """Turn country hints into a set of 2-letter codes.

    A 2-letter alpha token is taken as a code directly; any token matching a
    country name in the db contributes that country's code. This is why
    ``"kaohsiung, TAIWAN , CHINA"`` still resolves to Taiwan: the city-name match
    wins and the noisy country tokens never override it.
    """
    ccs = set()
    for h in hints:
        if len(h) == 2 and h.isalpha():
            ccs.add(h)
        if h in _country_word:
            ccs |= _country_word[h]
    return ccs


def resolve_port(raw):
    """Resolve a raw port string to a canonical portdb key.

    Returns ``(canonical_key | None, method)``. Pure: never mutates state.
    """
    _load()
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty"

    override = _overrides.get(raw.strip().lower())
    if override:
        return override, "override"

    forms, hints = _city_forms(raw)
    if not forms:
        return None, "unresolved"
    ccs = _country_codes(hints)

    for city in forms:
        # 1) exact (name, country code), unambiguous
        for cc in ccs:
            cands = _by_name_cc.get((city, cc))
            if cands and len(set(cands)) == 1:
                return cands[0], "name+cc"

        # 2) exact name, unique across the whole db
        cands = list(dict.fromkeys(_by_name.get(city, [])))
        if len(cands) == 1:
            return cands[0], "name-unique"

        # 3) exact name filtered to the country hint, unique
        if ccs:
            filtered = list(dict.fromkeys(k for k in cands if _key_cc.get(k) in ccs))
            if len(filtered) == 1:
                return filtered[0], "name+cc-filter"

    return None, "unresolved"


def normalize_port(raw):
    """Return the canonical port name, or ``raw`` unchanged if unresolved.

    Unresolved (non-empty, string) inputs are recorded for :func:`get_unresolved`.
    """
    key, _method = resolve_port(raw)
    if key:
        return key
    if isinstance(raw, str) and raw.strip():
        _unresolved.add(raw.strip())
    return raw


def normalize_ports(seq):
    """Map :func:`normalize_port` over a sequence, preserving order."""
    return [normalize_port(p) for p in (seq or [])]


def get_unresolved():
    """Sorted snapshot of raw strings that could not be resolved this run."""
    return sorted(_unresolved)
