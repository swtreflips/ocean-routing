import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

import os
import sys
import json
import time
import random
import shutil
import pandas as pd
import geopandas as gpd
from pathlib import Path
from datetime import datetime, timezone
from geopy.geocoders import Nominatim
from patchright.sync_api import sync_playwright

from utils import (
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_hmm_code,
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


# --- Project root (Schedules/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# --- Shared folders ---
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "src" / "data" / "hmm" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "hmm" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "hmm"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "hmm" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "hmm" / "canonical"

for d in (CSV_DIR, CANONICAL_DIR):
    d.mkdir(parents=True, exist_ok=True)
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent


# --- Single UTC run timestamp; every file/field below derives from it ---
run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")                              # ISO date, fed to assign_snapshot
today_str = today.strftime("%m.%d.%y")                              # log filenames (e.g. 09.15.25)
today_api = today.strftime("%Y%m%d")                                # HMM API srchSailDate / paramToday
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")      # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")      # Windows-safe (no ':')
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"HMM{today_str}.csv")



# Log set up
logfile = get_unique_filename(LOG_DIR / f"HMM_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "hmm_yards.geojson"

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

quotes = quotes_progress

# Ensure tracking columns exist
if "LastCY" not in quotes.columns:
    quotes["LastCY"] = None
if "status" not in quotes.columns:
    quotes["status"] = "pending"
if "result_file" not in quotes.columns:
    quotes["result_file"] = None

quotes["result_file"] = quotes["result_file"].astype("string")

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

DELAY_RANGE = (1.5, 4)


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


# === MAIN LOOP ===
with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chrome",
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--disable-http2"]
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    # Warm up Chrome's network stack (cold-start can cause timeouts)
    print("Warming up browser...")
    page.goto("https://www.hmm21.com", wait_until="commit", timeout=60000)
    page.wait_for_timeout(2000)

    print("Establishing HMM session...")
    headers = establish_session(context, page)

    try:
        for idx, row in quotes.iterrows():
            if row["status"] != "pending":
                continue

            pol_name = row["Port of Loading"]
            pod_name = row["LastCY"]

            # --- Get HMM city codes (single code1 each) ---
            pol_code = get_hmm_code(pol_name)
            pod_code = get_hmm_code(pod_name)

            if not pol_code or not pod_code:
                quotes.at[idx, "status"] = "skipped_not_found"
                missing = []
                if not pol_code:
                    missing.append(f"POL '{pol_name}'")
                if not pod_code:
                    missing.append(f"POD '{pod_name}'")
                print(f"WARNING: No HMM code for {', '.join(missing)} -- skipped")
                continue

            print(f"Processing: {pol_name} ({pol_code}) -> {pod_name} ({pod_code})")

            success = False

            # --- Step 1: INIT call to get GrmNo ---
            payload_init = {
                "srchViewType": "L",
                "srchPointFromCd": pol_code,
                "srchCityFrom": "CY",
                "srchPointToCd": pod_code,
                "srchCityTo": "CY",
                "srchSailDate": today_api,
                "srchSelWeeks": "4",
                "srchSelPriority": "A",
                "srchSelSortBy": "D",
                "srchPorFcltyCd": "",
                "srchPvyFcltyCd": "",
                "itemPolCd": "",
                "itemPodCd": "",
                "paramToday": today_api
            }

            try:
                res1 = page.request.post(API_INIT, headers=headers, data=json.dumps(payload_init))

                # Retry once with session refresh on non-200
                if res1.status != 200:
                    print(f"  INIT returned {res1.status}, refreshing session and retrying...")
                    headers = establish_session(context, page)
                    res1 = page.request.post(API_INIT, headers=headers, data=json.dumps(payload_init))

                if res1.status != 200:
                    print(f"  INIT failed again ({res1.status}), skipping.")
                    quotes.at[idx, "status"] = "error_init"
                    time.sleep(random.uniform(*DELAY_RANGE))
                    continue

                data1 = res1.json()
                grm_no = data1["RTN_DATA"]["resultData"]["GrmNo"]
                print(f"  GrmNo: {grm_no}")

            except Exception as e:
                print(f"  Exception during INIT: {e}")
                quotes.at[idx, "status"] = "error_init"
                time.sleep(random.uniform(*DELAY_RANGE))
                continue

            # --- Step 2: RESULT call with GrmNo ---
            payload_result = {
                "srchViewType": "L",
                "srchGrmNo": grm_no,
                "grmSeqs": "",
                "srchSelPriority": "A",
                "srchSelSortBy": "D",
                "isNew": True
            }
            headers2 = {**headers, "accept": "application/json, text/javascript, */*; q=0.01"}

            try:
                res2 = page.request.post(API_RESULT, headers=headers2, data=json.dumps(payload_result))

                if res2.status != 200:
                    print(f"  RESULT returned {res2.status}, skipping.")
                    quotes.at[idx, "status"] = "error_result"
                    time.sleep(random.uniform(*DELAY_RANGE))
                    continue

                data2 = res2.json()

            except Exception as e:
                print(f"  Exception during RESULT: {e}")
                quotes.at[idx, "status"] = "error_result"
                time.sleep(random.uniform(*DELAY_RANGE))
                continue

            # --- Step 3: Check for schedule data and wrap ---
            schedules = data2.get("grmData")

            if not schedules:
                print(f"  No schedules for {pol_code} -> {pod_code}")
            else:
                success = True
                wrapped = {
                    "query_date": query_timestamp,
                    "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                    "LastCY": pod_name,
                    "OFQ": row.get("ID"),
                    "FinalDestination": row.get("Final Destination"),
                    "PortOfLoading": pol_name,
                    "schedules": schedules
                }

                # --- Dynamic filename ---
                pol_short = pol_name.replace(" ", "")[:5]
                pod_short = pod_name.replace(" ", "")[:5]

                filename = PROCESSING_DIR / f"HMM_{pol_short}_{pod_short}_{pol_code}_{pod_code}_{filename_timestamp}.json"
                filename = get_unique_path(filename)
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(wrapped, f, ensure_ascii=False, indent=2)

                quotes.at[idx, "result_file"] = str(filename)
                print(f"  Saved schedules -> {filename}")

            quotes.at[idx, "status"] = "done" if success else "no_records"

            time.sleep(random.uniform(*DELAY_RANGE))
    except (Exception, KeyboardInterrupt) as e:
        crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
        safe_to_csv(quotes, crash_file, index=False)
        print(f"💥 Run failed: {e}")
        print(f"📋 Partial progress saved to: {crash_file}")
        raise

    browser.close()

print("✅ All done.")


# =============================================================================
# Phase 2 — Data Structuring: JSON → CSV + archive
# =============================================================================

# === Data Structuring: build CSV + canonical JSONs from raw HMM_*.json files ===
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
    df = pd.DataFrame(all_rows)
    for col in ["ETD", "ETA", "POD ETA", "Cut-Off Date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    if "Query Date" in df.columns:
        df["Query Date"] = pd.to_datetime(df["Query Date"], errors="coerce").dt.strftime("%m/%d/%Y %H:%M:%S")

    csv_out = get_unique_filename(CSV_DIR / f"HMM_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"HMM_{pol5}_{last5}_{filename_timestamp}.json"
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
            shutil.move(str(src), str(dst))
            print(f"📦 Moved {file} → {dst}")

    print("✅ All JSONs archived to RAW_DIR.")

except Exception as e:
    print(f"❌ Transform failed. JSONs kept in {PROCESSING_DIR}.")
    print("Error:", e)

