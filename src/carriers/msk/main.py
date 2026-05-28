#!/usr/bin/env python3
import os
import sys
# GDAL env (keep as you had it)
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

import json
import time
import random
import shutil
import requests
import pandas as pd
import geopandas as gpd
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from geopy.geocoders import Nominatim

from utils import (
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_locations as util_get_locations,  # kept in case you use utils' version elsewhere
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


# === PROJECT PATHS ===
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "src" / "data" / "msk" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "msk" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "msk"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "msk" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "msk" / "canonical"
CARRIER_DIR = Path(__file__).resolve().parent  # src/carriers/msk

# ensure dirs exist
for d in (DATA_DIR, LOG_DIR, RAW_DIR, PROCESSING_DIR, TABLES_DIR, CSV_DIR, CANONICAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

# === TIMESTAMP / NAMES ===
run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")  # for log/progress filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")   # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")    # Windows-safe
progress_file_initial = get_unique_filename(LOG_DIR / f"MSK{today_str}.csv")

# === Logging (redirect stdout/stderr to log file) ===
logfile = get_unique_filename(LOG_DIR / f"MSK_run_{today_str}.log")
# ensure log directory exists (already done above), then redirect
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# === Shared input files ===
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# sanity check: required input files
if not quotes_file.exists():
    raise FileNotFoundError(f"quotes.csv not found at {quotes_file}")
if not locations_file.exists():
    raise FileNotFoundError(f"locations.csv not found at {locations_file}")

# === Load static inputs ===
locations = pd.read_csv(locations_file)

# carrier-specific files
voronoi_file = CARRIER_DIR / "assets" / "msk_yards.geojson"
if not voronoi_file.exists():
    raise FileNotFoundError(f"Voronoi file not found: {voronoi_file}")

gdf_voronoi = gpd.read_file(voronoi_file)

# geocoder
geolocator = Nominatim(user_agent="voronoi_lookup")

# === Step 1: Load or initialize progress ===
# load_progress should create the initial progress_file if none exists
quotes_progress = load_progress(quotes_file, progress_file_initial)

# === Step 2: Resolve missing destinations ===
locations = resolve_missing_locations(quotes_progress, locations, locations_file, geocode_city, geolocator)

# === Step 3: Build Voronoi lookup ===
lookup = build_voronoi_lookup(quotes_progress, locations, gdf_voronoi)

# === Step 4: Fill LastCY only if missing ===
quotes_progress["LastCY"] = quotes_progress.apply(
    lambda row: row["LastCY"] if pd.notnull(row["LastCY"]) else lookup.get(row["Final Destination"]),
    axis=1
)

# ensure tracking columns exist on quotes_progress before saving
for col, default in {"LastCY": None, "status": "pending", "result_file": None}.items():
    if col not in quotes_progress.columns:
        quotes_progress[col] = default
quotes_progress["result_file"] = quotes_progress["result_file"].astype("string")

# === Step 5: Hand off to API loop (in-memory; no intermediate disk write) ===
print("✅ Geocoding complete.")
print(quotes_progress[["ID", "Final Destination", "LastCY", "status"]])

quotes = quotes_progress

# === Load maersk cities JSON (groupable entries allowed) ===
cities_file = CARRIER_DIR / "assets" / "msk_cities.json"
if not cities_file.exists():
    raise FileNotFoundError(f"msk_cities.json not found at {cities_file}")

with open(cities_file, "r", encoding="utf-8") as f:
    maersk_cities = json.load(f)

# === API constants / timing ===
DELAY_RANGE = (1, 3)
MAX_RETRIES = 1
EARLIEST = today_iso
LATEST = (today + timedelta(days=29)).strftime("%Y-%m-%d")

url = "https://api.maersk.com/routing-unified/routing/routings-queries"
headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "api-version": "1",
    "consumer-key": "uXe7bxTHLY0yY0e8jnS6kotShkLuAAqG",
}

# helper to fetch one-or-many city entries
def get_locations(city_name: str):
    entry = maersk_cities.get(city_name)
    if not entry:
        return None
    return entry if isinstance(entry, list) else [entry]

def make_payload(pol_geo_id: str, pod_geo_id: str):
    return {
        "requestType": "DATED_SCHEDULES",
        "includeFutureSchedules": True,
        "routingCondition": "PREFERRED",
        "exportServiceType": "CY",
        "importServiceType": "CY",
        "brandCode": "MSL",
        "startLocation": {
            "dataObject": "CITY",
            "alternativeCodes": [
                {"alternativeCodeType": "GEO_ID", "alternativeCode": pol_geo_id}
            ],
            "cityCode": ""
        },
        "endLocation": {
            "dataObject": "CITY",
            "alternativeCodes": [
                {"alternativeCodeType": "GEO_ID", "alternativeCode": pod_geo_id}
            ],
            "cityCode": ""
        },
        "timeRange": {
            "routingsBasedOn": "DEPARTURE_DATE",
            "earliestTime": EARLIEST,
            "latestTime": LATEST
        },
        "cargo": {
            "cargoType": "DRY",
            "isTemperatureControlRequired": False
        },
        "carriage": {"vessel": {"flagCountryCode": ""}},
        "equipment": {
            "equipmentSizeCode": "40",
            "equipmentTypeCode": "HDRY",
            "constructionMaterial": "",
            "isEmpty": False,
            "isShipperOwned": False
        },
        "IsUseOfInternetMarkedRoutesOnly": False
    }

# === MAIN LOOP ===
try:
    for idx, row in quotes.iterrows():
        # Skip if not pending
        if row.get("status", "pending") != "pending":
            continue

        pol_name = row.get("Port of Loading")
        pod_name = row.get("LastCY")

        pol_locations = get_locations(pol_name)
        pod_locations = get_locations(pod_name)

        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ Missing codes for {pol_name} or {pod_name} — skipped")
            continue

        success = False

        for pol in pol_locations:
            pol_geo_id = pol.get("maerskGeoLocationId")
            for pod in pod_locations:
                pod_geo_id = pod.get("maerskGeoLocationId")
                pod_name_proper = pod.get("localityName", pod_name)

                payload = make_payload(pol_geo_id, pod_geo_id)

                retried = False
                while True:
                    try:
                        resp = requests.post(url, headers=headers, json=payload, timeout=40)
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
                            print(f"⚠️ Could not parse JSON for {pol_name} → {pod_name_proper}. Saving raw text.")
                            raw_file = PROCESSING_DIR / f"MAERSK_{pol_name}_{pod_name_proper}_raw.txt"
                            with open(raw_file, "w", encoding="utf-8") as f:
                                f.write(resp.text)
                            break

                        routings = data.get("routings", [])

                        if not routings:
                            print(f"⚪ No routings for {pol_name} → {pod_name_proper}")
                        else:
                            success = True
                            snapshot_date = assign_snapshot(today_iso)
                            wrapped = {
                                "query_date": query_timestamp,
                                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                                "PortOfLoading": pol_name,
                                "LastCY": pod_name_proper,
                                "OFQ": row.get("ID"),
                                "FinalDestination": row.get("Final Destination"),
                                "routings": routings
                            }

                            pol_short = (pol_name or "").replace(" ", "")[:5]
                            pod_short = (pod_name or "").replace(" ", "")[:5]
                            filename = PROCESSING_DIR / f"MAE_{pol_short}_{pod_short}_{filename_timestamp}.json"
                            with open(filename, "w", encoding="utf-8") as f:
                                json.dump(wrapped, f, ensure_ascii=False, indent=2)

                            print(f"✅ Saved {len(routings)} routings → {filename}")

                        break  # exit retry loop

                    except Exception as e:
                        print(f"💥 Exception for {pol_name} → {pod_name_proper}: {e}")
                        break

                time.sleep(random.uniform(*DELAY_RANGE))

        quotes.at[idx, "status"] = "done" if success else "no_records"

    print("🏁 All done.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file_initial.with_stem(progress_file_initial.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise

# checking location codes on json raw responses
# Folder containing your MAE*.json files
input_folder = PROCESSING_DIR
output_json = CARRIER_DIR / "city_codes.json"

def extract_codes(obj, found_codes):
    """Recursively find all alternativeCode values in the object."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "alternativeCode":
                found_codes.add(value)
            else:
                extract_codes(value, found_codes)
    elif isinstance(obj, list):
        for item in obj:
            extract_codes(item, found_codes)

# --- Step 1: Load existing codes (if file exists)
if os.path.exists(output_json):
    with open(output_json, "r", encoding="utf-8") as f:
        existing_codes = json.load(f)
    print(f"📂 Loaded {len(existing_codes)} existing codes from {output_json}")
else:
    existing_codes = {}
    print("🆕 No existing codes file found — creating new one")

# --- Step 2: Extract new codes from MAE*.json files
new_codes = set()

for file in os.listdir(input_folder):
    if file.startswith("MAE") and file.endswith(".json"):
        file_path = os.path.join(input_folder, file)
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                extract_codes(data, new_codes)
                print(f"✅ Extracted from {file} ({len(new_codes)} found so far)")
            except Exception as e:
                print(f"❌ Error in {file}: {e}")

# --- Step 3: Add only new codes
added = 0
for code in new_codes:
    if code not in existing_codes:
        existing_codes[code] = None
        added += 1

# --- Step 4: Save back to JSON
with open(output_json, "w", encoding="utf-8") as f:
    json.dump(existing_codes, f, ensure_ascii=False, indent=2)

print(f"\n📦 Added {added} new codes ({len(existing_codes)} total) → {output_json}")



# Make calls for missing cities in msk_cities.json
# --- Config ---
consumer_key = "uXe7bxTHLY0yY0e8jnS6kotShkLuAAqG"
base_url = "https://api.maersk.com/synergy/reference-data/geography/locations/"
output_json = CARRIER_DIR / "city_codes.json"
delay = 0.5  # seconds between calls

# --- Headers for the API ---
headers = {
    "Consumer-Key": consumer_key,
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Referer": "https://www.maersk.com/",
    "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# --- Load JSON file ---
if not output_json.exists():
    raise FileNotFoundError(f"{output_json} not found")

with output_json.open("r", encoding="utf-8") as f:
    codes_dict = json.load(f)

# --- Filter only codes with null values ---
pending_codes = [code for code, value in codes_dict.items() if value is None]
print(f"📋 {len(pending_codes)} codes pending API lookup")

# --- Atomic write helper (safe for Path objects) ---
def atomic_write_json(data: dict, path: Path):
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp_path), str(path))  # atomic swap

# --- Make API calls ---
for i, code in enumerate(pending_codes, start=1):
    url = base_url + code
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            city_name = data.get("cityName")
            codes_dict[code] = city_name
            print(f"[{i}/{len(pending_codes)}] ✅ {code} → {city_name}")
        else:
            codes_dict[code] = None
            print(f"[{i}/{len(pending_codes)}] ⚠️ {code} (HTTP {response.status_code})")
    except Exception as e:
        codes_dict[code] = None
        print(f"[{i}/{len(pending_codes)}] ❌ {code}: {e}")

    # Save progress atomically after each call
    try:
        atomic_write_json(codes_dict, output_json)
    except Exception as e:
        print(f"❌ Failed to write atomically: {e}")
        # Optional: break if you prefer to stop on write failure

    time.sleep(delay)

print(f"\n✅ Done — updated {output_json} with {len(pending_codes)} processed codes")

# mapping cities into raw json responses
# Load mapping
with output_json.open("r", encoding="utf-8") as f:
    code_to_city = json.load(f)
print(f"✅ Loaded {len(code_to_city)} location mappings.")

def replace_codes(obj):
    """Recursively walk through any JSON structure and replace matching alternativeCodes."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "alternativeCode" and v in code_to_city:
                obj[k] = code_to_city[v]
            else:
                replace_codes(v)
    elif isinstance(obj, list):
        for item in obj:
            replace_codes(item)

# --- Process all MAE*.json files using os.listdir ---
for filename in os.listdir(input_folder):
    if filename.startswith("MAE") and filename.endswith(".json"):
        file_path = input_folder / filename
        print(f"🛠️ Processing {filename}...")

        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"❌ Skipping {filename} (error: {e})")
            continue

        before = json.dumps(data, sort_keys=True)
        replace_codes(data)
        after = json.dumps(data, sort_keys=True)

        if before != after:
            with file_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ Updated {filename}")
        else:
            print(f"ℹ️ No matching codes found in {filename}")

print("🎉 Done! All MAE JSON files updated.")

# === Data Structuring: build CSV + canonical JSONs from raw MAE_*.json files ===
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("MAE"):
        continue
    full_path = os.path.join(PROCESSING_DIR, file)
    all_rows.extend(build_schedule_rows(full_path))
    rec = build_canonical_record(full_path)
    if rec is not None:
        all_canonical.append(rec)

try:
    df = pd.DataFrame(all_rows)
    for col in ["ETD", "ETA", "POD ETA"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    if "Query Date" in df.columns:
        df["Query Date"] = pd.to_datetime(df["Query Date"], errors="coerce").dt.strftime("%m/%d/%Y %H:%M:%S")

    csv_out = get_unique_filename(CSV_DIR / f"MSK_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"MSK_{pol5}_{last5}_{filename_timestamp}.json"
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

