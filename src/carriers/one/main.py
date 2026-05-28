import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

import json
import time
import random
import shutil
import requests
import pandas as pd
import geopandas as gpd
from pathlib import Path
from datetime import datetime, timedelta, timezone
from geopy.geocoders import Nominatim
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utils import (
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    build_schedule_rows,
    build_canonical_record,
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


# In notebooks, use the notebook's working directory as a stand-in for __file__
# --- Project root (Schedules/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# --- Shared folders ---
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "src" / "data" / "one" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "one" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "one"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "one" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "one" / "canonical"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent



run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")  # e.g. 09.15.25
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")

progress_file = get_unique_filename(LOG_DIR / f"ONE{today_str}.csv")



# Log set up
logfile = get_unique_filename(LOG_DIR / f"one_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "one_yards.geojson"


# --- Load data ---
quotes = pd.read_csv(quotes_file)
locations = pd.read_csv(locations_file)


gdf_voronoi = gpd.read_file(voronoi_file)


# --- Geocoder ---
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
cities_file = CARRIER_DIR / "assets" / "one_cities.json"

quotes = quotes_progress

# Ensure tracking columns exist
if "LastCY" not in quotes.columns:
    quotes["LastCY"] = None
if "status" not in quotes.columns:
    quotes["status"] = "pending"
if "result_file" not in quotes.columns:
    quotes["result_file"] = None

quotes["result_file"] = quotes["result_file"].astype("string")


# api calls

import requests
import json
import time
import random
import pandas as pd

from pathlib import Path

# --- Dynamic dates ---
fromDate = today_iso
toDate = (today + timedelta(days=14)).strftime("%Y-%m-%d")
snapshot_date = assign_snapshot(today_iso)
DELAY_RANGE = (2.5, 5)

# --- Load your one_cities.json ---
with open(CARRIER_DIR / "assets" / "one_cities.json", "r", encoding="utf-8") as f:
    allonecities = json.load(f)

# --- API constants ---
url = "https://ecomm.one-line.com/api/v1/schedule/point-to-point"

headers = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://ecomm.one-line.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

cookies = {
    "sessLocale": "en",
    "usrCntCd": "US",
    "AKA_A2": "A"
}

# --- Iterate over quotes ---
try:
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue  # skip finished rows

        pol_name = row["Port of Loading"]
        pod_name = row["LastCY"]

        # Look up codes
        pol_code = allonecities.get(pol_name, {}).get("code")
        pod_code = allonecities.get(pod_name, {}).get("code")

        if not pol_code or not pod_code:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ Missing code for {pol_name} or {pod_name} → skipped_not_found")
            continue

        params = {
            "porCode": pol_code,
            "delCode": pod_code,
            "rcvTermCode": "Y",
            "deTermCode": "Y",
            "tsFlag": "",
            "fromDate": fromDate,
            "toDate": toDate,
            "polCode": "",
            "podCode": "",
            "polYardCode": "",
            "podYardCode": "",
            "standardizationEtaEtb": "false",
            "cargoNature": "GP",
            "searchType": "List"
        }

        # --- Retry logic ---
        retried = False
        while True:
            try:
                resp = requests.get(url, headers=headers, params=params, cookies=cookies, timeout=60)
                print(f"📡 {pol_name} → {pod_name}: {resp.status_code}")

                if resp.status_code != 200:
                    if not retried:
                        retried = True
                        print("🔄 Retrying once...")
                        time.sleep(random.uniform(5, 10))  # wait before retry
                        continue
                    else:
                        quotes.at[idx, "status"] = f"error_{resp.status_code}"
                        print(f"❌ Error {resp.status_code} for {pol_name} → {pod_name}")
                else:
                    data = resp.json()
                    schedules = data.get("scheduleLines", [])

                    # choose whether to count scheduleLines or sailInfo legs
                    # count = len(schedules)   # counts lines
                    count = sum(len(line.get("sailInfo", [])) for line in schedules)  # counts legs

                    if count == 0:
                        quotes.at[idx, "status"] = "no_records"
                        print(f"⚠️ No schedules found for {pol_name} → {pod_name}")
                    else:
                        quotes.at[idx, "status"] = "done"

                        # --- Wrap data with metadata ---
                        wrapped_data = {
                            "query_date": query_timestamp,
                            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                            "LastCY": pod_name,
                            "OFQ": row.get("ID"),
                            "FinalDestination": row.get("Final Destination"),
                            "PortofLoading": pol_name,
                            "schedules": schedules
                        }

                        # --- Dynamic filename ---
                        pol_short = pol_name.replace(" ", "")[:5]
                        pod_short = pod_name.replace(" ", "")[:5]
                        filename = PROCESSING_DIR / f"ONE_{pol_short}_{pod_short}_{filename_timestamp}.json"

                        with open(filename, "w", encoding="utf-8") as f:
                            json.dump(wrapped_data, f, ensure_ascii=False, indent=2)

                        print(f"✅ Saved {count} schedules to {filename}")

                break  # exit retry loop after success or error

            except Exception as e:
                quotes.at[idx, "status"] = "error_exception"
                print(f"💥 Exception for {pol_name} → {pod_name}: {e}")
                break

        # --- Random wait between calls ---
        sleep_time = random.uniform(*DELAY_RANGE)
        print(f"⏳ Sleeping {sleep_time:.2f}s...\n")
        time.sleep(sleep_time)

    print("📊 All quotes processed.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# --- Transform: collect CSV rows + canonical records in one pass ---
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json"):
        continue
    full_path = os.path.join(PROCESSING_DIR, file)
    all_rows.extend(build_schedule_rows(full_path))
    rec = build_canonical_record(full_path)
    if rec is not None:
        all_canonical.append(rec)

try:
    # --- Write CSV
    df = pd.DataFrame(all_rows)
    for col in ["ETD", "ETA", "POD ETA"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    csv_out = get_unique_filename(CSV_DIR / f"ONE_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False)
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            fname = f"ONE_{pol5}_{last5}_{filename_timestamp}.json"
            out = get_unique_path(CANONICAL_DIR / fname)
            with open(out, "w", encoding="utf-8") as f:
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