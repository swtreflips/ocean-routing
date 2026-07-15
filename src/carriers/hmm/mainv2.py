import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# HMM v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like COS/EMC v2: origins x type=="port"
# coverage, the v3 multi-sweep retry, a run-stats summary in the log, and a
# LOCAL-date query window.
#
#   MODE = "ports"     PRODUCTION / final. Matrix = origins x type=="port" keys
#                      (origin -> port only). Pushes the port canonicals to Supabase.
#   MODE = "calibrate" Retained (not deleted) for the separate port<->inland-yard
#                      relationships project: matrix = origins x type=="inland" yards,
#                      writes canonicals for extract_connections.py — NO Supabase push.
#
# HMM mechanics: patchright (Playwright) browser session (CSRF) + a 2-step JSON API
# (INIT apiPointToPointList -> GrmNo -> RESULT selectPointToPointList -> grmData),
# so each pair = TWO API calls. Runs in the `patch` conda env. get_hmm_code()
# lowercases, so the geojson "City, GA" names resolve against the city map directly.
# Sweep re-establishes the CSRF session at most once per sweep on a non-200.
# coverage.json is a bare keys+type utility (port / inland / sibling — a co-located
# or phantom port intentionally not queried; e.g. Long Beach sibling of LA, Seattle
# phantom of Tacoma).

# calibrate = scrape inland (relationships project); ports = go live.
# ⚠️ Set to "ports" for production runs.
MODE = "ports"

import json
import time
import random
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, date
from patchright.sync_api import sync_playwright

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_hmm_code,
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
            wait = backoff * (attempt + 1)
            print(f"🔒 CSV locked ({path}), retrying in {wait}s...")
            time.sleep(wait)


# --- Project root (ocean-routing/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"

# --- Carrier-specific folder (hmm/) ---
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

# --- v2 output: local temp only (no DB) ---
# calibrate and ports write to SEPARATE dirs so a calibration run (which feeds
# coverage + the origins gate) never clobbers the port canonicals that
# synthesize_inland.py needs, and vice-versa.
TEMP_DIR = ASSETS_DIR / "temp"
if MODE == "calibrate":
    RAW_DIR = TEMP_DIR / "raw_calibrate"
    CANONICAL_DIR = TEMP_DIR / "canonicals_calibrate"   # read by extract_connections.py
else:  # ports
    RAW_DIR = TEMP_DIR / "raw"
    CANONICAL_DIR = TEMP_DIR / "canonicals"             # read by synthesize_inland.py
LOG_DIR = TEMP_DIR

for d in (RAW_DIR, CANONICAL_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)   # UTC — query_date / filenames (audit trail)
today = date.today()                         # LOCAL date -> query window (srchSailDate); a run made
#                                              after UTC-midnight keeps the local day so imminent
#                                              same-day sailings aren't dropped (matches a UI search)
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
today_api = today.strftime("%Y%m%d")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"HMMv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"HMM_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"   # v2's OWN coverage — independent of v3's coverage.json

# --- Build the query matrix: origins x (ports | inland yards) --------------
origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()

with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]

if MODE == "ports":
    dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]
    dest_label = "ports"
elif MODE == "calibrate":
    dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
    dest_label = "inland yards"
else:
    raise ValueError(f"Unknown MODE: {MODE!r} (use 'ports' or 'calibrate')")

matrix_rows = []
_qid = 1
for pol in origins:
    for pod in dests:
        matrix_rows.append({
            "ID": f"V2-{_qid:04d}",
            "Port of Loading": pol,
            "LastCY": pod,
            "status": "pending",
            "result_file": None,
        })
        _qid += 1

quotes = pd.DataFrame(matrix_rows)
quotes["result_file"] = quotes["result_file"].astype("string")

print(f"✅ [{MODE}] Query matrix built: {len(origins)} origins x {len(dests)} {dest_label} "
      f"= {len(quotes)} pairs.")
print(quotes[["ID", "Port of Loading", "LastCY", "status"]])

# Pre-flight: every destination must resolve to an HMM code.
_unresolved = [p for p in dests if not get_hmm_code(p)]
if _unresolved:
    print(f"⚠️ {len(_unresolved)} {dest_label} without an HMM code (will be skipped): {_unresolved}")

# === HMM API CONFIGURATION ===
URL = "https://www.hmm21.com/e-service/general/schedule/ScheduleMain.do"
API_INIT = "https://www.hmm21.com/e-service/general/schedule/apiPointToPointList.do"
API_RESULT = "https://www.hmm21.com/e-service/general/schedule/selectPointToPointList.do"

COMMON_HEADERS = {
    "content-type": "application/json; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
    "origin": "https://www.hmm21.com",
    "referer": "https://www.hmm21.com/e-service/general/schedule/ScheduleMain.do",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

DELAY_RANGE = (0.45, 1.2)                      # between pairs within a sweep
#                                                (tightened ~3.3x from (1.5, 4), the same proportion
#                                                COS was cut (2,5)->(0.5,1.5); raise if 403s / CSRF
#                                                re-establishes start appearing)
MAX_SWEEPS = 6                                 # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]      # seconds before each requeue sweep


def extract_csrf(context, page):
    """Extract CSRF token from cookies, meta tags, or JS globals."""
    csrf = None
    for cookie in context.cookies():
        if "csrf" in cookie["name"].lower():
            csrf = cookie["value"]
            print(f"CSRF from cookie '{cookie['name']}': {csrf}")
            break
    if not csrf:
        csrf = page.evaluate(
            "() => document.querySelector('meta[name=\"_csrf\"]')?.getAttribute('content')"
        )
        if csrf:
            print(f"CSRF from meta tag: {csrf}")
    if not csrf:
        csrf = page.evaluate(
            "() => window._csrf || window.csrfToken || window.CSRF_TOKEN || null"
        )
        if csrf:
            print(f"CSRF from JS global: {csrf}")
    if not csrf:
        print("WARNING: Could not find CSRF token.")
    return csrf


def establish_session(context, page):
    """Visit HMM site and extract CSRF token for API calls."""
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    page.wait_for_timeout(5000)
    csrf = extract_csrf(context, page)
    headers = {**COMMON_HEADERS, "x-csrf-token": csrf or ""}
    return headers


# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(page, context, state, pol_name, pod_name, row, sweep_state, stats):
    """One HMM 2-step query (INIT -> GrmNo -> RESULT -> grmData); save wrapped JSON
    on success. On a non-200 the CSRF session is re-established once per sweep and
    the call retried; anything that still fails goes back to 'pending' (requeue).

    Returns (status, out_path). status is one of:
      'done'              -> schedules saved
      'no_records'        -> query returned no grmData (a real empty answer)
      'skipped_not_found' -> POL or POD has no HMM code
      'pending'           -> TRANSIENT failure (non-200 / exception) — requeue
    """
    pol_code = get_hmm_code(pol_name)
    pod_code = get_hmm_code(pod_name)
    if not pol_code or not pod_code:
        miss = []
        if not pol_code:
            miss.append(f"POL '{pol_name}'")
        if not pod_code:
            miss.append(f"POD '{pod_name}'")
        print(f"⚠️ No HMM code for {', '.join(miss)}")
        return "skipped_not_found", None

    payload_init = {
        "srchViewType": "L", "srchPointFromCd": pol_code, "srchCityFrom": "CY",
        "srchPointToCd": pod_code, "srchCityTo": "CY", "srchSailDate": today_api,
        "srchSelWeeks": "4", "srchSelPriority": "A", "srchSelSortBy": "D",
        "srchPorFcltyCd": "", "srchPvyFcltyCd": "", "itemPolCd": "", "itemPodCd": "",
        "paramToday": today_api,
    }

    # --- INIT (with one per-sweep session re-establish on a non-200) ---
    grm_no = None
    for attempt in (1, 2):
        stats["calls"] += 1
        try:
            res1 = page.request.post(API_INIT, headers=state["headers"], data=json.dumps(payload_init))
        except Exception as e:
            print(f"💥 {pol_name} → {pod_name} INIT: {e} (transient → requeue)")
            return "pending", None
        if res1.status == 200:
            try:
                grm_no = res1.json()["RTN_DATA"]["resultData"]["GrmNo"]
                break
            except Exception as e:
                print(f"⚠️ {pol_name} → {pod_name} INIT parse: {e} (requeue)")
                return "pending", None
        print(f"  INIT {res1.status} for {pol_name} → {pod_name}")
        if attempt == 1 and not sweep_state["rebooted"]:
            sweep_state["rebooted"] = True
            print("  ↻ re-establishing HMM session...")
            state["headers"] = establish_session(context, page)
            continue
        return "pending", None
    if grm_no is None:
        return "pending", None

    # --- RESULT ---
    payload_result = {"srchViewType": "L", "srchGrmNo": grm_no, "grmSeqs": "",
                      "srchSelPriority": "A", "srchSelSortBy": "D", "isNew": True}
    headers2 = {**state["headers"], "accept": "application/json, text/javascript, */*; q=0.01"}
    stats["calls"] += 1
    try:
        res2 = page.request.post(API_RESULT, headers=headers2, data=json.dumps(payload_result))
    except Exception as e:
        print(f"💥 {pol_name} → {pod_name} RESULT: {e} (requeue)")
        return "pending", None
    if res2.status != 200:
        print(f"  RESULT {res2.status} for {pol_name} → {pod_name} (requeue)")
        return "pending", None
    try:
        schedules = res2.json().get("grmData")
    except Exception as e:
        print(f"⚠️ {pol_name} → {pod_name} RESULT parse: {e} (requeue)")
        return "pending", None

    if not schedules:
        print(f"  ⚪ No schedules for {pol_name} → {pod_name}")
        return "no_records", None

    wrapped = {
        "query_date": query_timestamp,
        "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
        "LastCY": pod_name,
        "OFQ": row.get("ID"),
        "FinalDestination": row.get("Final Destination"),
        "PortOfLoading": pol_name,
        "schedules": schedules,
    }
    pol_short = pol_name.replace(" ", "")[:5]
    pod_short = pod_name.replace(" ", "")[:5]
    out = get_unique_path(RAW_DIR / f"HMM_{pol_short}_{pod_short}_{pol_code}_{pod_code}_{filename_timestamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(wrapped, f, ensure_ascii=False, indent=2)
    print(f"  ✅ Saved {len(schedules)} schedules → {out}")
    return "done", out


def scrape_matrix(page, context, state, quotes):
    """Multi-sweep drain of the matrix (the v3 retry model). CSRF session is
    re-established at most once per sweep on a non-200; transient failures requeue.
    A whole sweep that resolves nothing aborts."""
    stats = {"calls": 0}                       # total HMM API POSTs (INIT + RESULT)
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        sweep_state = {"rebooted": False}      # one session re-establish allowed per sweep
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status, out = _scrape_pair(page, context, state, row["Port of Loading"], row["LastCY"],
                                       row, sweep_state, stats)
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
            print(f"🛑 zero progress this sweep — HMM unresponsive; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Run summary to the log: totals, wall-clock, throughput. HMM makes TWO API
    calls per pair (INIT + RESULT), so 'API calls' ≈ 2× resolved pairs. `elapsed`
    is full scrape wall-clock (includes cooldowns / session re-establishes)."""
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
    print(f"  API calls       : {calls}  (HMM = INIT+RESULT per pair)")
    print(f"  scrape elapsed  : {elapsed:.1f}s  ({mins:.2f} min)")
    print(f"  throughput      : {per_min:.1f} calls/min")
    print(f"                    {min_per_100:.2f} min per 100 calls")
    print("=" * 48)

    if pending:
        print(f"⚠️ {pending} pair(s) still pending after {MAX_SWEEPS} sweeps (no raw file for them).")


# === MAIN: one browser, kept alive across the whole sweep ===
with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chrome", headless=True,
        args=["--disable-blink-features=AutomationControlled", "--disable-http2"])
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
    page = context.new_page()

    print("Warming up browser...")
    page.goto("https://www.hmm21.com", wait_until="commit", timeout=60000)
    page.wait_for_timeout(2000)
    print("Establishing HMM session...")
    state = {"headers": establish_session(context, page)}

    try:
        scrape_matrix(page, context, state, quotes)
    except (Exception, KeyboardInterrupt) as e:
        crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
        safe_to_csv(quotes, crash_file, index=False)
        print(f"💥 Run failed: {e}")
        print(f"📋 Partial progress saved to: {crash_file}")
        raise
    finally:
        browser.close()

print("✅ All scraping done.")


# === AFTER LOOP: build canonical records (one per query) ===
all_canonical = []
for file in os.listdir(RAW_DIR):
    if file.startswith("HMM_") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is not None:
            all_canonical.append(rec)

unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"HMMv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

written = []
for rec in all_canonical:
    pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
    last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
    out = get_unique_path(CANONICAL_DIR / f"HMM_{pol5}_{last5}_{filename_timestamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, default=str)
    written.append(out)

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

if MODE == "ports":
    # --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals(
            "HMM",
            canonical_dir=CANONICAL_DIR,
            ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json",
        )
    except Exception as e:
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")
    print("ℹ️ Next: run  python synthesize_inland.py  to recreate + push inland schedules.")
else:
    print(f"ℹ️ [{MODE}] No Supabase push. Inland canonicals are ready to survey / extract.")
