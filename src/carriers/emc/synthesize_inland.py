"""
synthesize_inland.py — EMC: recreate port->inland schedules from port->port scrapes.

EMC needs NO discriminator (rail time is stable per yard+port), so the lookup is a
plain (last_cy, port_of_discharge) -> rail_days from coverage.json:

    yard_eta          = pod_eta + rail_days
    transit_time_days = (yard_eta - etd).days

Inputs : port-to-port canonicals in assets/temp/canonicals/ (last_cy == a port)
         + assets/coverage.json
Output : canonical JSONs in assets/temp/synthesized/, one per (origin -> inland
         yard), same schema as a real EMC inland canonical (+ rail_source). Pushes
         to Supabase (own ledger).

Run:  python synthesize_inland.py
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
    """port -> { inland_yard: connection_dict } from coverage.connections."""
    index = defaultdict(dict)
    for yard, meta in coverage.items():
        if meta.get("type") != "inland":
            continue
        for conn in meta.get("connections", []):
            port = conn.get("via_port")
            if port:
                index[port][yard] = conn
    return index


def synth_schedule(s, yard, conn):
    """Build one synthesized inland schedule from a port-to-port schedule `s`."""
    P = s.get("port_of_discharge")
    etd = s.get("etd")
    pod_eta = s.get("pod_eta")
    rail = conn["rail_days"]
    yard_eta = (date.fromisoformat(pod_eta) + timedelta(days=rail)).isoformat()
    transit = (date.fromisoformat(yard_eta) - date.fromisoformat(etd)).days if etd else None
    return {
        "id": _hash16("EMC", P, yard, etd, s.get("mother_vessel"), s.get("id")),
        "port_of_discharge": P,
        "cutoff_date": s.get("cutoff_date"),
        "etd": etd,
        "eta": yard_eta,                 # availability at the inland yard
        "pod_eta": pod_eta,              # arrival at the sea port (unchanged)
        "transit_time_days": transit,    # end-to-end ETD -> yard
        "transport_type": s.get("transport_type"),
        "mother_vessel": s.get("mother_vessel"),
        "ts_ports": s.get("ts_ports"),
        "ts_vessels": s.get("ts_vessels"),
        "route_ports": s.get("route_ports"),
        "vessel_sequence": s.get("vessel_sequence"),
        "ocean_service": s.get("ocean_service"),
        "rail_transit_days": rail,
        # synthesize the explicit Intermodal leg for parity with real EMC records
        "intermodal_legs": [{"from": P, "to": yard, "service": "Intermodal", "transit_days": rail}],
        "intermodal_transit_days": rail,
        "rail_source": "lookup",         # (yard, port) -> rail_days; EMC has no discriminator
    }


def main():
    coverage_doc = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    coverage = coverage_doc["coverage"]
    ports = {n for n, m in coverage.items() if m.get("type") == "port"}
    index = build_index(coverage)

    SYNTH_DIR.mkdir(parents=True, exist_ok=True)
    for p in SYNTH_DIR.glob("EMC_*.json"):
        p.unlink()

    groups = {}
    n_src = n_sched = n_synth = n_skip = n_railrouted = 0
    for fp in sorted(CANONICAL_DIR.glob("EMC_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        if rec.get("last_cy") not in ports:        # only port-to-port canonicals
            continue
        n_src += 1
        origin = rec.get("port_of_loading")
        qd, sd = rec.get("query_date"), rec.get("snapshot_date")
        for s in rec.get("schedules", []):
            n_sched += 1
            # Only synthesize from PURE ocean arrivals (eta == pod_eta). EMC is
            # hub-and-spoke: a rail-routed schedule (eta != pod_eta) discharges at
            # a hub and already rails to a secondary port — its hub sailing is
            # also in the hub's own canonical, so using it here would double-count.
            if s.get("eta") != s.get("pod_eta"):
                n_railrouted += 1
                continue
            P = s.get("port_of_discharge")
            if not P or P not in index or not s.get("pod_eta"):
                continue
            for yard, conn in index[P].items():
                if conn.get("rail_days") is None:
                    n_skip += 1
                    continue
                g = groups.setdefault((origin, yard), {"qd": qd, "sd": sd, "scheds": []})
                g["scheds"].append(synth_schedule(s, yard, conn))
                n_synth += 1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    written = 0
    for (origin, yard), g in groups.items():
        rec = {
            "schema_version": 2,
            "id": _hash16("EMC", origin, yard, g["qd"]),
            "carrier": {"code": "EMC", "name": "Evergreen"},
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
        out = _unique_path(SYNTH_DIR / f"EMC_{o5}_{y5}_{ts}.json")
        out.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        written += 1

    print(f"Port-to-port canonicals read : {n_src}")
    print(f"Source schedules             : {n_sched}")
    print(f"Synthesized schedules        : {n_synth}")
    print(f"Skipped rail-routed (hub-spoke): {n_railrouted}")
    print(f"Skipped (no rail_days)       : {n_skip}")
    print(f"Canonicals written           : {written}  -> {SYNTH_DIR}")
    if n_src == 0:
        print("\nℹ️ No port-to-port canonicals found (last_cy must be a port). "
              "Run the ports scrape first.")
    if written:
        _ingest_synthesized()


def _ingest_synthesized():
    """Push synthesized inland canonicals into Supabase (env-based, ledger-tracked)."""
    import sys
    try:
        sys.path.insert(0, str(CARRIER_DIR.parents[1]))   # .../src on path for `ingest`
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals(
            "EMC",
            canonical_dir=SYNTH_DIR,
            ledger_path=SYNTH_DIR.parent / "ingest_ledger_synthesized.json",
        )
    except Exception as e:
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
