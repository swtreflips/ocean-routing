"""
extract_connections.py — EMC inland-yard rail connection table.

Walks the v2 calibration canonicals under assets/temp/canonicals/ and pools the
rail leg per (inland yard, discharge port) across all origins.

UNLIKE COS, EMC needs NO discriminator: Evergreen publishes the rail move as an
explicit "Intermodal" leg with a fixed contracted transit, so rail time is stable
per (yard, port) — calibration showed 37/38 lanes perfectly flat, zero spread >=2.
So each connection is simply {via_port, rail_days, min, max, n, samples}; the
lookup key is (last_cy, port_of_discharge), no ocean_service.

Run:  python extract_connections.py
No scraping, no network. Safe to re-run; recomputes from whatever canonicals exist.
"""

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# Flag a lane if its rail days still differ by this much (should be rare for EMC).
SPREAD_FLAG = 2

CARRIER_DIR = Path(__file__).resolve().parent
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"

SYNTHESIS_POLICY = {
    "lookup_key": ["last_cy", "port_of_discharge"],
    "estimate": "rail_days",
    "show_range": True,
}


def _mode(samples):
    """Most frequent value; ties favor the smaller day (samples pre-sorted)."""
    return Counter(samples).most_common(1)[0][0]


def _conn_stats(samples):
    samples = sorted(samples)
    return {
        "rail_days": _mode(samples),
        "min": samples[0],
        "max": samples[-1],
        "n": len(samples),
        "samples": samples,
    }


def _serialize(obj):
    """Pretty JSON, but keep each `samples` array inline on one line."""
    text = json.dumps(obj, indent=2)

    def collapse(m):
        inner = re.sub(r"\s+", " ", m.group(1)).strip()
        return '"samples": [' + inner + "]"

    return re.sub(r'"samples": \[([^\]]*)\]', collapse, text, flags=re.DOTALL)


def _with_policy(doc):
    """Return doc with synthesis_policy placed right before `coverage` (idempotent)."""
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

    # yard -> port -> [rail_days, ...], pooled across origins
    pooled = defaultdict(lambda: defaultdict(list))
    files = sorted(CANONICAL_DIR.glob("EMC_*.json"))
    for fp in files:
        rec = json.loads(fp.read_text(encoding="utf-8"))
        yard = rec.get("last_cy")
        if yard not in inland_yards:
            continue
        for sched in rec.get("schedules", []):
            rd = sched.get("rail_transit_days")
            pod = sched.get("port_of_discharge")
            if rd is None or not pod:
                continue
            pooled[yard][pod].append(rd)

    updated, discovered_ports, flagged = [], set(), []
    for yard, by_port in pooled.items():
        connections = []
        for port, samples in sorted(by_port.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            stats = _conn_stats(samples)
            connections.append({"via_port": port, **stats})
            if port not in known_ports and port not in inland_yards:
                discovered_ports.add(port)
            if stats["max"] - stats["min"] >= SPREAD_FLAG:
                flagged.append((yard, port, stats["min"], stats["max"], stats["n"]))
        coverage[yard]["connections"] = connections
        updated.append((yard, len(connections), sum(len(s) for s in by_port.values())))

    coverage_doc["description"] = (
        "Last CY coverage for Evergreen (EMC). type is port (scraped directly) or "
        "inland (rail-recreated). EMC publishes the rail leg explicitly (Intermodal "
        "leg), so rail time is stable per (via_port); connections give rail_days + "
        "range, keyed by (last_cy, port_of_discharge) with no discriminator. See synthesis_policy."
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
        print(f"\n🔀 (yard, port) lanes spread >= {SPREAD_FLAG} days (unexpected for EMC — investigate):")
        for yard, port, lo, hi, n in sorted(flagged, key=lambda x: -(x[3] - x[2])):
            print(f"     {yard:<20} via {port:<16} {lo}-{hi} days  (n={n})")
    else:
        print(f"\n✅ No (yard, port) lane spreads >= {SPREAD_FLAG} days — rail time is stable, no discriminator needed.")


if __name__ == "__main__":
    main()
