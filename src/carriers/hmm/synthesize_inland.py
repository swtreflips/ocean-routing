"""
synthesize_inland.py — HMM: recreate port->inland schedules from port->port scrapes.

HMM is the COS pattern: rail keyed by ocean service. Lookup follows coverage's
synthesis_policy — match (last_cy, port_of_discharge, ocean_service) ->
services[svc].rail_days; unseen service falls back to the port-level aggregate.

    yard_eta          = pod_eta + rail_days
    transit_time_days = (yard_eta - etd).days

Only PURE ocean arrivals (eta == pod_eta) seed synthesis: if HMM turns out
hub-and-spoke (a rail-routed schedule discharging at a hub and railing on), that
schedule's hub sailing is also in the hub's own canonical, so using it would
double-count (see /synth.md).

Inputs : port-to-port canonicals in assets/temp/canonicals/ (last_cy == a port)
Output : canonical JSONs in assets/temp/synthesized/, one per (origin -> inland
         yard), same schema (+ rail_source). Pushes to Supabase (own ledger).

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


def lookup_rail(services, fallback, svc):
    """Return (rail_days, source). source: 'service' | 'fallback' | None."""
    if svc and svc in services:
        return services[svc].get("rail_days"), "service"
    if fallback and fallback.get("rail_days") is not None:
        return fallback["rail_days"], "fallback"
    return None, None


def synth_schedule(s, yard, rail, source):
    """Build one synthesized inland schedule from a port-to-port schedule `s`."""
    P = s.get("port_of_discharge")
    etd = s.get("etd")
    pod_eta = s.get("pod_eta")
    yard_eta = (date.fromisoformat(pod_eta) + timedelta(days=rail)).isoformat()
    transit = (date.fromisoformat(yard_eta) - date.fromisoformat(etd)).days if etd else None
    return {
        "id": _hash16("HMM", P, yard, etd, s.get("mother_vessel"), s.get("id")),
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
        "discharge_terminal": s.get("discharge_terminal"),
        "vessel_carrier": s.get("vessel_carrier"),
        "rail_transit_days": rail,
        "rail_source": source,           # 'service' (exact) or 'fallback'
    }


def main():
    coverage_doc = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    coverage = coverage_doc["coverage"]
    ports = {n for n, m in coverage.items() if m.get("type") == "port"}
    index = build_index(coverage)

    SYNTH_DIR.mkdir(parents=True, exist_ok=True)
    for p in SYNTH_DIR.glob("HMM_*.json"):
        p.unlink()

    groups = {}
    n_src = n_sched = n_synth = n_skip = n_railrouted = n_invalid_routing = 0
    by_source = defaultdict(int)
    for fp in sorted(CANONICAL_DIR.glob("HMM_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        if rec.get("last_cy") not in ports:
            continue
        n_src += 1
        origin = rec.get("port_of_loading")
        qd, sd = rec.get("query_date"), rec.get("snapshot_date")
        for s in rec.get("schedules", []):
            n_sched += 1
            if s.get("eta") != s.get("pod_eta"):     # skip hub-spoke rail-routed
                n_railrouted += 1
                continue
            P = s.get("port_of_discharge")
            if not P or P not in index or not s.get("pod_eta"):
                continue
            svc = s.get("ocean_service")
            for yard, conn in index[P].items():
                # Routing-validity gate: only synthesize if this origin actually
                # routes to `yard` via `P` (origin-specific; see /synth.md).
                if origin not in conn.get("origins", []):
                    n_invalid_routing += 1
                    continue
                rail, source = lookup_rail(conn.get("services", {}), conn.get("fallback"), svc)
                if rail is None:
                    n_skip += 1
                    continue
                g = groups.setdefault((origin, yard), {"qd": qd, "sd": sd, "scheds": []})
                g["scheds"].append(synth_schedule(s, yard, rail, source))
                n_synth += 1
                by_source[source] += 1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    written = 0
    for (origin, yard), g in groups.items():
        rec = {
            "schema_version": 2,
            "id": _hash16("HMM", origin, yard, g["qd"]),
            "carrier": {"code": "HMM", "name": "HMM (Hyundai Merchant Marine)"},
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
        out = _unique_path(SYNTH_DIR / f"HMM_{o5}_{y5}_{ts}.json")
        out.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        written += 1

    print(f"Port-to-port canonicals read : {n_src}")
    print(f"Source schedules             : {n_sched}")
    print(f"Skipped rail-routed (hub-spoke): {n_railrouted}")
    print(f"Skipped invalid routing (origin gate): {n_invalid_routing}")
    print(f"Synthesized schedules        : {n_synth}  "
          f"(service={by_source['service']}, fallback={by_source['fallback']})")
    print(f"Skipped (no rail_days)       : {n_skip}")
    print(f"Canonicals written           : {written}  -> {SYNTH_DIR}")
    if n_src == 0:
        print("\nℹ️ No port-to-port canonicals found (last_cy must be a port). "
              "Run mainv2.py in ports mode first.")
    if written:
        _ingest_synthesized()


def _ingest_synthesized():
    """Push synthesized inland canonicals into Supabase (env-based, ledger-tracked)."""
    import sys
    try:
        sys.path.insert(0, str(CARRIER_DIR.parents[1]))   # .../src on path for `ingest`
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals(
            "HMM",
            canonical_dir=SYNTH_DIR,
            ledger_path=SYNTH_DIR.parent / "ingest_ledger_synthesized.json",
        )
    except Exception as e:
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
