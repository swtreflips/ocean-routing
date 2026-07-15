"""
backtest_synthesis.py — validate synthesis against ground truth.

The existing inland canonicals (origin->inland scrapes) carry BOTH the real port
arrival (pod_eta) and the real yard arrival (eta). So we can run the actual
synthesis code path on them and compare the predicted yard_eta to the truth.

For each inland schedule:
    predicted = synth_schedule(s, yard, lookup_rail(...))   # the real code
    error     = (predicted.eta - actual.eta).days

NOTE: this is IN-SAMPLE (same data built coverage), so it measures whether the
mode/fallback + date math reproduce reality, not out-of-sample generalization.
A true out-of-sample test needs a fresh port scrape. Errors here should be tiny.

Run:  python backtest_synthesis.py
"""

import json
from collections import Counter
from datetime import date
from pathlib import Path

from synthesize_inland import build_index, lookup_rail, synth_schedule

CARRIER_DIR = Path(__file__).resolve().parent
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"


def main():
    coverage = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))["coverage"]
    inland = {n for n, m in coverage.items() if m.get("type") == "inland"}
    index = build_index(coverage)

    err_by_source = {"service": Counter(), "fallback": Counter()}
    n_total = n_predicted = n_nocover = 0

    for fp in sorted(CANONICAL_DIR.glob("COS_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        yard = rec.get("last_cy")
        if yard not in inland:
            continue
        for s in rec.get("schedules", []):
            if not s.get("pod_eta") or not s.get("eta"):
                continue
            n_total += 1
            P = s.get("port_of_discharge")
            services, fallback = index.get(P, {}).get(yard, ({}, None))
            rail, hm, source = lookup_rail(services, fallback, s.get("ocean_service"))
            if rail is None:
                n_nocover += 1
                continue
            pred = synth_schedule(s, yard, rail, hm, source)
            err = (date.fromisoformat(pred["eta"]) - date.fromisoformat(s["eta"])).days
            err_by_source[source][err] += 1
            n_predicted += 1

    print(f"Inland schedules           : {n_total}")
    print(f"Predicted                  : {n_predicted}")
    print(f"No coverage (skipped)      : {n_nocover}")
    print()
    for source in ("service", "fallback"):
        c = err_by_source[source]
        n = sum(c.values())
        if not n:
            print(f"[{source}] none")
            continue
        within1 = sum(v for e, v in c.items() if abs(e) <= 1)
        mae = sum(abs(e) * v for e, v in c.items()) / n
        dist = " ".join(f"{e:+d}:{c[e]}" for e in sorted(c))
        print(f"[{source}] n={n}  MAE={mae:.2f}  within±1={100*within1/n:.0f}%")
        print(f"   error days -> count: {dist}")


if __name__ == "__main__":
    main()
