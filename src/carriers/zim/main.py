import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

import os
import sys
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


# --- Project root (Schedules/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# --- Shared folders ---
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "src" / "data" / "zim" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "zim" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "zim"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "zim" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "zim" / "canonical"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent

for d in (CSV_DIR, CANONICAL_DIR):
    d.mkdir(parents=True, exist_ok=True)


run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")  # e.g. 09.15.25
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
progress_file = get_unique_filename(LOG_DIR / f"ZIM{today_str}.csv")



# Log set up
logfile = get_unique_filename(LOG_DIR / f"ZIM_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "zim_yards.geojson"

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
cities_file = CARRIER_DIR / "assets" / "zim_cities.json"

quotes = quotes_progress

# Ensure tracking columns exist
if "LastCY" not in quotes.columns:
    quotes["LastCY"] = None
if "status" not in quotes.columns:
    quotes["status"] = "pending"
if "result_file" not in quotes.columns:
    quotes["result_file"] = None

quotes["result_file"] = quotes["result_file"].astype("string")

# === Load zim cities JSON (groupable entries allowed) ===
cities_file = CARRIER_DIR / "assets" / "zim_cities.json"
if not cities_file.exists():
    raise FileNotFoundError(f"zim_cities.json not found at {cities_file}")

with open(cities_file, "r", encoding="utf-8") as f:
    zim_cities = json.load(f)


API_URL = "https://apigw.zim.com/digitalSchedules/PointToPoint/v1"
API_KEY = "9d63cf020a4c4708a7b0ebfe39578300"

FROM_DATE = today
DELAY_RANGE = (2, 4.5)
MAX_RETRIES = 1

# === LOAD CITIES ===
cities_file = CARRIER_DIR / "assets" / "zim_cities.json"
if not cities_file.exists():
    raise FileNotFoundError(f"zim_cities.json not found at {cities_file}")

with open(cities_file, "r", encoding="utf-8") as f:
    zim_cities = json.load(f)


API_URL = "https://apigw.zim.com/digitalSchedules/PointToPoint/v1"
API_KEY = "9d63cf020a4c4708a7b0ebfe39578300"

FROM_DATE = today  # Dynamic current date
DELAY_RANGE = (2, 4.5)
MAX_RETRIES = 1

# Format example: "23-October-2025"
FROM_DATE_STR = FROM_DATE.strftime("%d-%B-%Y")

print(f"📅 Running for date: {FROM_DATE_STR}")

# === LOAD CITIES ===
cities_file = CARRIER_DIR / "assets" / "zim_cities.json"
if not cities_file.exists():
    raise FileNotFoundError(f"zim_cities.json not found at {cities_file}")

with open(cities_file, "r", encoding="utf-8") as f:
    zim_cities = json.load(f)

# === HELPERS ===

def get_locations(port_name: str):
    """Return one or many port code entries."""
    entry = zim_cities.get(port_name)
    if not entry:
        return None
    return entry if isinstance(entry, list) else [entry]

def make_params(pol_code: str, pod_code: str):
    """Return query parameters for GET request."""
    return {
        "PortCode": f"{pol_code};10",
        "PortDestinationCode": f"{pod_code};10",
        "Direction": "true",
        "FromDate": FROM_DATE_STR,  # ✅ Fixed: required format
        "WeeksAhead": "4",
        "CountryCode": "US",
        "subscription-key": API_KEY
    }

def get_unique_filename(base_path: Path):
    """Append numeric suffix if file already exists."""
    if not base_path.exists():
        return base_path
    stem, suffix = base_path.stem, base_path.suffix
    i = 1
    while True:
        new_name = base_path.parent / f"{stem}_{i}{suffix}"
        if not new_name.exists():
            return new_name
        i += 1



# === MAIN LOOP ===
try:
    for idx, row in quotes.iterrows():
        if row.get("status", "pending") != "pending":
            continue

        pol_name = row.get("Port of Loading")
        pod_name = row.get("LastCY")

        pol_locations = get_locations(pol_name)
        pod_locations = get_locations(pod_name)

        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ Missing codes for {pol_name} or {pod_name}")
            continue

        success = False

        for pol in pol_locations:
            pol_code = pol.get("portCode", "").split(";")[0]
            for pod in pod_locations:
                pod_code = pod.get("portCode", "").split(";")[0]
                pod_name_proper = pod.get("shortPortName", pod_name)

                params = make_params(pol_code, pod_code)

                retried = False
                while True:
                    try:
                        headers = {
                            "accept": "application/json, text/plain, */*",
                            "accept-language": "en-US,en;q=0.9",
                            "cache-control": "no-cache",
                            "culture": "en-US",
                            "origin": "https://www.zim.com",
                            "pageid": "16439",
                            "pragma": "no-cache",
                            "referer": "https://www.zim.com/",
                            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                        }

                        resp = requests.get(API_URL, headers=headers, params=params, timeout=40)
                        print(f"📡 {pol_name} → {pod_name_proper}: {resp.status_code}")

                        if resp.status_code != 200:
                            if not retried and MAX_RETRIES > 0:
                                retried = True
                                print("🔄 Retrying once...")
                                time.sleep(random.uniform(5, 10))
                                continue
                            else:
                                print(f"❌ Error {resp.status_code} for {pol_name} → {pod_name_proper}")
                                break

                        try:
                            data = resp.json()
                        except json.JSONDecodeError:
                            print(f"⚠️ JSON parse error for {pol_name} → {pod_name_proper}")
                            raw_file = PROCESSING_DIR / f"ZIM_{pol_name}_{pod_name_proper}_raw.txt"
                            with open(raw_file, "w", encoding="utf-8") as f:
                                f.write(resp.text)
                            break

                        if not data.get("routes") and not data.get("midPoints"):
                            print(f"⚪ No schedules for {pol_name} → {pod_name_proper}")
                        else:
                            success = True

                            # === WRAP EVERYTHING ===
                            wrapped = {
                                "query_date": query_timestamp,
                                "PortOfLoading": pol_name,
                                "LastCY": pod_name_proper,
                                "OFQ": row.get("ID"),
                                "FinalDestination": row.get("Final Destination"),

                                # Rename: previously "routes", now full "schedules" payload
                                "schedules": data
                            }

                            # === SAVE FILE ===
                            pol_short = (pol_name or "").replace(" ", "")[:5]
                            pod_short = (pod_name or "").replace(" ", "")[:5]
                            filename = PROCESSING_DIR / f"ZIM_{pol_short}_{pod_short}_{filename_timestamp}.json"
                            with open(filename, "w", encoding="utf-8") as f:
                                json.dump(wrapped, f, ensure_ascii=False, indent=2)
                            print(f"✅ Saved schedules → {filename}")
                        if not data.get("routes") and not data.get("midPoints"):
                            print(f"⚪ No schedules for {pol_name} → {pod_name_proper}")
                        else:
                            success = True
                            # === Compute snapshot ===
                            snapshot_date = assign_snapshot(today_iso)
                            # === WRAP EVERYTHING ===
                            wrapped = {
                                "query_date": query_timestamp,
                                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),  # add here ✅
                                "PortOfLoading": pol_name,
                                "LastCY": pod_name_proper,
                                "OFQ": row.get("ID"),
                                "FinalDestination": row.get("Final Destination"),

                                # Rename: previously "routes", now full "schedules" payload
                                "schedules": data
                            }

                            # === SAVE FILE ===
                            pol_short = (pol_name or "").replace(" ", "")[:5]
                            pod_short = (pod_name or "").replace(" ", "")[:5]
                            filename = PROCESSING_DIR / f"ZIM_{pol_short}_{pod_short}_{filename_timestamp}.json"
                            with open(filename, "w", encoding="utf-8") as f:
                                json.dump(wrapped, f, ensure_ascii=False, indent=2)
                            print(f"✅ Saved schedules → {filename}")

                        break  # exit retry loop

                    except Exception as e:
                        print(f"💥 Exception for {pol_name} → {pod_name_proper}: {e}")
                        break

                time.sleep(random.uniform(*DELAY_RANGE))

        quotes.at[idx, "status"] = "done" if success else "no_records"

    print("🏁 All done.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise

# === Data Structuring: build CSV + canonical JSONs from raw ZIM_*.json files ===
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("ZIM"):
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
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")

    csv_out = get_unique_filename(CSV_DIR / f"ZIM_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"ZIM_{pol5}_{last5}_{filename_timestamp}.json"
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

