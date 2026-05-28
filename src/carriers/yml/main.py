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
    get_locations,
    human_sleep,
    build_schedule_rows,
    build_canonical_record,
)

# --- Project root (Schedules/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# --- Shared folders ---
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "src" / "data" / "yml" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "yml" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "yml"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "yml" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "yml" / "canonical"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent

for d in (CSV_DIR, CANONICAL_DIR):
    d.mkdir(parents=True, exist_ok=True)


# --- Single UTC run timestamp; all dates/files/metadata derive from this ---
run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")           # assign_snapshot + CSV name
today_str = today.strftime("%m.%d.%y")           # log filenames, e.g. 09.15.25
today_api = today.strftime("%Y%m%d")             # YM API date param
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")  # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")  # Windows-safe

progress_file = get_unique_filename(LOG_DIR / f"YML{today_str}.csv")



# Log set up
logfile = get_unique_filename(LOG_DIR / f"YML_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "yml_yards.geojson"

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

# --- Step 5: Save progress ---
quotes_progress.to_csv(progress_file, index=False)

print("✅ Progress saved to:", progress_file)
print(quotes_progress[["ID", "Final Destination", "LastCY", "status"]])

# API Call Script


# --- Load most recent YML progress file if it exists ---
YML_files = [f for f in os.listdir(LOG_DIR) if f.startswith("YML") and f.endswith(".csv")]
if not YML_files:
    raise FileNotFoundError("❌ No YML progress file found in LOG_DIR!")

# sort by modified time (latest first)
YML_files = sorted(YML_files, key=lambda f: os.path.getmtime(LOG_DIR / f), reverse=True)
progress_file = LOG_DIR / YML_files[0]

print(f"📂 Using progress file: {progress_file}")
quotes = pd.read_csv(progress_file)

# Ensure tracking columns exist
if "LastCY" not in quotes.columns:
    quotes["LastCY"] = None
if "status" not in quotes.columns:
    quotes["status"] = "pending"
if "result_file" not in quotes.columns:
    quotes["result_file"] = None

quotes["result_file"] = quotes["result_file"].astype("string")

# API call


# api calls

import requests
import json
import time
import random
import pandas as pd

from pathlib import Path

# --- Dynamic dates (derived from run_timestamp captured at startup) ---
fromDate = today_api
toDate = (today + timedelta(days=14)).strftime("%Y%m%d")

snapshot_date = assign_snapshot(today_iso)

DELAY_RANGE = (2, 6)
MAX_RETRIES = 1

# --- API constants ---

# --- API constants ---
url = "https://www.yangming.com/api/P2P/GetP2PRoutes"
total = len(quotes)

# --- Minimal headers (no cookies, no tokens) ---
headers = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/143.0.0.0",
    "Referer": "https://www.yangming.com/en/esolution/schedule/point_to_point_search",
}

for idx, row in quotes.iterrows():
    from_city = row["Port of Loading"]
    to_city = row["LastCY"]

    from_codes = get_locations(from_city)  # always a list
    to_codes = get_locations(to_city)      # always a list

    if not from_codes or not to_codes:
        print(f"[{idx+1}/{total}] Skipping {from_city} → {to_city} (missing code)")
        quotes.at[idx, "status"] = "skipped_not_found"
        quotes.to_csv(progress_file, index=False)
        continue

    success = False

    # Loop through all combinations of POL / POD codes
    for from_code in from_codes:
        for to_code in to_codes:
            params = {
                "locationCodeFrom": from_code,
                "serviceTermFrom": "Y",
                "locationCodeTo": to_code,
                "serviceTermTo": "Y",
                "priorityWay": "ALL",
                "dateDefinition": "DEP",
                "startDate": fromDate,
                "endDate": toDate
            }

            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                print(f"📡 {from_city} ({from_code}) → {to_city} ({to_code}): {resp.status_code}")

                if resp.status_code == 400:
                    print(f"🛑 400 Bad Request for {from_code} → {to_code}")
                    continue

                if resp.status_code != 200:
                    print(f"❌ Error {resp.status_code} for {from_code} → {to_code}")
                    continue

                data = resp.json()  # YM returns a list

                if not data:
                    print(f"⚪ No schedules for {from_code} → {to_code}")
                    continue

                # ✅ Save each call separately
                wrapped = {
                    "query_date": query_timestamp,
                    "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                    "PortOfLoading": from_city,
                    "LastCY": to_city,
                    "OFQ": row.get("ID"),
                    "FinalDestination": row.get("Final Destination"),
                    "locationCodeFrom": from_code,
                    "locationCodeTo": to_code,
                    "schedules": data
                }

                filename = PROCESSING_DIR / f"YML_{from_code}_{to_code}_{filename_timestamp}.json"

                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(wrapped, f, ensure_ascii=False, indent=2)

                print(f"✅ Saved {len(data)} schedules → {filename}")
                success = True

            except Exception as e:
                print(f"💥 Exception for {from_code} → {to_code}: {e}")

            # Human-like sleep after each call
            sleep_time = human_sleep()
            print(f"⏳ Sleeping {sleep_time:.1f}s...")
            time.sleep(sleep_time)

    quotes.at[idx, "status"] = "done" if success else "no_records"
    quotes.to_csv(progress_file, index=False)


# Functions
def get_transport_type(route_details):
    vessel_legs = sum(
        1
        for leg in route_details
        if leg.get("transitMode") in {"VESSEL", "FEEDER"}
    )

    if vessel_legs <= 1:
        return "Direct"
    else:
        return f"{vessel_legs - 1} TS"


def get_port_of_discharge(route_details):
    ocean_legs = [
        leg for leg in route_details
        if leg.get("transitMode") in {"VESSEL", "FEEDER"}
    ]

    if not ocean_legs:
        return None

    return ocean_legs[-1].get("locationNameTo")

def get_pod_eta(route_details):
    ocean_legs = [
        leg for leg in route_details
        if leg.get("transitMode") in {"VESSEL", "FEEDER"}
    ]

    if not ocean_legs:
        return None

    return ocean_legs[-1].get("eta")

def get_ts_ports(route_details):
    ocean_legs = [
        leg for leg in route_details
        if leg.get("transitMode") in {"VESSEL", "FEEDER"}
    ]

    # Direct service → no TS ports
    if len(ocean_legs) <= 1:
        return ""

    # All ocean discharge ports except final POD
    ts_ports = [
        leg.get("locationNameTo")
        for leg in ocean_legs[:-1]
        if leg.get("locationNameTo")
    ]

    return " - ".join(ts_ports)


def get_mother_vessel(schedule):
    vessel = schedule.get("masterVesselName")
    voyage = schedule.get("masterComnVoyage")

    if vessel and voyage:
        return f"{vessel} / {voyage}"
    elif vessel:
        return vessel
    else:
        return None


def get_ts_vessels(route_details):
    ocean_legs = [
        leg for leg in route_details
        if leg.get("transitMode") in {"VESSEL", "FEEDER"}
    ]

    # Direct service → no TS vessel
    if len(ocean_legs) <= 1:
        return ""

    ts_vessels = []

    # Skip first ocean leg (mother vessel)
    for leg in ocean_legs[1:]:
        vessel = leg.get("vesselName")
        voyage = leg.get("comnVoyage")

        if vessel and voyage:
            ts_vessels.append(f"{vessel} / {voyage}")
        elif vessel:
            ts_vessels.append(vessel)

    return " - ".join(ts_vessels)



# === Data Structuring: build CSV + canonical JSONs from raw YML_*.json files ===
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("YML"):
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

    # Query Date keeps full timestamp granularity (e.g. 05/26/2026 14:35:22)
    if "Query Date" in df.columns:
        df["Query Date"] = pd.to_datetime(df["Query Date"], errors="coerce").dt.strftime("%m/%d/%Y %H:%M:%S")

    csv_out = get_unique_filename(CSV_DIR / f"YML_{filename_timestamp}.csv")
    df.to_csv(csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"YML_{pol5}_{last5}_{filename_timestamp}.json"
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

