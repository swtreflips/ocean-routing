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
    build_canonical_record,
    normalize_pod,
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
LOG_DIR = PROJECT_ROOT / "src" / "data" / "hpl" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "hpl" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "hpl"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "hpl" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "hpl" / "canonical"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent


# --- Single UTC run timestamp; everything else derives from this moment ---
run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")                              # API + snapshot
today_str = today.strftime("%m.%d.%y")                              # log filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")      # ISO 8601 UTC ('Z')
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")      # Windows-safe

progress_file = get_unique_filename(LOG_DIR / f"HPL{today_str}.csv")



# Log set up
logfile = get_unique_filename(LOG_DIR / f"HPL_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "hpl_yards.geojson"

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

# API call


# api calls

import requests
import json
import time
import random
import pandas as pd

from pathlib import Path

# --- Dynamic dates (derived from the single run_timestamp captured at startup) ---
fromDate = today_iso
toDate = (today + timedelta(days=28)).strftime("%Y-%m-%d")
snapshot_date = assign_snapshot(today_iso)
DELAY_RANGE = (1, 3)
MAX_RETRIES = 1
# --- Load your onecities.json ---
cities_file = CARRIER_DIR / "assets" / "hpl_cities.json"

# --- API constants ---
url = "https://schedule.api.hlag.cloud/api/routes"

headers = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.hapag-lloyd.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "x-token": "public"
}


# === MAIN LOOP ===
try:
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue

        pol_name = row["Port of Loading"]
        pod_name = row["LastCY"]

        pol_locations = get_locations(pol_name)
        pod_locations = get_locations(pod_name)

        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ Missing codes for {pol_name} or {pod_name} — skipped")
            continue

        success = False

        # Loop through all POL/POD combinations
        for pol in pol_locations:
            pol_code = pol["code"]
            for pod in pod_locations:
                pod_code = pod["code"]
                pod_name_proper = pod["name"].title()  # "PORT EVERGLADES, FL" → "Port Everglades, FL"

                params = {
                    "startLocation": pol_code,
                    "endLocation": pod_code,
                    "startDate": fromDate,
                    "startHaulage": "MERCHANT",
                    "endHaulage": "MERCHANT",
                    "containerType": "42GP"
                }

                retried = False
                while True:
                    try:
                        resp = requests.get(url, headers=headers, params=params, timeout=40)
                        print(f"📡 {pol_name} ({pol_code}) → {pod_name_proper} ({pod_code}): {resp.status_code}")

                        if resp.status_code not in (200, 206):
                            if not retried and MAX_RETRIES > 0:
                                retried = True
                                print("🔄 Retrying once...")
                                time.sleep(random.uniform(5, 10))
                                continue
                            else:
                                print(f"❌ Error {resp.status_code} for {pol_code} → {pod_code}")
                                break

                        # ✅ Treat 200 and 206 as valid — attempt JSON parsing
                        try:
                            data = resp.json()
                        except json.JSONDecodeError:
                            print(f"⚠️ Could not parse JSON for {pol_code} → {pod_code}. Saving raw text.")
                            raw_file = PROCESSING_DIR / f"HLAG_{pol_code}_{pod_code}_raw.txt"
                            with open(raw_file, "w", encoding="utf-8") as f:
                                f.write(resp.text)
                            break

                        schedules = data.get("routes", [])

                        if resp.status_code == 206:
                            print(f"⚠️ Partial response (206) for {pol_code} → {pod_code}")

                        if not schedules:
                            print(f"⚪ No schedules for {pol_code} → {pod_code}")
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

                            base_name = f"HPL_{pol_code}_{pod_code}"
                            filename = PROCESSING_DIR / f"HPL_{pol_short}_{pod_short}_{pol_code}_{pod_code}_{filename_timestamp}.json"
                            with open(filename, "w", encoding="utf-8") as f:
                                json.dump(wrapped, f, ensure_ascii=False, indent=2)

                            print(f"✅ Saved {len(schedules)} schedules → {filename}")

                        break  # exit retry loop

                    except Exception as e:
                        print(f"💥 Exception for {pol_code} → {pod_code}: {e}")
                        break

                time.sleep(random.uniform(*DELAY_RANGE))

        quotes.at[idx, "status"] = "done" if success else "no_records"

    print("🏁 All done!")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise

# Data Structuring

def build_schedule_dataframe(processing_dir):
    """
    Reads all HPL_*.json files from processing_dir,
    extracts schedule data, and returns a combined DataFrame.
    """
    records = []

    for file in os.listdir(processing_dir):
        if not file.endswith(".json"):
            continue

        full_path = os.path.join(processing_dir, file)
        with open(full_path, "r", encoding="utf-8") as infile:
            data = json.load(infile)

        carrier = "HPL"
        port_of_loading = data.get("PortOfLoading")
        last_cy = data.get("LastCY")
        final_destination = data.get("FinalDestination")

        # Reformat query/snapshot dates
        query_date_raw = data.get("query_date")
        snapshot_date_raw = data.get("snapshot_date")
        query_date = (
            pd.to_datetime(query_date_raw).strftime("%m/%d/%Y %H:%M:%S")
            if query_date_raw else ""
        )
        snapshot_date = pd.to_datetime(snapshot_date_raw).strftime("%Y-%m-%d") if snapshot_date_raw else ""

        for schedule in data.get("schedules", []):
            vessel_legs = [leg for leg in schedule.get("legs", []) if leg.get("modeOfTransport") == "VESSEL"]
            vessel_count = len(vessel_legs)

            # --- Transport Type ---
            transport_type = "Direct" if vessel_count <= 1 else f"{vessel_count - 1} TS"

            # --- TS Port(s) ---
            if vessel_count <= 1:
                ts_ports = ""
            elif vessel_count == 2:
                ts_ports = vessel_legs[0]["arrivalLocation"]["locationName"]
            else:
                ts_ports = " - ".join(leg["arrivalLocation"]["locationName"] for leg in vessel_legs[:-1])

            # --- Mother Vessel ---
            mother_vessel = ""
            if vessel_legs:
                vessel_name = vessel_legs[0].get("vesselDetails", {}).get("name", "")
                voyage_number = str(vessel_legs[0].get("voyageNumber", ""))
                mother_vessel = f"{vessel_name} {voyage_number}".strip()

            # --- TS Vessel(s): all except the first vessel ---
            ts_vessels = ""
            if vessel_count > 1:
                ts_vessels_list = [
                    f"{leg.get('vesselDetails', {}).get('name', '')} {leg.get('voyageNumber', '')}".strip()
                    for leg in vessel_legs[1:]
                ]
                ts_vessels = " - ".join(ts_vessels_list)

            # --- Port of Discharge (last vessel leg arrivalLocation) ---
            port_of_discharge = normalize_pod(
                vessel_legs[-1].get("arrivalLocation", {}).get("locationName", "")
            ) if vessel_legs else ""

            # --- POD ETA (arrivalDateTime of last vessel leg) ---
            pod_eta_raw = vessel_legs[-1].get("arrivalDateTime") if vessel_legs else ""
            pod_eta = pd.to_datetime(pod_eta_raw).strftime("%Y-%m-%d") if pod_eta_raw else ""

            # --- ETD & ETA ---
            etd_raw = schedule.get("placeOfReceiptDateTime")
            eta_raw = schedule.get("placeOfDeliveryDateTime")
            etd = pd.to_datetime(etd_raw).strftime("%Y-%m-%d") if etd_raw else ""
            eta = pd.to_datetime(eta_raw).strftime("%Y-%m-%d") if eta_raw else ""

            # --- Cut-Off Date (DOC) ---
            cut_off_raw = ""
            for cutoff in schedule.get("gateInCutOffDateTimes", []):
                if cutoff.get("cutOffDateTimeCode") == "DOC":
                    cut_off_raw = cutoff.get("cutOffDateTime")
                    break
            cut_off_date = pd.to_datetime(cut_off_raw).strftime("%m/%d/%Y") if cut_off_raw else ""

            # --- Build Record ---
            record = {
                "Carrier": carrier,
                "Port of Loading": port_of_loading,
                "Port of Discharge": port_of_discharge,
                "Last CY": last_cy,
                "Final Destination": final_destination,
                "Query Date": query_date,
                "Period": snapshot_date,
                "ETD": etd,
                "ETA": eta,
                "POD ETA": pod_eta,
                "Transit Time": schedule.get("transitTimeInDays"),
                "Transport Type": transport_type,
                "TS Port(s)": ts_ports,
                "Mother Vessel": mother_vessel,
                "TS Vessel(s)": ts_vessels,
                "Cut-Off Date": cut_off_date,
            }
            records.append(record)

    # Define consistent column order
    cols = [
        "Carrier",
        "Port of Loading",
        "Port of Discharge",
        "Last CY",
        "Final Destination",
        "Query Date",
        "Period",
        "ETD",
        "ETA",
        "POD ETA",
        "Transit Time",
        "Transport Type",
        "TS Port(s)",
        "Mother Vessel",
        "TS Vessel(s)",
        "Cut-Off Date",
    ]

    return pd.DataFrame(records, columns=cols)


# --- Collect canonical records (CSV is built separately to preserve %m/%d/%Y) ---
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("HPL_"):
        continue
    full_path = os.path.join(PROCESSING_DIR, file)
    rec = build_canonical_record(full_path)
    if rec is not None:
        all_canonical.append(rec)

try:
    # --- STEP 1: CSV (Query Date now includes time — see DATE.md) ---
    df = build_schedule_dataframe(PROCESSING_DIR)
    output_file = get_unique_filename(CSV_DIR / f"HPL_{filename_timestamp}.csv")
    safe_to_csv(df, output_file, index=False, encoding="utf-8-sig")

    print(f"✅ Combined CSV created: {output_file}")
    print(df.head())

    # --- STEP 2: Canonical JSONs (one per query) with rollback on failure ---
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(CANONICAL_DIR / f"HPL_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
            written_canonical.append(out)
    except Exception:
        for p in written_canonical:
            p.unlink(missing_ok=True)
        raise

    print(f"✅ Wrote {len(written_canonical)} canonical JSON(s) → {CANONICAL_DIR}")

    # --- STEP 3: Both outputs succeeded → archive raw JSONs ---
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