import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# MSK v3 ORCHESTRATOR (single run):
#
#   1. inland scrape   (origins × inland yards)        -> exact inland canonicals
#   2. derive ocean    (truncate at discharge port)    -> port-to-port canonicals
#   3. missing-ports   (port universe − observed PODs) -> per-origin pure-ocean gaps
#   4. secondary scrape(origins × missing ports)       -> the pure-ocean ports
#   5. push everything to Supabase, at the very end
#
# Maersk = a clean JSON POST API (api.maersk.com/routing-unified) keyed by GEO_ID,
# consumer-key header, no browser/session bootstrap. coverage.json is READ-ONLY
# (only type=="port" / type=="inland" keys are read). City codes come from
# msk_citiesv3.json (v1's msk_cities.json with the one nested "Kansas City" key
# exploded into single-city keys). v1 (main.py + msk_cities.json) is untouched.
#
# LA/Long Beach & Seattle/Tacoma: only Los Angeles and Tacoma are in the yard set
# (coverage). utils.normalize_pod keeps Los Angeles/Long Beach and Seattle/Tacoma
# distinct, so any Long Beach/Seattle voyages arrive as distinct derive-only
# discharge ports and are never queried separately.

import json
import time
import random
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    build_canonical_record,
    get_unresolved,
)
from derive_ocean import derive_ocean


def safe_to_csv(df, path, retries=5, backoff=1.0, **kwargs):
    """to_csv that retries on PermissionError (OneDrive sync / Excel locks)."""
    for attempt in range(retries):
        try:
            df.to_csv(path, **kwargs)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            wait = backoff * (attempt + 1)
            print(f"🔒 CSV locked ({path}), retrying in {wait}s...")
            time.sleep(wait)


# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

TEMP_DIR = ASSETS_DIR / "temp_v3"
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"
OCEAN_DIR = TEMP_DIR / "ocean"
RAW_SECONDARY_DIR = TEMP_DIR / "raw_secondary"
SECONDARY_DIR = TEMP_DIR / "secondary"
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, OCEAN_DIR, RAW_SECONDARY_DIR, SECONDARY_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"MSKv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"MSK_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"
cities_file = ASSETS_DIR / "msk_citiesv3.json"

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
inland_dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
port_universe = {name for name, meta in coverage.items() if meta.get("type") == "port"}

with open(cities_file, "r", encoding="utf-8") as f:
    msk_cities = json.load(f)


def get_locations(city_name):
    """Return [{"name", "code"=maerskGeoLocationId}, ...] for a city; [] if unknown.

    msk_citiesv3.json is flat (one city per key); the list branch is kept only for
    safety in case a grouped v1-style entry ever sneaks back in.
    """
    entry = msk_cities.get(city_name)
    if not entry:
        return []
    entries = entry if isinstance(entry, list) else [entry]
    return [{"name": e.get("localityName") or city_name, "code": e.get("maerskGeoLocationId")}
            for e in entries if e.get("maerskGeoLocationId")]


# --- Dynamic dates / API ---
EARLIEST = today_iso
LATEST = (today + timedelta(days=29)).strftime("%Y-%m-%d")
DELAY_RANGE = (1, 3)
MAX_RETRIES = 1
URL = "https://api.maersk.com/routing-unified/routing/routings-queries"
HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "api-version": "1",
    "consumer-key": "uXe7bxTHLY0yY0e8jnS6kotShkLuAAqG",
}


def make_payload(pol_geo_id, pod_geo_id):
    return {
        "requestType": "DATED_SCHEDULES",
        "includeFutureSchedules": True,
        "routingCondition": "PREFERRED",
        "exportServiceType": "CY",
        "importServiceType": "CY",
        "brandCode": "MSL",
        "startLocation": {
            "dataObject": "CITY",
            "alternativeCodes": [
                {"alternativeCodeType": "GEO_ID", "alternativeCode": pol_geo_id}
            ],
            "cityCode": "",
        },
        "endLocation": {
            "dataObject": "CITY",
            "alternativeCodes": [
                {"alternativeCodeType": "GEO_ID", "alternativeCode": pod_geo_id}
            ],
            "cityCode": "",
        },
        "timeRange": {
            "routingsBasedOn": "DEPARTURE_DATE",
            "earliestTime": EARLIEST,
            "latestTime": LATEST,
        },
        "cargo": {"cargoType": "DRY", "isTemperatureControlRequired": False},
        "carriage": {"vessel": {"flagCountryCode": ""}},
        "equipment": {
            "equipmentSizeCode": "40",
            "equipmentTypeCode": "HDRY",
            "constructionMaterial": "",
            "isEmpty": False,
            "isShipperOwned": False,
        },
        "IsUseOfInternetMarkedRoutesOnly": False,
    }


# =========================================================================
# Location-code enrichment (mirrors v1 main.py)
# =========================================================================
# MSK returns some POD / transshipment ports as GEO_ID codes, not names. Resolve
# each code -> city name via Maersk's geography API, cached in city_codes.json
# (shared with v1), then rewrite the codes inside the raw responses BEFORE
# canonical building — so port_of_discharge / ts_ports come out as names.
CITY_CODES_FILE = CARRIER_DIR / "city_codes.json"
LOC_BASE_URL = "https://api.maersk.com/synergy/reference-data/geography/locations/"
LOC_DELAY = 0.5
LOC_HEADERS = {
    "Consumer-Key": HEADERS["consumer-key"],
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Referer": "https://www.maersk.com/",
    "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _atomic_write_json(data, path):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def _collect_codes(obj, found):
    """Recursively gather every 'alternativeCode' value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "alternativeCode":
                found.add(v)
            else:
                _collect_codes(v, found)
    elif isinstance(obj, list):
        for it in obj:
            _collect_codes(it, found)


def _apply_codes(obj, mapping):
    """Rewrite 'alternativeCode' values in place using mapping (mirrors v1)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "alternativeCode" and v in mapping:
                obj[k] = mapping[v]
            else:
                _apply_codes(v, mapping)
    elif isinstance(obj, list):
        for it in obj:
            _apply_codes(it, mapping)


def enrich_location_codes(raw_dir, label):
    """Resolve GEO_ID codes -> city names (cached in city_codes.json) and rewrite
    them inside every MSK_*.json in raw_dir, in place, before canonical building."""
    files = [raw_dir / f for f in os.listdir(raw_dir)
             if f.startswith("MSK_") and f.endswith(".json")]
    if not files:
        return

    cache = {}
    if CITY_CODES_FILE.exists():
        with CITY_CODES_FILE.open("r", encoding="utf-8") as f:
            cache = json.load(f)

    # 1. collect every code seen in the raw responses
    codes, docs = set(), {}
    for fp in files:
        with fp.open("r", encoding="utf-8") as f:
            docs[fp] = json.load(f)
        _collect_codes(docs[fp], codes)

    # 2. resolve codes not yet resolved (absent or cached as None — v1 retries None)
    pending = [c for c in codes if cache.get(c) is None]
    print(f"🗺️ [{label}] {len(codes)} codes seen, {len(pending)} to resolve via geography API")
    for code in pending:
        try:
            r = requests.get(LOC_BASE_URL + str(code), headers=LOC_HEADERS, timeout=10)
            cache[code] = r.json().get("cityName") if r.status_code == 200 else None
        except Exception as e:
            cache[code] = None
            print(f"  ❌ {code}: {e}")
        _atomic_write_json(cache, CITY_CODES_FILE)
        time.sleep(LOC_DELAY)

    # 3. rewrite codes -> names inside each raw file
    for fp, doc in docs.items():
        _apply_codes(doc, cache)
        with fp.open("w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"✅ [{label}] enriched {len(files)} raw file(s)")


# =========================================================================
# Reusable scrape + canonical build
# =========================================================================
def scrape_matrix(quotes, raw_dir, label):
    """POST the MSK routings API per (origin, dest) row; save wrapped JSON to raw_dir."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue
        pol_name, pod_name = row["Port of Loading"], row["LastCY"]
        pol_locations = get_locations(pol_name)
        pod_locations = get_locations(pod_name)
        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ [{label}] Missing codes for {pol_name} or {pod_name}")
            continue

        success = False
        for pol in pol_locations:
            pol_code = pol["code"]
            for pod in pod_locations:
                pod_code = pod["code"]
                payload = make_payload(pol_code, pod_code)
                retried = False
                while True:
                    try:
                        resp = requests.post(URL, headers=HEADERS, json=payload, timeout=40)
                        print(f"📡 [{label}] {pol_name}({pol_code}) → {pod_name}({pod_code}): {resp.status_code}")
                        if resp.status_code != 200:
                            if not retried and MAX_RETRIES > 0:
                                retried = True
                                time.sleep(random.uniform(5, 10))
                                continue
                            break
                        try:
                            data = resp.json()
                        except json.JSONDecodeError:
                            print(f"⚠️ [{label}] Bad JSON for {pol_code} → {pod_code}")
                            break
                        routings = data.get("routings", [])
                        if not routings:
                            print(f"⚪ [{label}] No routings for {pol_code} → {pod_code}")
                        else:
                            success = True
                            wrapped = {
                                "query_date": query_timestamp,
                                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                                "PortOfLoading": pol_name,
                                "LastCY": pod_name,
                                "OFQ": row.get("ID"),
                                "FinalDestination": row.get("Final Destination"),
                                "routings": routings,
                            }
                            pol_short = pol_name.replace(" ", "")[:5]
                            pod_short = pod_name.replace(" ", "")[:5]
                            out = get_unique_path(
                                raw_dir / f"MSK_{pol_short}_{pod_short}_{filename_timestamp}.json")
                            with open(out, "w", encoding="utf-8") as f:
                                json.dump(wrapped, f, ensure_ascii=False, indent=2)
                            print(f"✅ [{label}] Saved {len(routings)} routings → {out}")
                        break
                    except Exception as e:
                        print(f"💥 [{label}] Exception {pol_code} → {pod_code}: {e}")
                        break
                time.sleep(random.uniform(*DELAY_RANGE))
        quotes.at[idx, "status"] = "done" if success else "no_records"


def _drop_conflated(rec):
    """Secondary-pass guard: drop DIRECT schedules (eta==pod_eta) that discharge at a
    port other than the queried one — the carrier resolved the query to a different
    port's voyages, already covered. Keeps pure-ocean ports (discharge==queried) and
    rail-served ports (eta!=pod_eta). Returns (kept_schedules, n_dropped)."""
    lc = rec.get("last_cy")
    scheds = rec.get("schedules", [])
    kept = [s for s in scheds
            if s.get("port_of_discharge") == lc or s.get("eta") != s.get("pod_eta")]
    return kept, len(scheds) - len(kept)


def build_canonicals(raw_dir, canonical_dir, drop_conflated=False):
    """build_canonical_record for every raw MSK_*.json -> canonical_dir.
    drop_conflated=True (secondary pass) applies the conflated-port guard."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    written, dropped = [], 0
    for file in os.listdir(raw_dir):
        if file.startswith("MSK_") and file.endswith(".json"):
            rec = build_canonical_record(os.path.join(raw_dir, file))
            if rec is None:
                continue
            if drop_conflated:
                kept, n = _drop_conflated(rec)
                dropped += n
                if not kept:
                    continue
                rec["schedules"] = kept
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(canonical_dir / f"MSK_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
            written.append(out)
    msg = f"✅ Wrote {len(written)} canonical(s) → {canonical_dir}"
    if drop_conflated:
        msg += f"  (dropped {dropped} conflated direct schedules)"
    print(msg)
    return written


def build_quotes(rows, prefix):
    out = [{"ID": f"{prefix}-{i+1:04d}", "Port of Loading": o, "LastCY": d,
            "status": "pending", "result_file": None}
           for i, (o, d) in enumerate(rows)]
    df = pd.DataFrame(out)
    if not df.empty:
        df["result_file"] = df["result_file"].astype("string")
    return df


# =========================================================================
# ORCHESTRATION
# =========================================================================
inland_quotes = build_quotes([(o, d) for o in origins for d in inland_dests], "V3")
secondary_quotes = None

try:
    # --- 1. inland scrape ---
    print(f"\n=== INLAND: {len(origins)} origins × {len(inland_dests)} yards = {len(inland_quotes)} pairs ===")
    scrape_matrix(inland_quotes, RAW_DIR, "inland")
    enrich_location_codes(RAW_DIR, "inland")
    build_canonicals(RAW_DIR, CANONICAL_DIR)

    # --- 2. derive ocean (no push) ---
    print("\n=== DERIVE OCEAN ===")
    derive_ocean(CANONICAL_DIR, OCEAN_DIR, ports=port_universe)

    # --- 3. missing-ports diff (per origin), read-only on coverage ---
    observed = defaultdict(set)
    for fp in CANONICAL_DIR.glob("MSK_*.json"):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        o = rec.get("port_of_loading")
        for s in rec.get("schedules", []):
            pod = s.get("port_of_discharge")
            if pod:
                observed[o].add(pod)

    sec_rows, unresolved_missing = [], set()
    print("\n=== MISSING PORTS (port universe − observed PODs) ===")
    for o in origins:
        missing = sorted(port_universe - observed.get(o, set()))
        resolvable = [p for p in missing if get_locations(p)]
        unresolved_missing |= (set(missing) - set(resolvable))
        print(f"  {o}: {len(missing)} missing → {len(resolvable)} queryable: {resolvable}")
        sec_rows += [(o, p) for p in resolvable]
    if unresolved_missing:
        print(f"⚠️ missing ports without MSK codes: {sorted(unresolved_missing)}")

    # --- 4. secondary scrape ---
    secondary_quotes = build_quotes(sec_rows, "V3S")
    print(f"\n=== SECONDARY: {len(secondary_quotes)} (origin, pure-ocean port) pairs ===")
    if not secondary_quotes.empty:
        scrape_matrix(secondary_quotes, RAW_SECONDARY_DIR, "secondary")
        enrich_location_codes(RAW_SECONDARY_DIR, "secondary")
        build_canonicals(RAW_SECONDARY_DIR, SECONDARY_DIR, drop_conflated=True)
    else:
        print("  (no missing ports — nothing to scrape)")

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"MSKv3_unresolved_ports_{today_str}.csv")
        safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
        print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(inland_quotes, crash_file, index=False)
    if secondary_quotes is not None:
        safe_to_csv(secondary_quotes, get_unique_filename(crash_file.with_stem(crash_file.stem + "_secondary")), index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}  (no push)")
    raise


# --- 5. push everything, at the very end (clean finish only) ---
print("\n=== PUSH (end of run) ===")
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

for cdir, ledger in [
    (CANONICAL_DIR, "ingest_ledger_inland.json"),
    (OCEAN_DIR, "ingest_ledger_ocean.json"),
    (SECONDARY_DIR, "ingest_ledger_secondary.json"),
]:
    try:
        ingest_new_canonicals("MSK", canonical_dir=cdir, ledger_path=TEMP_DIR / ledger)
    except Exception as e:
        print(f"⚠️ Supabase push failed for {cdir.name} (non-fatal): {e}")

print("✅ v3 run complete.")
