"""
backtest_synthesis.py — EMC: validate synthesis against ground truth.

The calibration inland canonicals carry BOTH the real pod_eta (port arrival) and
the real eta (yard arrival). Run the actual synthesis lookup on them and compare
predicted yard_eta to the truth.

    predicted = pod_eta + lookup(yard, port).rail_days
    error     = (predicted - actual eta).days

IN-SAMPLE (same data built coverage) — measures whether the (yard, port) mode
reproduces reality. Errors should be ~0 since EMC rail is stable.

Run:  python backtest_synthesis.py
"""

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from synthesize_inland import build_index

CARRIER_DIR = Path(__file__).resolve().parent
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"


def main():
    coverage = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))["coverage"]
    inland = {n for n, m in coverage.items() if m.get("type") == "inland"}
    index = build_index(coverage)

    errs = Counter()
    n_total = n_pred = n_nocover = 0
    for fp in sorted(CANONICAL_DIR.glob("EMC_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        yard = rec.get("last_cy")
        if yard not in inland:
            continue
        for s in rec.get("schedules", []):
            if not s.get("pod_eta") or not s.get("eta"):
                continue
            n_total += 1
            conn = index.get(s.get("port_of_discharge"), {}).get(yard)
            if not conn or conn.get("rail_days") is None:
                n_nocover += 1
                continue
            pred = (date.fromisoformat(s["pod_eta"]) + timedelta(days=conn["rail_days"])).isoformat()
            errs[(date.fromisoformat(pred) - date.fromisoformat(s["eta"])).days] += 1
            n_pred += 1

    print(f"Inland schedules           : {n_total}")
    print(f"Predicted                  : {n_pred}")
    print(f"No coverage (skipped)      : {n_nocover}")
    if n_pred:
        within1 = sum(v for e, v in errs.items() if abs(e) <= 1)
        mae = sum(abs(e) * v for e, v in errs.items()) / n_pred
        dist = " ".join(f"{e:+d}:{errs[e]}" for e in sorted(errs))
        print(f"\nMAE={mae:.3f}  within±1={100*within1/n_pred:.0f}%  exact={100*errs[0]/n_pred:.0f}%")
        print(f"error days -> count: {dist}")


if __name__ == "__main__":
    main()
