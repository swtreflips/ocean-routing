"""
derive_ocean.py — ZIM v3: derive port-to-port (ocean-only) schedules from inland scrapes.

Each inland schedule already contains its ocean leg: arrival at the discharge
port is `pod_eta`. A port-to-port schedule is that inland schedule TRUNCATED at
the port (eta = pod_eta, rail/'Land Trans' dropped). No estimation. Deduped per
ocean sailing.

NOTE on Long Beach / Seattle / Tacoma: ZIM's yard set (coverage) has only Los
Angeles on the West Coast (no Long Beach, no PNW port). utils.normalize_pod keeps
those distinct, so any Long Beach / Seattle / Tacoma services that surface as a
`port_of_discharge` here derive into their own (distinct) canonicals — never
queried separately, i.e. derive-only (like HPL Long Beach).

Inputs : inland canonicals in assets/temp_v3/canonicals/ (last_cy == inland yard)
Output : port-to-port canonicals in assets/temp_v3/ocean/, one per (origin -> port).

Run:  python derive_ocean.py
"""

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

CARRIER_DIR = Path(__file__).resolve().parent
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp_v3" / "canonicals"
OCEAN_DIR = CARRIER_DIR / "assets" / "temp_v3" / "ocean"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"


def _hash16(*fields):
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    i = 1
    while True:
        cand = base.with_stem(f"{base.stem}_{i}")
        if not cand.exists():
            return cand
        i += 1


def _ocean_key(origin, s):
    """Identity of an ocean sailing — same across the inland yards that share it."""
    return (
        origin,
        s.get("port_of_discharge"),
        s.get("etd"),
        s.get("pod_eta"),
        s.get("mother_vessel"),
        tuple(s.get("vessel_sequence") or []),
    )


def derive_ocean_schedule(s):
    """Truncate a ZIM inland schedule to its ocean leg (port-to-port)."""
    P = s.get("port_of_discharge")
    etd = s.get("etd")
    port_eta = s.get("pod_eta")
    transit = (date.fromisoformat(port_eta) - date.fromisoformat(etd)).days \
        if (port_eta and etd) else None
    return {
        "id": _hash16("ZIM", P, etd, port_eta, s.get("mother_vessel"),
                      tuple(s.get("vessel_sequence") or [])),
        "port_of_discharge": P,
        "cutoff_date": s.get("cutoff_date"),
        "etd": etd,
        "eta": port_eta,                           # ETA == POD ETA (ocean-only)
        "pod_eta": port_eta,
        "transit_time_days": transit,
        "transport_type": s.get("transport_type"),
        "mother_vessel": s.get("mother_vessel"),
        "ts_ports": s.get("ts_ports"),
        "ts_vessels": s.get("ts_vessels"),
        "route_ports": s.get("route_ports"),
        "vessel_sequence": s.get("vessel_sequence"),
        "rail_transit_days": 0,
        "derived_from": "inland",
    }


def derive_ocean(canonical_dir=CANONICAL_DIR, ocean_dir=OCEAN_DIR, ports=None):
    """Derive port-to-port canonicals from inland canonicals. NO push. Returns count."""
    if ports is None:
        ports = {n for n, m in json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))["coverage"].items()
                 if m.get("type") == "port"}

    ocean_dir.mkdir(parents=True, exist_ok=True)
    for p in ocean_dir.glob("ZIM_*.json"):
        p.unlink()

    groups = {}
    n_files = n_in = n_derived = n_dup = 0
    for fp in sorted(canonical_dir.glob("ZIM_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        if rec.get("last_cy") in ports:            # skip any already-ocean canonicals
            continue
        n_files += 1
        origin = rec.get("port_of_loading")
        qd, sd = rec.get("query_date"), rec.get("snapshot_date")
        for s in rec.get("schedules", []):
            n_in += 1
            P = s.get("port_of_discharge")
            if not P or not s.get("pod_eta"):
                continue
            g = groups.setdefault((origin, P), {"qd": qd, "sd": sd, "scheds": {}})
            k = _ocean_key(origin, s)
            if k in g["scheds"]:
                n_dup += 1
                continue
            g["scheds"][k] = derive_ocean_schedule(s)
            n_derived += 1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    written = 0
    for (origin, P), g in groups.items():
        rec = {
            "schema_version": 2,
            "id": _hash16("ZIM", origin, P, g["qd"]),
            "carrier": {"code": "ZIM", "name": "ZIM Integrated Shipping Services"},
            "query_date": g["qd"],
            "snapshot_date": g["sd"],
            "port_of_loading": origin,
            "last_cy": P,
            "final_destination": None,
            "derived_from": "inland",
            "schedules": list(g["scheds"].values()),
        }
        o5 = (origin or "").replace(" ", "")[:5]
        p5 = (P or "").replace(" ", "")[:5]
        out = _unique_path(ocean_dir / f"ZIM_{o5}_{p5}_{ts}.json")
        out.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        written += 1

    print(f"Inland canonicals read   : {n_files}")
    print(f"Inland schedules         : {n_in}")
    print(f"Ocean schedules derived  : {n_derived}  (deduped {n_dup} shared sailings)")
    print(f"Canonicals written       : {written}  -> {ocean_dir}")
    if n_files == 0:
        print("\nℹ️ No inland canonicals found. Run mainv3.py first.")
    return written


def _ingest_ocean():
    import sys
    try:
        sys.path.insert(0, str(CARRIER_DIR.parents[1]))
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals("ZIM", canonical_dir=OCEAN_DIR,
                              ledger_path=OCEAN_DIR.parent / "ingest_ledger_ocean.json")
    except Exception as e:
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")


def main():
    """Standalone: derive + push (mainv3.py calls derive_ocean() and pushes at the end)."""
    if derive_ocean():
        _ingest_ocean()


if __name__ == "__main__":
    main()
