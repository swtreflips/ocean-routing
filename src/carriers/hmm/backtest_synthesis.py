"""
backtest_synthesis.py — HMM: validate synthesis against ground truth.

The calibration inland canonicals carry both the real pod_eta (port arrival) and
the real eta (yard arrival). Run the real synthesis lookup on them and compare
predicted yard_eta to the truth.

    predicted = pod_eta + lookup(yard, port, ocean_service).rail_days
    error     = (predicted - actual eta).days

IN-SAMPLE (same data built coverage). Run: python backtest_synthesis.py
"""

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from synthesize_inland import build_index, lookup_rail

CARRIER_DIR = Path(__file__).resolve().parent
# back-test on the calibration canonicals (ground truth: real pod_eta + eta)
CANONICAL_DIR = CARRIER_DIR / "assets" / "temp" / "canonicals_calibrate"
COVERAGE_FILE = CARRIER_DIR / "assets" / "coverage.json"


def main():
    coverage = json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))["coverage"]
    inland = {n for n, m in coverage.items() if m.get("type") == "inland"}
    index = build_index(coverage)

    err_by_source = {"service": Counter(), "fallback": Counter()}
    n_total = n_pred = n_nocover = 0
    for fp in sorted(CANONICAL_DIR.glob("HMM_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        yard = rec.get("last_cy")
        if yard not in inland:
            continue
        for s in rec.get("schedules", []):
            if not s.get("pod_eta") or not s.get("eta"):
                continue
            n_total += 1
            conn = index.get(s.get("port_of_discharge"), {}).get(yard)
            if not conn:
                n_nocover += 1
                continue
            rail, source = lookup_rail(conn.get("services", {}), conn.get("fallback"), s.get("ocean_service"))
            if rail is None:
                n_nocover += 1
                continue
            pred = (date.fromisoformat(s["pod_eta"]) + timedelta(days=rail)).isoformat()
            err_by_source[source][(date.fromisoformat(pred) - date.fromisoformat(s["eta"])).days] += 1
            n_pred += 1

    print(f"Inland schedules           : {n_total}")
    print(f"Predicted                  : {n_pred}")
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
        print(f"[{source}] n={n}  MAE={mae:.3f}  within±1={100*within1/n:.0f}%  exact={100*c[0]/n:.0f}%")
        print(f"   error days -> count: {dist}")


if __name__ == "__main__":
    main()
