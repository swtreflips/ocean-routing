import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

from utils import (
    chrome_major,
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_ids_inplace,
    build_schedule_rows,
    build_canonical_record,
    get_unresolved,
)
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import datetime
from pathlib import Path
### API CALL libraries
import os
import pandas as pd
import json
import datetime
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
import sys
from pathlib import Path
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


# --- Project root (Schedules/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# --- Shared folders ---
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "src" / "data" / "cos" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "cos" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "cos"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "cos" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "cos" / "canonical"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent

# --- Ensure output dirs exist (they're gitignored, so a fresh clone lacks them) ---
for _d in (LOG_DIR, RAW_DIR, PROCESSING_DIR, TABLES_DIR, CSV_DIR, CANONICAL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Capture a single UTC run timestamp at startup (see DATE.md). All rows,
#     files, and metadata produced by this run derive from this one moment. ---
run_timestamp = datetime.datetime.now(datetime.timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")                            # for assign_snapshot
today_str = today.strftime("%m.%d.%y")                            # for log filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")    # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")    # Windows-safe (no ':')

progress_file = get_unique_filename(LOG_DIR / f"COSCO{today_str}.csv")

# Log set up
logfile = get_unique_filename(LOG_DIR / f"cosco_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "cos_yards.geojson"

# --- Load data ---
quotes = pd.read_csv(quotes_file)
locations = pd.read_csv(locations_file)
gdf_voronoi = gpd.read_file(voronoi_file)

# --- Initialize geocoder ---
geolocator = Nominatim(user_agent="voronoi_lookup")

# --- Step 1: Load or initialize progress ---
quotes_progress = load_progress(quotes_file, progress_file)

# --- Step 2: Resolve missing destinations ---
locations = resolve_missing_locations(quotes_progress, locations, locations_file, geocode_city, geolocator)

# --- Step 3: Build Voronoi lookup ---
lookup = build_voronoi_lookup(quotes_progress, locations, gdf_voronoi)

# --- Step 4: Fill LastCY only if missing ---
quotes_progress["LastCY"] = quotes_progress.apply(
    lambda row: row["LastCY"] if pd.notnull(row["LastCY"]) else lookup.get(row["Final Destination"]),
    axis=1
)

# --- Step 5: Hand off to API loop (in-memory; no intermediate disk write) ---
print("✅ Geocoding complete.")
print(quotes_progress[["ID", "Final Destination", "LastCY", "status"]])

# API Call Script
cities_file = CARRIER_DIR / "assets" / "cos_cities.json"

quotes = quotes_progress

# Ensure tracking columns exist
if "LastCY" not in quotes.columns:
    quotes["LastCY"] = None
if "status" not in quotes.columns:
    quotes["status"] = "pending"
if "result_file" not in quotes.columns:
    quotes["result_file"] = None

quotes["result_file"] = quotes["result_file"].astype("string")

# Dynamic dates (use the UTC `today` captured at startup; see DATE.md)
fromDate = today_iso
toDate = (today + datetime.timedelta(days=26)).strftime("%Y-%m-%d")
# Compute snapshot date for today

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
        version_main=chrome_major(),   # detected at runtime; Chrome auto-updates
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

    # Step 4: Interact with input
    print("  🔍 Probing city-select to populate session state...")
    input_el = driver.find_element(By.CSS_SELECTOR, "input.ivu-select-input")
    input_el.click()
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

snapshot_date = assign_snapshot(today_iso)
# --- Retry config (v2-style multi-sweep drain) ---------------------------
# Sweep 1 tries every pending quote; a quote that hits a TRANSIENT failure
# (403/429/5xx anti-bot, timeout, connection error) stays 'pending' and is
# re-attempted on the next sweep after an escalating cooldown. The first non-200
# in a sweep re-bootstraps the COSCO session once (fresh cookies usually clear
# the block); a whole sweep that resolves nothing aborts. A valid 200 with an
# empty records list is a terminal 'no_records' — cleanly distinct from a failure.
MAX_SWEEPS = 6
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]   # seconds before each requeue sweep
# Cadence tightened ~3.3x (was (2,5) + a 6% 10–20s spike), same proportion and
# approach as the v2 scripts (spike dropped).
PAIR_DELAY_RANGE = (0.5, 1.5)


def _scrape_pair(idx, pol_name, pod_name, row, creds, sweep_state, stats):
    """Query one quote (origin, LastCY) ONCE; write a wrapped raw file on success.

    Returns status:
      'completed'    -> schedules saved
      'no_records'   -> 200 but empty records (a real "nothing here" answer)
      'not_found'    -> POL or LastCY missing from cos_cities.json
      'pending'      -> TRANSIENT failure (403/429/5xx anti-bot / timeout / conn
                        error) — leave pending so the next sweep retries it
      'error_<code>' -> a non-transient 4xx (surfaced, not requeued)
    """
    key = f"{pol_name}__{pod_name}"
    origin_data = allcoscocities.get(pol_name)
    destination_data = allcoscocities.get(pod_name)
    if not origin_data or not destination_data:
        print(f"⚠️ Skipping: city not found for {key}")
        return "not_found"

    payload = {
        "fromDate": fromDate,
        "toDate": toDate,
        "pickup": "C",
        "delivery": "C",
        "estimateDate": "D",
        "originCityUuid": origin_data["cityUuid"],
        "destinationCityUuid": destination_data["cityUuid"],
        "originCity": origin_data["fullFormate"] + "," + origin_data["unloCode"],
        "destinationCity": destination_data["fullFormate"] + "," + destination_data["unloCode"],
        "cargoNature": "GC",
        "dataSource": "COSCO IRIS4",
    }

    for attempt in (1, 2):
        creds["headers"]["X-Client-Timestamp"] = str(int(time.time() * 1000))
        stats["calls"] += 1
        print(f"🔎 Fetching {key} ... (attempt {attempt})")
        try:
            resp = requests.post(
                url, headers=creds["headers"],
                cookies={c["name"]: c["value"] for c in creds["cookies"]},
                json=payload, timeout=20)
        except requests.RequestException as e:
            print(f"💥 {key}: {e} (transient → requeue)")
            return "pending"

        code = resp.status_code
        if code != 200:
            print(f"❌ Status {code} for {key}")
            if attempt == 1 and not sweep_state["rebooted"]:
                sweep_state["rebooted"] = True
                print("🔄 re-bootstrapping COSCO session...")
                c, h = get_new_session()
                creds["cookies"], creds["headers"] = c, h
                continue                              # retry same quote with fresh creds
            # blocked/throttled/server → requeue; other 4xx → surface
            return "pending" if code in (403, 429) or code >= 500 else f"error_{code}"

        data = resp.json().get("data", {})
        records = data.get("records") or data.get("content", {}).get("data", [])
        print(f"DEBUG: keys={list(data.keys())}, records={len(records)}")
        if not records:
            print(f"⚠️ No schedules found for {key}")
            return "no_records"

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
        out_file = PROCESSING_DIR / f"COS_{pol_short}_{pod_short}_{filename_timestamp}.json"
        with open(out_file, "w") as f:
            json.dump(wrapped_data, f, indent=2)
        quotes.at[idx, "LastCY"] = pod_name
        quotes.at[idx, "result_file"] = str(out_file)
        print(f"✅ Got {len(records)} total schedules for {key}")
        return "completed"

    return "pending"


def _log_run_stats(quotes, calls, elapsed):
    """Run summary to the log: totals, wall-clock, throughput. `elapsed` is the full
    scrape wall-clock (includes cooldowns + session re-bootstraps)."""
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
    print(f"  quotes          : {len(quotes)} total")
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
    if pending:
        print(f"⚠️ {pending} quote(s) still pending after {MAX_SWEEPS} sweeps (no raw file for them).")


def scrape_quotes(quotes, creds):
    """Multi-sweep drain of the pending quotes (the v2 retry model)."""
    stats = {"calls": 0}
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending quote(s) ---")
        sweep_state = {"rebooted": False}      # one session re-bootstrap allowed per sweep
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(idx, row["Port of Loading"], row["LastCY"], row, creds, sweep_state, stats)
            quotes.at[idx, "status"] = status
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

    _log_run_stats(quotes, stats["calls"], time.perf_counter() - t0)


# --- Start session (bootstrapped once; re-bootstrapped inside a sweep on non-200) ---
_c, _h = get_new_session()
creds = {"cookies": _c, "headers": _h}

try:
    scrape_quotes(quotes, creds)
    print("\n🎉 Quotes fetch complete!")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# --- Step A: forward-fill leg IDs in every raw JSON ---
for file in os.listdir(PROCESSING_DIR):
    if file.startswith("COS_") and file.endswith(".json"):
        full_path = os.path.join(PROCESSING_DIR, file)
        assign_ids_inplace(full_path)

# --- Step B: build CSV rows + canonical records in one pass ---
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if file.startswith("COS_") and file.endswith(".json"):
        full_path = os.path.join(PROCESSING_DIR, file)
        all_rows.extend(build_schedule_rows(full_path))
        rec = build_canonical_record(full_path)
        if rec is not None:
            all_canonical.append(rec)

# --- Log any ports that couldn't be resolved against portdbCanonical.json ---
unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"COS_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

try:
    # --- Write CSV
    df = pd.DataFrame(all_rows)
    for col in ["ETD", "ETA", "POD ETA"]:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    # Query Date now embeds the full UTC timestamp; format it as US date + 24-hour time.
    df["Query Date"] = (
        pd.to_datetime(df["Query Date"], errors="coerce", utc=True)
          .dt.strftime("%m/%d/%Y %H:%M:%S")
    )

    csv_out = get_unique_filename(CSV_DIR / f"COS_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False)
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per schedule), with rollback on failure
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

    # --- Both outputs succeeded → archive raw JSONs
    for file in os.listdir(PROCESSING_DIR):
        if file.startswith("COS_") and file.endswith(".json"):
            src = PROCESSING_DIR / file
            dst = get_unique_path(RAW_DIR / file)
            shutil.move(src, dst)
            print(f"📦 Moved {file} → {dst}")

    print("✅ All JSONs archived to RAW_DIR.")

    # --- Ingest newly written canonicals into Supabase (only new ones; ledger-tracked) ---
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals("COS")
    except Exception as e:
        # Ingestion is best-effort and crash-safe (re-runs retry un-ledgered files);
        # never let it sink an otherwise-successful scrape.
        print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")

except Exception as e:
    print(f"❌ Transform failed. JSONs kept in {PROCESSING_DIR}.")
    print("Error:", e)