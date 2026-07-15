import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# ONE v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like COS/EMC/HPL/MSC/MSK v2: origins x
# type=="port" coverage, the v3 multi-sweep retry, a run-stats summary in the log,
# and a LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# ONE mechanics: a clean JSON GET API (ecomm.one-line.com/api/v1/schedule/
# point-to-point) keyed by porCode/delCode + a few session cookies — no browser,
# no session bootstrap (stateless), so transient failures (429 congestion / 5xx /
# timeout) just requeue for the next sweep. one_cities.json is flat (one code per
# key — nothing to explode); v2 shares it read-only with v1/v3. coverage_v2.json is
# v2's OWN bare keys+type coverage (13 ports) — independent of v3's coverage.json.

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

TEMP_DIR = ASSETS_DIR / "temp"            # v2 output (separate from v3's temp_v3/)
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)   # UTC — query_date / filenames (audit trail)
today = date.today()                         # LOCAL date -> query window
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"ONEv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"ONE_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage — independent of v3's coverage.json

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

# one_cities.json is flat (one code per key); shared read-only with v1/v3.
cities_file = ASSETS_DIR / "one_cities.json"
with open(cities_file, "r", encoding="utf-8") as f:
    one_cities = json.load(f)


def get_locations(city_name):
    """Return [{"name", "code"}] for a city; [] if unknown or code-less. Wrapped in a
    list to match the matrix-scrape shape (utils.py has no get_locations for ONE)."""
    entry = one_cities.get(city_name)
    if not entry or not entry.get("code"):
        return []
    return [{"name": entry.get("name") or city_name, "code": entry["code"]}]

# --- API config ---
URL = "https://ecomm.one-line.com/api/v1/schedule/point-to-point"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://ecomm.one-line.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
COOKIES = {"sessLocale": "en", "usrCntCd": "US", "AKA_A2": "A"}
fromDate = today_iso
toDate = (today + timedelta(days=14)).strftime("%Y-%m-%d")
DELAY_RANGE = (0.75, 1.5)                       # between pairs within a sweep
#                                                 (tightened ~3.3x from (2.5, 5), the same proportion
#                                                 COS was cut (2,5)->(0.5,1.5); raise if ONE's 429
#                                                 congestion / requeues pick up)
MAX_SWEEPS = 6                                  # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]       # seconds before each requeue sweep


def make_params(pol_code, pod_code):
    return {
        "porCode": pol_code, "delCode": pod_code,
        "rcvTermCode": "Y", "deTermCode": "Y", "tsFlag": "",
        "fromDate": fromDate, "toDate": toDate,
        "polCode": "", "podCode": "", "polYardCode": "", "podYardCode": "",
        "standardizationEtaEtb": "false", "cargoNature": "GP", "searchType": "List",
    }


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
    print(f"⚠️ {len(_unresolved)} port(s) without a ONE code (will be skipped): {_unresolved}")


# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(pol_name, pod_name, row, stats):
    """One ONE GET for a pair; save a wrapped raw file on success.

    Returns (status, out_path). status is one of:
      'done'              -> schedules saved
      'no_records'        -> 200 but empty scheduleLines (a real empty answer)
      'skipped_not_found' -> POL or POD has no ONE code
      'pending'           -> TRANSIENT failure (429 congestion / 5xx / timeout) — requeue
      'error_<code>'      -> a non-transient 4xx (surfaced, not requeued)
    """
    pol_locs = get_locations(pol_name)
    pod_locs = get_locations(pod_name)
    if not pol_locs or not pod_locs:
        print(f"⚠️ Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found", None

    pol, pod = pol_locs[0], pod_locs[0]
    stats["calls"] += 1
    try:
        resp = requests.get(URL, headers=HEADERS, params=make_params(pol["code"], pod["code"]),
                            cookies=COOKIES, timeout=60)
    except requests.RequestException as e:
        print(f"💥 {pol_name} → {pod_name}: {e} (transient → requeue)")
        return "pending", None

    code = resp.status_code
    print(f"📡 {pol_name}({pol['code']}) → {pod_name}({pod['code']}): {code}")
    if code == 429 or code >= 500:               # server busy / overloaded → transient
        return "pending", None
    if code != 200:                              # 4xx (e.g. 403) → non-transient, surface
        return f"error_{code}", None

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print(f"⚠️ Bad JSON for {pol_name} → {pod_name}")
        return "error_badjson", None

    schedules = data.get("scheduleLines", [])
    if not schedules:
        print(f"⚪ No schedules for {pol_name} → {pod_name}")
        return "no_records", None

    wrapped = {
        "query_date": query_timestamp,
        "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
        "PortofLoading": pol_name,
        "LastCY": pod_name,
        "OFQ": row.get("ID"),
        "FinalDestination": row.get("Final Destination"),
        "schedules": schedules,
    }
    pol_short = pol_name.replace(" ", "")[:5]
    pod_short = pod_name.replace(" ", "")[:5]
    out = get_unique_path(RAW_DIR / f"ONE_{pol_short}_{pod_short}_{filename_timestamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(wrapped, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved {len(schedules)} schedule line(s) → {out}")
    return "done", out


def scrape_matrix(quotes):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Stateless
    HTTP, so no session bootstrap; transient failures (ONE congestion 429s) just
    requeue for the next sweep after an escalating cooldown. A whole sweep that
    resolves nothing aborts."""
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
            status, out = _scrape_pair(row["Port of Loading"], row["LastCY"], row, stats)
            quotes.at[idx, "status"] = status
            if out is not None:
                quotes.at[idx, "result_file"] = str(out)
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — ONE saturated; stopping with {still} pending.")
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
    print(f"  API calls       : {calls}")
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
all_canonical = []
for file in os.listdir(RAW_DIR):
    if file.startswith("ONE_") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is not None:
            all_canonical.append(rec)

unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"ONEv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

written = []
for rec in all_canonical:
    pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
    last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
    out = get_unique_path(CANONICAL_DIR / f"ONE_{pol5}_{last5}_{filename_timestamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, default=str)
    written.append(out)

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("ONE", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
