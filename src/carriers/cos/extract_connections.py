"""
extract_connections.py — build the inland-yard rail connection table.

Walks every v2 canonical record under assets/temp/canonicals/, and for each
inland yard pools the rail leg across ALL origins (rail time is origin-
independent). Each schedule already carries the precomputed `rail_transit_days`,
`ocean_service`, and `haulage_mode` (see utilsv2), so this is a pure read.

The rail leg is keyed by OCEAN SERVICE — analysis showed (yard, port, service)
predicts rail days to <=2 days (80% exact), far tighter than haulage_mode. So
each connection nests a `services` map (the synthesis lookup) plus a `fallback`
aggregate used when a production service wasn't seen during calibration:

    connections: [
      { "via_port": "Long Beach, CA",
        "services": { "SEA3": {rail_days, haulage_mode, min, max, n, samples}, ... },
        "fallback": {rail_days, min, max, n} }
    ]

A top-level `synthesis_policy` block records how to consume this table.
`haulage_mode` is kept per service as display metadata only (service already
implies it).

Run:  python extract_connections.py
No scraping, no network — pure local analysis. Safe to re-run.
"""

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# A (yard, port, service) lane is flagged if its rail days still differ by this
# much — after service-keying this should be rare (residual terminal/dwell).
SPREAD_FLAG = 3

CARRIER_DIR = Path(__file__).resolve().parent
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"

SYNTHESIS_POLICY = {
    "lookup_key": ["last_cy", "port_of_discharge", "ocean_service"],
    "estimate": "rail_days",
    "on_unknown_service": "fallback",
    "show_range": True,
}


def _mode(samples):
    """Most frequent value; ties favor the smaller day (samples pre-sorted)."""
    return Counter(samples).most_common(1)[0][0]


def _service_stats(samples, haulage_modes):
    """Per-service rail block: point estimate + range + samples for recompute."""
    samples = sorted(samples)
    hm = Counter(h for h in haulage_modes if h is not None)
    return {
        "rail_days": _mode(samples),
        "haulage_mode": hm.most_common(1)[0][0] if hm else None,
        "min": samples[0],
        "max": samples[-1],
        "n": len(samples),
        "samples": samples,
    }


def _fallback_stats(samples):
    """Port-level aggregate (all services pooled) for unseen-service lookups."""
    samples = sorted(samples)
    return {
        "rail_days": _mode(samples),
        "min": samples[0],
        "max": samples[-1],
        "n": len(samples),
    }


def _serialize(obj):
    """Pretty JSON, but keep each `samples` array inline on one line."""
    text = json.dumps(obj, indent=2)

    def collapse(m):
        inner = re.sub(r"\s+", " ", m.group(1)).strip()
        return '"samples": [' + inner + "]"

    # samples contain only digits/commas/space/newlines -> no nested brackets
    return re.sub(r'"samples": \[([^\]]*)\]', collapse, text, flags=re.DOTALL)


def _with_policy(doc):
    """Return doc with synthesis_policy placed right before `coverage` (idempotent)."""
    out = {}
    for k, v in doc.items():
        if k == "synthesis_policy":
            continue  # drop any previous copy; re-inserted below
        if k == "coverage":
            out["synthesis_policy"] = SYNTHESIS_POLICY
        out[k] = v
    return out


def main():
    coverage_doc = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
    coverage = coverage_doc["coverage"]

    inland_yards = {name for name, meta in coverage.items() if meta.get("type") == "inland"}
    known_ports = {name for name, meta in coverage.items() if meta.get("type") == "port"}

    # yard -> port -> {service -> [rail_days], "_all" -> [rail_days], "_hm" -> {service: [modes]}}
    def _bucket():
        return {"svc": defaultdict(list), "all": [], "hm": defaultdict(list)}

    pooled = defaultdict(lambda: defaultdict(_bucket))
    files = sorted(CANONICAL_DIR.glob("COS_*.json"))
    for fp in files:
        rec = json.loads(fp.read_text(encoding="utf-8"))
        yard = rec.get("last_cy")
        if yard not in inland_yards:
            continue
        for sched in rec.get("schedules", []):
            rd = sched.get("rail_transit_days")
            pod = sched.get("port_of_discharge")
            svc = sched.get("ocean_service")
            if rd is None or not pod:
                continue
            b = pooled[yard][pod]
            b["all"].append(rd)
            if svc:                                  # named service feeds the lookup
                b["svc"][svc].append(rd)
                b["hm"][svc].append(sched.get("haulage_mode"))

    # Write connections back into each inland yard that produced data.
    updated, discovered_ports, flagged = [], set(), []
    for yard, by_port in pooled.items():
        connections = []
        for port, b in sorted(by_port.items(), key=lambda kv: (-len(kv[1]["all"]), kv[0])):
            services = {}
            for svc, samples in sorted(b["svc"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
                st = _service_stats(samples, b["hm"][svc])
                services[svc] = st
                if st["max"] - st["min"] >= SPREAD_FLAG:
                    flagged.append((yard, port, svc, st["min"], st["max"], st["n"]))
            connections.append({
                "via_port": port,
                "services": services,
                "fallback": _fallback_stats(b["all"]),
            })
            if port not in known_ports and port not in inland_yards:
                discovered_ports.add(port)
        coverage[yard]["connections"] = connections
        updated.append((yard, len(connections), sum(len(b["all"]) for b in by_port.values())))

    coverage_doc["description"] = (
        "Last CY coverage for COSCO. type is port (scraped directly) or inland "
        "(rail-recreated). For inland yards, connections lists each via_port with a "
        "services map keyed by ocean_service -> rail_days (the synthesis lookup), plus "
        "a fallback aggregate for services unseen during calibration. See synthesis_policy."
    )
    COVERAGE_FILE.write_text(_serialize(_with_policy(coverage_doc)) + "\n", encoding="utf-8")

    # ---- report ----
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
        print(f"\n🔀 (yard, port, service) lanes still spread >= {SPREAD_FLAG} days "
              f"(residual terminal/dwell — use the range):")
        for yard, port, svc, lo, hi, n in sorted(flagged, key=lambda x: -(x[4] - x[3])):
            print(f"     {yard:<20} via {port:<16} [{svc:<6}] {lo}-{hi} days  (n={n})")
    else:
        print(f"\n✅ No (yard, port, service) lane spreads >= {SPREAD_FLAG} days — service keying is tight.")


if __name__ == "__main__":
    main()
