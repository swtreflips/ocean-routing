"""
extract_connections.py — HMM inland-yard rail connection table.

Walks the v2 canonicals under assets/temp/canonicals/ and pools the rail leg per
(inland yard, discharge port, ocean_service) across all origins. Survey showed
HMM is the COS pattern: the ocean service loop (`mthLoopCd`, stored as
`ocean_service`) pins rail time to +-1 day, so connections are service-nested
with a per-port `fallback` for services unseen during calibration.

    connections: [
      { "via_port": "Los Angeles, CA",
        "services": { "PS3": {rail_days, min, max, n, samples}, ... },
        "fallback": {rail_days, min, max, n} }
    ]

A top-level `synthesis_policy` records the lookup. Run: python extract_connections.py
"""

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

SPREAD_FLAG = 3

CARRIER_DIR = Path(__file__).resolve().parent
# calibration canonicals (origin->inland); separate from the ports dir
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals_calibrate"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"

SYNTHESIS_POLICY = {
    "lookup_key": ["last_cy", "port_of_discharge", "ocean_service"],
    "estimate": "rail_days",
    "on_unknown_service": "fallback",
    # Routing validity is origin-specific: only synthesize a connection for an
    # origin (port_of_loading) listed in that connection's `origins`. An
    # uncalibrated origin matches nothing and emits nothing (see /synth.md).
    "validity_gate": "origins",
    "show_range": True,
}


def _mode(samples):
    return Counter(samples).most_common(1)[0][0]


def _service_stats(samples):
    samples = sorted(samples)
    return {
        "rail_days": _mode(samples),
        "min": samples[0],
        "max": samples[-1],
        "n": len(samples),
        "samples": samples,
    }


def _fallback_stats(samples):
    samples = sorted(samples)
    return {"rail_days": _mode(samples), "min": samples[0], "max": samples[-1], "n": len(samples)}


def _serialize(obj):
    text = json.dumps(obj, indent=2)

    def collapse(m):
        field = m.group(1)
        inner = re.sub(r"\s+", " ", m.group(2)).strip()
        return f'"{field}": [' + inner + "]"

    # keep `samples` and `origins` arrays inline (no nested brackets inside)
    return re.sub(r'"(samples|origins)": \[([^\]]*)\]', collapse, text, flags=re.DOTALL)


def _with_policy(doc):
    out = {}
    for k, v in doc.items():
        if k == "synthesis_policy":
            continue
        if k == "coverage":
            out["synthesis_policy"] = SYNTHESIS_POLICY
        out[k] = v
    return out


def main():
    coverage_doc = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    coverage = coverage_doc["coverage"]

    inland_yards = {n for n, m in coverage.items() if m.get("type") == "inland"}
    known_ports = {n for n, m in coverage.items() if m.get("type") == "port"}

    def _bucket():
        return {"svc": defaultdict(list), "all": [], "origins": set()}

    pooled = defaultdict(lambda: defaultdict(_bucket))
    files = sorted(CANONICAL_DIR.glob("HMM_*.json"))
    for fp in files:
        rec = json.loads(fp.read_text(encoding="utf-8"))
        yard = rec.get("last_cy")
        if yard not in inland_yards:
            continue
        origin = rec.get("port_of_loading")
        for sched in rec.get("schedules", []):
            rd = sched.get("rail_transit_days")
            pod = sched.get("port_of_discharge")
            svc = sched.get("ocean_service")
            if rd is None or not pod:
                continue
            b = pooled[yard][pod]
            b["all"].append(rd)
            if origin:
                b["origins"].add(origin)          # routing-validity gate (origin-specific)
            if svc:
                b["svc"][svc].append(rd)

    updated, discovered_ports, flagged = [], set(), []
    for yard, by_port in pooled.items():
        connections = []
        for port, b in sorted(by_port.items(), key=lambda kv: (-len(kv[1]["all"]), kv[0])):
            services = {}
            for svc, samples in sorted(b["svc"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
                st = _service_stats(samples)
                services[svc] = st
                if st["max"] - st["min"] >= SPREAD_FLAG:
                    flagged.append((yard, port, svc, st["min"], st["max"], st["n"]))
            connections.append({
                "via_port": port,
                "origins": sorted(b["origins"]),     # only these origins route via this port
                "services": services,
                "fallback": _fallback_stats(b["all"]),
            })
            if port not in known_ports and port not in inland_yards:
                discovered_ports.add(port)
        coverage[yard]["connections"] = connections
        updated.append((yard, len(connections), sum(len(b["all"]) for b in by_port.values())))

    coverage_doc["description"] = (
        "Last CY coverage for HMM. type is port (scraped directly) or inland "
        "(rail-recreated). For inland yards, connections nests a services map keyed by "
        "ocean_service (mthLoopCd) -> rail_days (the synthesis lookup) + a fallback "
        "aggregate. See synthesis_policy."
    )
    COVERAGE_FILE.write_text(_serialize(_with_policy(coverage_doc)) + "\n", encoding="utf-8")

    print(f"Canonicals scanned : {len(files)}")
    print(f"Inland yards filled : {len(updated)} / {len(inland_yards)}")
    print()
    print("yard                       ports  samples")
    for yard, nconn, nsamp in sorted(updated):
        print(f"  {yard:<24} {nconn:>4}   {nsamp:>5}")

    empty = sorted(inland_yards - {y for y, *_ in updated})
    if empty:
        print(f"\nInland yards with NO data (left as-is): {empty}")

    if discovered_ports:
        print("\n⚠️  Discharge ports seen in data but NOT in coverage.json (add as ports?):")
        for p in sorted(discovered_ports):
            print(f"     {p}")

    if flagged:
        print(f"\n🔀 (yard, port, service) lanes still spread >= {SPREAD_FLAG} days:")
        for yard, port, svc, lo, hi, n in sorted(flagged, key=lambda x: -(x[4] - x[3])):
            print(f"     {yard:<20} via {port:<16} [{svc:<6}] {lo}-{hi} days  (n={n})")
    else:
        print(f"\n✅ No (yard, port, service) lane spreads >= {SPREAD_FLAG} days — service keying is tight.")


if __name__ == "__main__":
    main()
