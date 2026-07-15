import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search, unlike v3 which scraped inland and derived ports,
# surfacing extra sailings the port-to-port view hides).
#
# What it does:
#   * No Voronoi resolving. The query set is an explicit matrix of every origin in
#     data/origins.csv  x  every PORT in assets/coverage.json (type == "port").
#     origin -> port only; no inland leg, no derive, no secondary pass.
#   * Multi-sweep retry (ported from the v3 ONE/ZIM model): transient failures
#     (403/429/5xx anti-bot, timeout) are re-queued across sweeps with an
#     escalating cooldown; the session is re-bootstrapped once per sweep on a
#     non-200; a whole sweep that resolves nothing aborts.
#   * Output under assets/temp/: raw/ (wrapped API responses, written as each pair
#     succeeds) and canonicals/ (built after the loop). Canonicals ARE pushed to
#     Supabase (ledger-tracked, ingest_ledger_canonicals.json).
#   * coverage.json is a bare keys+type utility (port/inland); port<->inland-yard
#     relationships live in a separate project.

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_ids_inplace,
    get_unresolved,
)
from utilsv2 import build_canonical_record_v2
import pandas as pd
import datetime
from pathlib import Path
### API CALL libraries
import json
import time
import requests
import undetected_chromedriver as uc

# Silence undetected_chromedriver's __del__ cleanup. We already call
# driver.quit() explicitly inside get_new_session(); the destructor only fires
# at interpreter shutdown when sys.stdout is already torn down, which throws
# a noisy 'OSError: handle is invalid' that doesn't affect any data.
uc.Chrome.__del__ = lambda self: None
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random
import shutil


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

# --- Shared inputs ---
DATA_DIR = PROJECT_ROOT / "data"

# --- Carrier-specific folder (cos/) ---
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

# --- v2 output: local temp only (no DB). raw while querying, canonicals after. ---
TEMP_DIR = ASSETS_DIR / "temp"
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"

# --- v2 logs land in assets/temp so the whole run (raw, canonicals, log) is
#     in one place and easy to inspect after a run. ---
LOG_DIR = TEMP_DIR

# --- Ensure output dirs exist ---
for _d in (RAW_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Run timestamps. query_date/filenames stay UTC (audit trail), but the query
#     WINDOW date (fromDate/toDate/snapshot) uses the LOCAL calendar date so a run
#     made after UTC-midnight doesn't jump a day ahead and drop the current day's
#     imminent sailings — matching what a UI port-to-port search shows at the desk.
run_timestamp = datetime.datetime.now(datetime.timezone.utc)
today = datetime.date.today()                                     # LOCAL date -> query window
today_iso = today.strftime("%Y-%m-%d")                            # for assign_snapshot / fromDate
today_str = today.strftime("%m.%d.%y")                            # for log filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")    # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")    # Windows-safe (no ':')

progress_file = get_unique_filename(LOG_DIR / f"COSCOv2_{today_str}.csv")

# Log set up
logfile = get_unique_filename(LOG_DIR / f"cosco_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (v2-specific) ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"   # v2's OWN coverage — independent of v3's coverage.json
cities_file = ASSETS_DIR / "cos_cities.json"      # shared read-only (v2 doesn't modify/explode it)

# --- Build the query matrix: origins x PORTS -------------------------------
# v2 scrapes origin -> port only. Inland schedules are recreated offline by
# synthesize_inland.py, which adds the service-keyed rail leg from coverage.json.
origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()

with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

matrix_rows = []
_qid = 1
for pol in origins:
    for pod in port_dests:
        matrix_rows.append({
            "ID": f"V2-{_qid:04d}",
            "Port of Loading": pol,
            "Final Destination": None,   # v2 queries port -> port directly
            "LastCY": pod,               # the discharge port (no inland leg)
            "status": "pending",
            "result_file": None,
        })
        _qid += 1

quotes = pd.DataFrame(matrix_rows)
quotes["result_file"] = quotes["result_file"].astype("string")

print(f"✅ Query matrix built: {len(origins)} origins x {len(port_dests)} ports "
      f"= {len(quotes)} pairs.")
print(quotes[["ID", "Port of Loading", "LastCY", "status"]])

# Dynamic dates (use the UTC `today` captured at startup; see DATE.md)
fromDate = today_iso
toDate = (today + datetime.timedelta(days=26)).strftime("%Y-%m-%d")

# Base URL
url = "https://elines.coscoshipping.com/ebschedule/public/purpoShipmentWs"

# 🔹 Helper: handle cookie popup
def handle_cookie_popup(driver, timeout=5):
    try:
        wait = WebDriverWait(driver, timeout)
        allow_button = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[.//span[normalize-space()='Allow All']]")
        ))
        allow_button.click()
        print("🍪 Accepted cookies.")
        # wait until modal disappears
        WebDriverWait(driver, 5).until_not(
            EC.presence_of_element_located((By.CLASS_NAME, "ivu-modal-content"))
        )
    except Exception:
        print("No cookie popup found (or already dismissed).")

# 🔹 Function to bootstrap cookies + headers
def get_new_session():
    print("🌐 Bootstrapping new COSCO session...")
    options = uc.ChromeOptions()
    options.headless = True
    options.add_argument("--window-size=1920,1080")

    print("  🚗 Launching undetected Chrome (headless)...")
    driver = uc.Chrome(
        version_main=148,   # ✅ THIS goes here
        options=options
    )
    print("  ✓ Chrome launched")

    # Step 1: Open root domain to set cookies
    print("  🌍 Loading root domain (elines.coscoshipping.com)...")
    driver.get("https://elines.coscoshipping.com")
    time.sleep(1)  # give it a second to load
    print("  ✓ Root domain loaded")

    # Step 2: Inject consent cookies
    driver.add_cookie({
        "name": "cookieClause",
        "value": "Accepted",
        "domain": "elines.coscoshipping.com",
        "path": "/",
    })
    driver.add_cookie({
        "name": "cookiePreference",
        "value": "Accepted",
        "domain": "elines.coscoshipping.com",
        "path": "/",
    })
    print("  ✓ Consent cookies injected")

    # Step 3: Navigate to target page
    print("  🧭 Navigating to schedule search page...")
    driver.get("https://elines.coscoshipping.com/ebusiness/sailingSchedule/searchByCity")
    time.sleep(2)  # let the page load
    print("  ✓ Schedule page loaded")

    # Step 3b: Dismiss the consent/cookie popup if it rendered. The injected
    # cookies don't always suppress it, and the popup's <p> overlay intercepts
    # the city-select click (ElementClickInterceptedException) when it lingers.
    handle_cookie_popup(driver)

    # Step 4: Interact with input
    print("  🔍 Probing city-select to populate session state...")
    input_el = driver.find_element(By.CSS_SELECTOR, "input.ivu-select-input")
    try:
        input_el.click()
    except Exception as e:
        # Fallback: an overlay still intercepted the click — click via JS,
        # which ignores z-index/overlay hit-testing.
        print(f"  ⚠️ Direct click intercepted ({type(e).__name__}); clicking via JS.")
        driver.execute_script("arguments[0].click();", input_el)
    input_el.send_keys("Los")
    wait = WebDriverWait(driver, 10)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.ivu-select-item")))
    print("  ✓ City-select responded — session is warm")

    # Step 5: Grab all session cookies
    cookies = driver.get_cookies()
    cookie_dict = {c['name']: c['value'] for c in cookies}

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": driver.current_url,
        "Origin": "https://elines.coscoshipping.com",
        "language": "en_US",
        "sys": "eb",
    }

    driver.quit()
    print(f"✅ COSCO session established ({len(cookies)} cookies)")
    return cookies, headers

from datetime import datetime
import calendar

# Assign snapshot period

# Functions
def get_month_periods(year, month):
    """Return reference dates for a given month: start, mid, end."""
    start = datetime(year, month, 1)
    mid = datetime(year, month, 15)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day)
    return {'start': start, 'mid': mid, 'end': end}

def assign_snapshot(date_input):
    """
    Assign a snapshot period to a given date.
    - If within 5 days of the 1st → snap to that 1st.
    - If within 5 days of the 15th → snap to the 15th.
    - If day >= 28 → snap to the 1st of next month.
    - Otherwise → keep the original date.
    """
    date_input = datetime.strptime(date_input, '%Y-%m-%d')

    year, month = date_input.year, date_input.month
    periods = get_month_periods(year, month)

    # Snap near the 1st (only if in first 5 days of the month)
    if 1 <= date_input.day <= 5:
        return periods['start'].date()

    # Snap near the 15th
    if abs((date_input - periods['mid']).days) <= 5:
        return periods['mid'].date()

    # Snap to 1st of next month if late in month
    if date_input.day >= 28:
        if month == 12:
            return datetime(year + 1, 1, 1).date()
        else:
            return datetime(year, month + 1, 1).date()

    # Otherwise, keep original date
    return date_input.date()

# Load city mapping
with open(cities_file, "r") as f:
    allcoscocities = json.load(f)

# Pre-flight: every port key must resolve in cos_cities.json, else it silently
# becomes a not_found skip. Surface any gap loudly before burning a session.
_unresolved_ports = [p for p in port_dests if p not in allcoscocities]
if _unresolved_ports:
    print(f"⚠️ {len(_unresolved_ports)} port(s) not in cos_cities.json (will be skipped): "
          f"{_unresolved_ports}")

snapshot_date = assign_snapshot(today_iso)

# --- Retry config (v3-style multi-sweep drain) ---------------------------
# Sweep 1 tries every pending pair; a pair that hits a TRANSIENT failure
# (non-200 anti-bot / 429 / 5xx / timeout) stays 'pending' and is re-attempted
# on the next sweep after an escalating cooldown. The first non-200 in a sweep
# re-bootstraps the COSCO session once (fresh cookies usually clear the block);
# a whole sweep that resolves nothing aborts (COSCO hard-blocking). Successes
# write their raw file the moment they happen.
MAX_SWEEPS = 6
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]     # seconds before each requeue sweep
# Tight delay between calls within a sweep (stress-test pace — was (2,5) + a 6%
# 10–20s spike). Lower to push COSCO harder; raise if 403s start appearing.
PAIR_DELAY_RANGE = (0.5, 1.5)


def _scrape_pair(pol_name, pod_name, row, creds, sweep_state, stats):
    """Query one (origin, port) pair ONCE; write a wrapped raw file on success.

    Returns (status, out_path). status is one of:
      'completed'    -> schedules saved
      'no_records'   -> 200 but empty (a real "nothing here" answer)
      'not_found'    -> POL or POD missing from cos_cities.json
      'pending'      -> TRANSIENT failure (403/429/5xx anti-bot / timeout / conn
                        error) — leave pending so the next sweep retries it
      'error_<code>' -> a non-transient 4xx (surfaced, not requeued)
    """
    origin_data = allcoscocities.get(pol_name)
    destination_data = allcoscocities.get(pod_name)
    if not origin_data or not destination_data:
        print(f"⚠️ Skipping: city not found for {pol_name}__{pod_name}")
        return "not_found", None

    payload = {
        "fromDate": fromDate, "toDate": toDate,
        "pickup": "C", "delivery": "C", "estimateDate": "D",
        "originCityUuid": origin_data["cityUuid"],
        "destinationCityUuid": destination_data["cityUuid"],
        "originCity": origin_data["fullFormate"] + "," + origin_data["unloCode"],
        "destinationCity": destination_data["fullFormate"] + "," + destination_data["unloCode"],
        "cargoNature": "GC"
    }

    for attempt in (1, 2):
        creds["headers"]["X-Client-Timestamp"] = str(int(time.time() * 1000))
        stats["calls"] += 1                       # count every HTTP request to the endpoint
        try:
            resp = requests.post(
                url, headers=creds["headers"],
                cookies={c["name"]: c["value"] for c in creds["cookies"]},
                json=payload, timeout=20)
        except requests.RequestException as e:
            print(f"💥 {pol_name} → {pod_name}: {e} (transient → requeue)")
            return "pending", None

        code = resp.status_code
        print(f"🔎 {pol_name} → {pod_name}: {code} (attempt {attempt})")
        if code != 200:
            if attempt == 1 and not sweep_state["rebooted"]:
                sweep_state["rebooted"] = True
                print(f"❌ {code} — re-bootstrapping COSCO session...")
                c, h = get_new_session()
                creds["cookies"], creds["headers"] = c, h
                continue                              # retry same pair with fresh creds
            # blocked/throttled → requeue; other 4xx → surface
            return ("pending" if code in (403, 429) or code >= 500 else f"error_{code}"), None

        data = resp.json().get("data", {})
        records = data.get("records") or data.get("content", {}).get("data", [])
        print(f"DEBUG: keys={list(data.keys())}, records={len(records)}")
        if not records:
            print(f"⚠️ No schedules for {pol_name} → {pod_name}")
            return "no_records", None

        wrapped_data = {
            "query_date": query_timestamp,
            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "LastCY": pod_name,
            "OFQ": row.get("ID"),
            "FinalDestination": row.get("Final Destination"),
            "PortofLoading": pol_name,
            "schedules": records,
        }
        pol_short = pol_name.replace(" ", "")[:5]
        pod_short = pod_name.replace(" ", "")[:5]
        out_file = RAW_DIR / f"COS_{pol_short}_{pod_short}_{filename_timestamp}.json"
        with open(out_file, "w") as f:
            json.dump(wrapped_data, f, indent=2)
        print(f"✅ Got {len(records)} schedules for {pol_name} → {pod_name}")
        return "completed", out_file

    return "pending", None


def scrape_matrix(quotes, creds):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model)."""
    stats = {"calls": 0}                       # total HTTP requests to the endpoint
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        sweep_state = {"rebooted": False}      # one session re-bootstrap allowed per sweep
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status, out = _scrape_pair(row["Port of Loading"], row["LastCY"], row, creds, sweep_state, stats)
            quotes.at[idx, "status"] = status
            if out is not None:
                quotes.at[idx, "result_file"] = str(out)
            if status != "pending":
                resolved += 1
            sleep_time = random.uniform(*PAIR_DELAY_RANGE)
            print(f"⏳ Sleeping {sleep_time:.1f}s...")
            time.sleep(sleep_time)

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — COSCO blocking; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Write a run summary to the log: totals, wall-clock, and throughput.
    `elapsed` is the full scrape wall-clock (includes any mid-run cooldowns and
    session re-bootstraps) — i.e. the real end-to-end pace, not just active time."""
    def _n(pred):
        return int(sum(1 for i in quotes.index if pred(quotes.at[i, "status"])))

    completed = _n(lambda s: s == "completed")
    no_records = _n(lambda s: s == "no_records")
    not_found = _n(lambda s: s == "not_found")
    pending = _n(lambda s: s == "pending")
    errors = _n(lambda s: isinstance(s, str) and s.startswith("error_"))

    mins = elapsed / 60
    per_min = calls / mins if mins > 0 else 0.0
    min_per_100 = (mins / calls * 100) if calls > 0 else 0.0

    print("\n" + "=" * 48)
    print("RUN STATS")
    print("=" * 48)
    print(f"  pairs           : {len(quotes)} total")
    print(f"    completed     : {completed}")
    print(f"    no_records    : {no_records}")
    print(f"    not_found     : {not_found}")
    print(f"    error         : {errors}")
    print(f"    pending(left) : {pending}")
    print(f"  API calls       : {calls}")
    print(f"  scrape elapsed  : {elapsed:.1f}s  ({mins:.2f} min)")
    print(f"  throughput      : {per_min:.1f} calls/min")
    print(f"                    {min_per_100:.2f} min per 100 calls")
    print("=" * 48)

    left = pending
    if left:
        print(f"⚠️ {left} pair(s) still pending after {MAX_SWEEPS} sweeps (no raw file for them).")


# --- Start session (bootstrapped once; re-bootstrapped inside a sweep on 403) ---
_c, _h = get_new_session()
creds = {"cookies": _c, "headers": _h}

try:
    scrape_matrix(quotes, creds)
    print("\n🎉 Quotes fetch complete!")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# --- Step A: forward-fill leg IDs in every raw JSON ---
for file in os.listdir(RAW_DIR):
    if file.startswith("COS_") and file.endswith(".json"):
        full_path = os.path.join(RAW_DIR, file)
        assign_ids_inplace(full_path)

# --- Step B: build canonical records (one per query) ---
all_canonical = []
for file in os.listdir(RAW_DIR):
    if file.startswith("COS_") and file.endswith(".json"):
        full_path = os.path.join(RAW_DIR, file)
        rec = build_canonical_record_v2(full_path)
        if rec is not None:
            all_canonical.append(rec)

# --- Log any ports that couldn't be resolved against portdbCanonical.json ---
unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"COSv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

try:
    # --- Write canonical JSONs (one per schedule), with rollback on failure ---
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            fname = f"COS_{pol5}_{last5}_{filename_timestamp}.json"
            out = get_unique_path(CANONICAL_DIR / fname)
            with open(out, "w") as f:
                json.dump(rec, f, indent=2, default=str)
            written_canonical.append(out)
    except Exception:
        for p in written_canonical:
            p.unlink(missing_ok=True)
        raise

    print(f"✅ Wrote {len(written_canonical)} canonical JSON(s) → {CANONICAL_DIR}")

    # --- Ingest the port-to-port canonicals into Supabase (env-based, ledger-tracked) ---
    # Reuses v1's ingest with the v2 temp dir + a v2-specific ledger so the two
    # don't collide. Best-effort: never sinks an otherwise-successful scrape.
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals(
            "COS",
            canonical_dir=CANONICAL_DIR,
            ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json",
        )
    except Exception as e:
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")

except Exception as e:
    print(f"❌ Transform failed. Raw JSONs kept in {RAW_DIR}.")
    print("Error:", e)
