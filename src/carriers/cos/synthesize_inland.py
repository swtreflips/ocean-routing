"""
synthesize_inland.py — recreate port→inland schedules from port→port scrapes.

v2 scrapes origin→port only. This turns each port-to-port schedule into all the
inland schedules that port serves, by adding the rail leg looked up from
coverage.json (keyed by ocean service):

    yard_eta          = pod_eta + rail_days
    transit_time_days = (yard_eta - etd).days        # == ocean_transit + rail_days

Lookup follows coverage's `synthesis_policy`: match (last_cy, port_of_discharge,
ocean_service) -> services[svc].rail_days; if the service wasn't seen during
calibration, fall back to the port-level aggregate.

Inputs : port-to-port canonicals in assets/temp/canonicals/ (last_cy == a port)
         + assets/coverage.json (service-keyed)
Output : canonical JSONs in assets/temp/synthesized/, one per (origin -> inland
         yard), same schema as a real inland canonical (+ synthesized/rail_source).

Run:  python synthesize_inland.py   (stdlib only, no scrape, no DB)
"""

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CARRIER_DIR = Path(__file__).resolve().parent
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals"
SYNTH_DIR = CARRIER_DIR / "assets" / "temp" / "synthesized"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"


# --- light helpers (stdlib-only, mirror utils so this runs with any Python) ---
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


def build_index(coverage):
    """port -> { inland_yard: (services_map, fallback) } from coverage.connections."""
    index = defaultdict(dict)
    for yard, meta in coverage.items():
        if meta.get("type") != "inland":
            continue
        for conn in meta.get("connections", []):
            port = conn.get("via_port")
            if port:
                index[port][yard] = (conn.get("services", {}), conn.get("fallback"))
    return index


def lookup_rail(services, fallback, svc):
    """Return (rail_days, haulage_mode, source). source: 'service' | 'fallback' | None."""
    if svc and svc in services:
        s = services[svc]
        return s.get("rail_days"), s.get("haulage_mode"), "service"
    if fallback and fallback.get("rail_days") is not None:
        return fallback["rail_days"], None, "fallback"
    return None, None, None


def synth_schedule(s, yard, rail, haulage_mode, source):
    """Build one synthesized inland schedule from a port-to-port schedule `s`."""
    pod_eta = s.get("pod_eta")
    etd = s.get("etd")
    yard_eta = (date.fromisoformat(pod_eta) + timedelta(days=rail)).isoformat()
    transit = (date.fromisoformat(yard_eta) - date.fromisoformat(etd)).days if etd else None
    return {
        "id": _hash16("COS", s.get("port_of_discharge"), yard, etd,
                      s.get("mother_vessel"), s.get("id")),
        "port_of_discharge": s.get("port_of_discharge"),
        "cutoff_date": s.get("cutoff_date"),
        "etd": etd,
        "eta": yard_eta,                       # availability at the inland yard
        "pod_eta": pod_eta,                    # arrival at the sea port (unchanged)
        "transit_time_days": transit,          # end-to-end ETD -> yard
        "transport_type": s.get("transport_type"),
        "mother_vessel": s.get("mother_vessel"),
        "ts_ports": s.get("ts_ports"),
        "ts_vessels": s.get("ts_vessels"),
        "route_ports": s.get("route_ports"),
        "vessel_sequence": s.get("vessel_sequence"),
        "ocean_service": s.get("ocean_service"),
        "discharge_terminal": s.get("discharge_terminal"),
        "rail_transit_days": rail,
        "haulage_mode": haulage_mode,
        "rail_source": source,                 # 'service' (exact) or 'fallback'
    }


def main():
    coverage_doc = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    coverage = coverage_doc["coverage"]
    ports = {name for name, meta in coverage.items() if meta.get("type") == "port"}
    index = build_index(coverage)

    SYNTH_DIR.mkdir(parents=True, exist_ok=True)
    for p in SYNTH_DIR.glob("COS_*.json"):
        p.unlink()

    # group synthesized schedules by (origin, inland yard)
    groups = {}
    n_src_files = n_src_scheds = n_synth = n_skipped = 0
    src_by_source = defaultdict(int)

    for fp in sorted(CANONICAL_DIR.glob("COS_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        if rec.get("last_cy") not in ports:       # only port-to-port canonicals
            continue
        n_src_files += 1
        origin = rec.get("port_of_loading")
        qd, sd = rec.get("query_date"), rec.get("snapshot_date")
        for s in rec.get("schedules", []):
            n_src_scheds += 1
            P = s.get("port_of_discharge")
            if not P or P not in index or not s.get("pod_eta"):
                continue
            svc = s.get("ocean_service")
            for yard, (services, fallback) in index[P].items():
                rail, hm, source = lookup_rail(services, fallback, svc)
                if rail is None:
                    n_skipped += 1
                    continue
                g = groups.setdefault((origin, yard), {"qd": qd, "sd": sd, "scheds": []})
                g["scheds"].append(synth_schedule(s, yard, rail, hm, source))
                n_synth += 1
                src_by_source[source] += 1

    # write one canonical per (origin, inland yard)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    written = 0
    for (origin, yard), g in groups.items():
        rec = {
            "schema_version": 2,
            "id": _hash16("COS", origin, yard, g["qd"]),
            "carrier": {"code": "COS", "name": "COSCO Shipping Lines"},
            "query_date": g["qd"],
            "snapshot_date": g["sd"],
            "port_of_loading": origin,
            "last_cy": yard,
            "final_destination": None,
            "synthesized": True,
            "schedules": g["scheds"],
        }
        o5 = (origin or "").replace(" ", "")[:5]
        y5 = (yard or "").replace(" ", "")[:5]
        out = _unique_path(SYNTH_DIR / f"COS_{o5}_{y5}_{ts}.json")
        out.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        written += 1

    print(f"Port-to-port canonicals read : {n_src_files}")
    print(f"Source schedules             : {n_src_scheds}")
    print(f"Synthesized schedules        : {n_synth}  "
          f"(service={src_by_source['service']}, fallback={src_by_source['fallback']})")
    print(f"Skipped (no coverage)        : {n_skipped}")
    print(f"Canonicals written           : {written}  -> {SYNTH_DIR}")
    if n_src_files == 0:
        print("\nℹ️ No port-to-port canonicals found (last_cy must be a port). "
              "Run mainv2.py in ports mode first.")

    if written:
        _ingest_synthesized()


def _ingest_synthesized():
    """Push synthesized inland canonicals into Supabase (same env-based, ledger-
    tracked ingest as v1/mainv2). Guarded so a stdlib-only run still synthesizes
    even if the supabase/dotenv packages aren't installed."""
    import sys
    try:
        sys.path.insert(0, str(CARRIER_DIR.parents[1]))   # .../src on path for `ingest`
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals(
            "COS",
            canonical_dir=SYNTH_DIR,
            ledger_path=SYNTH_DIR.parent / "ingest_ledger_synthesized.json",
        )
    except Exception as e:
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
