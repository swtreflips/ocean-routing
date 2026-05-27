import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

from utils import (
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_ids_inplace,
    build_schedule_rows,
    build_canonical_record,
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
# Start session
cookies, headers = get_new_session()

try:
    for idx, row in quotes.iterrows():
        if row["status"] in ["completed", "no_data", "not_found"]:
            continue

        pol_name = row["Port of Loading"]
        pod_name = row["LastCY"]
        key = f"{pol_name}__{pod_name}"

        origin_data = allcoscocities.get(pol_name)
        destination_data = allcoscocities.get(pod_name)

        if not origin_data or not destination_data:
            print(f"⚠️ Skipping: city not found for {key}")
            quotes.at[idx, "status"] = "not_found"
            continue

        retried = False
        all_records = []

        while True:
            headers["X-Client-Timestamp"] = str(int(time.time() * 1000))
            print(f"🔎 Fetching {key} ... (retry={retried})")

            resp = requests.post(
                url,
                headers=headers,
                cookies={c["name"]: c["value"] for c in cookies},
                json={
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
                    "dataSource": "COSCO IRIS4"
                    # 👈 notice: no pageNum, no pageSize
                },
                timeout=20,
            )

            if resp.status_code != 200:
                print(f"❌ Status {resp.status_code} for {key}")
                if not retried:
                    cookies, headers = get_new_session()
                    retried = True
                    print("🔄 Retrying with new session...")
                    continue
                else:
                    quotes.at[idx, "status"] = f"error_{resp.status_code}"
                    break

            # Parse records (single batch, no paging)
            data = resp.json().get("data", {})
            records = data.get("records") or data.get("content", {}).get("data", [])

            print(f"DEBUG: keys={list(data.keys())}, records={len(records)}")

            if not records:
                print(f"⚠️ No schedules found for {key}")
                quotes.at[idx, "status"] = "no_records"
                break

            # ✅ Store all records directly
            all_records.extend(records)

            # Save once
            wrapped_data = {
                "query_date": query_timestamp,   # ISO 8601 UTC timestamp (see DATE.md)
                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),  # assigned snapshot
                "LastCY": pod_name,
                "OFQ": row.get("ID"),
                "FinalDestination": row.get("Final Destination"),
                "PortofLoading": pol_name,
                "schedules": all_records
            }

            # Build dynamic filename (timestamp keeps multi-run-per-day files unique)
            pol_short = pol_name.replace(" ", "")[:5]   # first 5 letters, no spaces
            pod_short = pod_name.replace(" ", "")[:5]   # first 5 letters, no spaces
            filename = f"COS_{pol_short}_{pod_short}_{filename_timestamp}.json"

            out_file = PROCESSING_DIR / filename
            with open(out_file, "w") as f:
                json.dump(wrapped_data, f, indent=2)

            quotes.at[idx, "status"] = "completed"
            quotes.at[idx, "LastCY"] = pod_name
            quotes.at[idx, "result_file"] = str(out_file)
            print(f"✅ Got {len(all_records)} total schedules for {key}")

            break  # ✅ no pagination loop

        sleep_time = random.uniform(2, 5)
        if random.random() < 0.06:
            sleep_time = random.uniform(10, 20)
        print(f"⏳ Sleeping {sleep_time:.1f}s...")
        time.sleep(sleep_time)

        print("\n🎉 Quotes fetch complete!")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# --- Step A: forward-fill leg IDs in every raw JSON ---
for file in os.listdir(PROCESSING_DIR):
    if file.endswith(".json"):
        full_path = os.path.join(PROCESSING_DIR, file)
        assign_ids_inplace(full_path)

# --- Step B: build CSV rows + canonical records in one pass ---
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if file.endswith(".json"):
        full_path = os.path.join(PROCESSING_DIR, file)
        all_rows.extend(build_schedule_rows(full_path))
        rec = build_canonical_record(full_path)
        if rec is not None:
            all_canonical.append(rec)

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
        if file.endswith(".json"):
            src = PROCESSING_DIR / file
            dst = get_unique_path(RAW_DIR / file)
            shutil.move(src, dst)
            print(f"📦 Moved {file} → {dst}")

    print("✅ All JSONs archived to RAW_DIR.")

except Exception as e:
    print(f"❌ Transform failed. JSONs kept in {PROCESSING_DIR}.")
    print("Error:", e)