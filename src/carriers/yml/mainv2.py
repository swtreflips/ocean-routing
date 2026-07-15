import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# YML v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like the other v2 carriers: origins x
# type=="port" coverage, the v3 multi-sweep retry, a run-stats summary in the log,
# and a LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# YML (Yang Ming) mechanics: a clean JSON GET API (yangming.com/api/P2P/GetP2PRoutes)
# — no browser, no session/cookies (stateless), so transient failures (429 / 5xx /
# timeout) just requeue for the next sweep. get_locations() returns a LIST of codes:
# a couple of ports map to several sub-ports (e.g. Manila = North + South Harbour),
# so each (origin_code, port_code) combination is its own call. On requeue we skip
# combinations that already produced a raw file (idempotent). yml_cities.json is
# shared read-only with v1. coverage_v2.json is v2's OWN keys+type coverage (12 ports).

import json
import time
import random
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, date, timedelta

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_locations,
    build_canonical_record,
    get_unresolved,
)


def safe_to_csv(df, path, retries=5, backoff=1.0, **kwargs):
    """to_csv that retries on PermissionError (OneDrive sync / Excel locks)."""
    for attempt in range(retries):
        try:
            df.to_csv(path, **kwargs)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (attempt + 1))


# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

TEMP_DIR = ASSETS_DIR / "temp"            # v2 output (local)
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)   # UTC — query_date / filenames (audit trail)
today = date.today()                         # LOCAL date -> query window
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
today_api = today.strftime("%Y%m%d")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"YMLv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"YML_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage (no v3 exists for YML)

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

# --- API config ---
URL = "https://www.yangming.com/api/P2P/GetP2PRoutes"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/143.0.0.0",
    "Referer": "https://www.yangming.com/en/esolution/schedule/point_to_point_search",
}
fromDate = today_api
toDate = (today + timedelta(days=14)).strftime("%Y%m%d")
MAX_SWEEPS = 6                                  # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]       # seconds before each requeue sweep
# YML is the most sensitive API — pacing keeps v1's SAME human-like SHAPE (3 tiers,
# weighted 55% short / 30% medium / 15% long, with the occasional long pause), just
# scaled down ~3.3x from v1's human_sleep() (20-45 / 60-120 / 180-420s), the same
# proportion the other v2 carriers were tightened. Still deliberately slow — keep the
# distribution + long-pause tail; do not flatten to a uniform DELAY_RANGE.


def make_params(from_code, to_code):
    return {
        "locationCodeFrom": from_code, "serviceTermFrom": "Y",
        "locationCodeTo": to_code, "serviceTermTo": "Y",
        "priorityWay": "ALL", "dateDefinition": "DEP",
        "startDate": fromDate, "endDate": toDate,
    }


def _human_sleep_v2():
    """v1 human_sleep()'s weighted 3-tier cadence, tightened ~3.3x (same philosophy,
    faster). Mean ~27s/call vs v1's ~90s; keeps the occasional long pause."""
    r = random.random()
    if r < 0.55:
        return random.uniform(6, 13.5)     # was 20-45
    elif r < 0.85:
        return random.uniform(18, 36)      # was 60-120
    else:
        return random.uniform(54, 126)     # was 180-420


def _pace():
    """Wait between calls using YML's tightened (but still human-like) cadence."""
    s = _human_sleep_v2()
    print(f"⏳ Sleeping {s:.1f}s...")
    time.sleep(s)


# --- Build the query matrix: origins x PORTS -------------------------------
matrix_rows = []
_qid = 1
for pol in origins:
    for pod in port_dests:
        matrix_rows.append({
            "ID": f"V2-{_qid:04d}",
            "Port of Loading": pol,
            "Final Destination": None,
            "LastCY": pod,
            "status": "pending",
            "result_file": None,
        })
        _qid += 1

quotes = pd.DataFrame(matrix_rows)
quotes["result_file"] = quotes["result_file"].astype("string")

print(f"✅ Query matrix built: {len(origins)} origins x {len(port_dests)} ports = {len(quotes)} pairs.")
print(quotes[["ID", "Port of Loading", "LastCY", "status"]])

_unresolved = [p for p in port_dests if not get_locations(p)]
if _unresolved:
    print(f"⚠️ {len(_unresolved)} port(s) without a YML code (will be skipped): {_unresolved}")


# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(pol_name, pod_name, row, stats):
    """Query one (origin, port) pair — over every (origin_code, port_code) combo, since
    a few YML ports map to multiple sub-ports (e.g. Manila harbours). Save a wrapped raw
    file per successful combo. On requeue, combos that already produced a raw file this
    run are skipped (idempotent).

    Returns status:
      'done'              -> at least one combo produced schedules
      'no_records'        -> every combo returned empty / 400 (a real empty answer)
      'skipped_not_found' -> POL or POD has no YML code
      'pending'           -> a combo hit a TRANSIENT failure (429 / 5xx / timeout) — requeue
    """
    from_codes = get_locations(pol_name)
    to_codes = get_locations(pod_name)
    if not from_codes or not to_codes:
        print(f"⚠️ Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found"

    transient = False
    for fc in from_codes:
        for tc in to_codes:
            raw_path = RAW_DIR / f"YML_{fc}_{tc}_{filename_timestamp}.json"
            if raw_path.exists():
                continue                          # already scraped this combo this run
            stats["calls"] += 1
            try:
                resp = requests.get(URL, headers=HEADERS, params=make_params(fc, tc), timeout=30)
            except requests.RequestException as e:
                print(f"💥 {pol_name}({fc}) → {pod_name}({tc}): {e} (transient → requeue)")
                transient = True
                _pace()
                continue

            code = resp.status_code
            print(f"📡 {pol_name}({fc}) → {pod_name}({tc}): {code}")
            if code == 429 or code >= 500:        # rate-limited / server error → transient
                transient = True
            elif code == 200:
                try:
                    data = resp.json()            # YM returns a list of schedules
                except json.JSONDecodeError:
                    print(f"  ⚠️ Bad JSON for {fc} → {tc}")
                    data = None
                if data:
                    wrapped = {
                        "query_date": query_timestamp,
                        "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                        "PortOfLoading": pol_name,
                        "LastCY": pod_name,
                        "OFQ": row.get("ID"),
                        "FinalDestination": row.get("Final Destination"),
                        "locationCodeFrom": fc,
                        "locationCodeTo": tc,
                        "schedules": data,
                    }
                    with open(raw_path, "w", encoding="utf-8") as f:
                        json.dump(wrapped, f, ensure_ascii=False, indent=2)
                    print(f"  ✅ Saved {len(data)} schedules → {raw_path.name}")
                else:
                    print(f"  ⚪ No schedules for {fc} → {tc}")
            # else: 400 (no route) / other 4xx — skip this combo (terminal empty)

            _pace()

    if transient:
        return "pending"
    any_saved = any((RAW_DIR / f"YML_{fc}_{tc}_{filename_timestamp}.json").exists()
                    for fc in from_codes for tc in to_codes)
    return "done" if any_saved else "no_records"


def scrape_matrix(quotes):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Stateless
    HTTP; transient failures requeue for the next sweep after an escalating cooldown.
    A whole sweep that resolves nothing aborts. (Pacing is per API call inside
    _scrape_pair, since a pair can fan out to several sub-port calls.)"""
    stats = {"calls": 0}                       # total HTTP requests to the endpoint
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(row["Port of Loading"], row["LastCY"], row, stats)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — YML unresponsive; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Run summary to the log: totals, wall-clock, throughput. `elapsed` is full
    scrape wall-clock (includes any mid-run cooldowns)."""
    def _n(pred):
        return int(sum(1 for i in quotes.index if pred(quotes.at[i, "status"])))

    done = _n(lambda s: s == "done")
    no_records = _n(lambda s: s == "no_records")
    skipped = _n(lambda s: s == "skipped_not_found")
    pending = _n(lambda s: s == "pending")
    errors = _n(lambda s: isinstance(s, str) and s.startswith("error_"))

    mins = elapsed / 60
    per_min = calls / mins if mins > 0 else 0.0
    min_per_100 = (mins / calls * 100) if calls > 0 else 0.0

    print("\n" + "=" * 48)
    print("RUN STATS")
    print("=" * 48)
    print(f"  pairs           : {len(quotes)} total")
    print(f"    done          : {done}")
    print(f"    no_records    : {no_records}")
    print(f"    skipped       : {skipped}")
    print(f"    error         : {errors}")
    print(f"    pending(left) : {pending}")
    print(f"  API calls       : {calls}  (a pair can fan out to several sub-port calls)")
    print(f"  scrape elapsed  : {elapsed:.1f}s  ({mins:.2f} min)")
    print(f"  throughput      : {per_min:.1f} calls/min")
    print(f"                    {min_per_100:.2f} min per 100 calls")
    print("=" * 48)

    if pending:
        print(f"⚠️ {pending} pair(s) still pending after {MAX_SWEEPS} sweeps (no raw file for them).")


try:
    scrape_matrix(quotes)
    print("\n🎉 Quotes fetch complete!")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# === AFTER LOOP: build canonical records (one per query) + push ===
written = []
for file in os.listdir(RAW_DIR):
    if file.startswith("YML") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is None:
            continue
        pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
        last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
        out = get_unique_path(CANONICAL_DIR / f"YML_{pol5}_{last5}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        written.append(out)

unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"YMLv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("YML", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
