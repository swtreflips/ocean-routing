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
from datetime import datetime, timezone
from geopy.geocoders import Nominatim
import re


from bs4 import BeautifulSoup
from requests.exceptions import Timeout, RequestException


from utils import (
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    batch_transform_processing_dir,
    build_schedule_dataframe,
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
LOG_DIR = PROJECT_ROOT / "src" / "data" / "emc" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "emc" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "emc"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "emc" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "emc" / "canonical"
HTML_DIR = PROJECT_ROOT / "src" / "data" / "emc" / "html"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent

run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")

snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"EMC{today_str}.csv")


# Log set up
logfile = get_unique_filename(LOG_DIR / f"EMC_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "emc_yards.geojson"

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
cities_file = CARRIER_DIR / "assets" / "emc_cities.json"

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
# =========================
# Evergreen helpers
# =========================

def save_schedule_html(
    html: str,
    row_id: str,
    pol: str,
    pod: str,
    meta_comment: str = ""
) -> str:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    filename = f"EMC_{row_id}_{pol}_{pod}_{ts}.html"
    filepath = PROCESSING_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        if meta_comment:
            f.write(meta_comment)
        f.write(html)

    return str(filepath)

def strip_brackets(text: str) -> str:
    return re.sub(r"\s*\[.*?\]", "", text).strip()


def build_evergreen_dates(start_date=None):
    if start_date is None:
        start_date = datetime.today()

    return {
        "departureMonth": start_date.strftime("%m"),
        "departureDay": start_date.strftime("%d"),
        "departureYear": start_date.strftime("%Y"),
        "departureDate": start_date.strftime("%Y%m%d"),
        "arrivalDate": start_date.strftime("%m%d"),                    # matches DevTools behavior
        "departureDateShow": start_date.strftime("%b-%d-%Y").upper(),  # JAN-22-2026
    }


def get_locations(city_name: str, cities_map: dict):
    """
    Evergreen version of get_locations() like your Hapag loop.

    emc_cities.json example value:
    "Miami, FL": [
      {...}, {...}
    ]

    We return a list of normalized dicts with keys:
    - code (unlocode)
    - name (display_name)
    """
    entries = cities_map.get(city_name)

    if not entries:
        return []

    locations = []
    for e in entries:
        unlocode = e.get("unlocode") or e.get("businessLocode")
        display_name = e.get("display_name") or e.get("businessLocationName")

        if not unlocode or not display_name:
            continue

        locations.append({
            "code": unlocode,
            "name": display_name,
        })

    return locations


def get_locationss(city_name: str, cities_map: dict):
    entries = cities_map.get(city_name)
    if not entries:
        return []

    locations = []
    for e in entries:
        locations.append({
            "code": e.get("unlocode") or e.get("businessLocode"),
            "name": e.get("display_name") or e.get("businessLocationName"),
            "short_name": e.get("short_name"),
        })

    return locations


# Optional (you said ignore count_schedules, so leaving it out)
def count_schedules(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    schedules = [
        thead
        for thead in soup.select("thead.Corner")
        if thead.select_one("td.ec-text-center")
    ]
    return len(schedules)


def is_no_results_page(html: str) -> bool:
    """EMC prints 'Data not found.' in the body when a query legitimately has
    zero schedules. Use this to distinguish a confirmed-empty response from a
    server stall so we don't burn retries on queries the website has already
    answered."""
    return "Data not found." in html
# =========================
# Config
# =========================
url = "https://ss.shipmentlink.com/tvs2/jsp/TVS2_InteractiveScheduleRouting.jsp"

headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0"
}

cookies = {}

REQUEST_TIMEOUT = 10
MAX_ATTEMPTS = 8
RETRY_DELAY = 20
DELAY_RANGE = (0, 4)   # sleep between requests if success (like you had)


# =========================
# Load city mappings
# =========================
with open(CARRIER_DIR / "assets" / "emc_cities.json", "r", encoding="utf-8") as f:
    cities_evm = json.load(f)



# =========================
# MAIN LOOP (Hapag philosophy)
# =========================
try:
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue

        # change these column names to match your file
        pol_name = row["Port of Loading"]
        pod_name = row["LastCY"]

        pol_locations = get_locationss(pol_name, cities_evm)
        pod_locations = get_locationss(pod_name, cities_evm)

        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            quotes.at[idx, "results"] = -1
            print(f"⚠️ Missing mapping: {pol_name} → {pod_name}")
            continue

        success = False
        attempted_pairs = 0
        success_pairs = 0

        # Loop through all POL/POD combinations (like Hapag)
        for pol in pol_locations:
            for pod in pod_locations:
                attempted_pairs += 1

                oriLocation = pol["code"]
                oriLocationName = pol["name"]

                desLocation = pod["code"]
                desLocationName = pod["name"]
                lastcy_clean = pod_name
                date_fields = build_evergreen_dates()  # ✅ inside loop

                payload = {
                    "oriCountry": "",
                    "groupRadioOri": "ALL",
                    "desCountry": "",
                    "groupRadioDes": "ALL",

                    **date_fields,

                    "arrivalMonth": "",
                    "arrivalDay": "",
                    "arrivalYear": "",
                    "durationWeek": "14",
                    "reeferCargo": "N",

                    "oriLocation": oriLocation,
                    "oriLocationName": oriLocationName,
                    "desLocation": desLocation,
                    "desLocationName": desLocationName,

                    "carrier": "V",
                    "serviceMode": "",
                    "isReefer": "N",
                    "func": "getSearchResult",

                    "oriUSCA": "",
                    "desUSCA": "",
                    "oriEastWest": "",
                    "desEastWest": "ALL",
                    "oriUseMode": "I",
                    "desUseMode": "I",
                }

                pair_success = False

                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        print(f"🚢 {pol_name}({oriLocation}) → {pod_name}({desLocation}) | Attempt {attempt}")

                        resp = requests.post(
                            url,
                            data=payload,
                            headers=headers,
                            cookies=cookies,
                            timeout=REQUEST_TIMEOUT
                        )

                        resp.raise_for_status()

                        schedule_count = count_schedules(resp.text)

                        if schedule_count > 0:
                            meta_comment = (
                                    "<!-- "
                                    f"carrier=EVERGREEN | "
                                    f"POL={pol_name} | "
                                    f"LastCY={lastcy_clean} | "
                                    f"OFQ={row.get('ID')} | "
                                    f"snapshot_date={snapshot_date} | "
                                    f"query_date={query_timestamp} | "
                                    " -->\n"
                                )
                            saved_path = save_schedule_html(
                                resp.text,
                                row["ID"],
                                oriLocation,
                                desLocation,
                                meta_comment=meta_comment
                            )

                            existing = quotes.at[idx, "result_file"]
                            paths = [] if pd.isna(existing) else str(existing).split(";")
                            paths.append(saved_path)
                            quotes.at[idx, "result_file"] = ";".join(paths)

                            quotes.at[idx, "results"] = schedule_count

                            print(f"✅ Saved {schedule_count} schedules → {saved_path}")

                            pair_success = True
                            success = True
                            success_pairs += 1
                            break
                        elif is_no_results_page(resp.text):
                            print("ℹ️ No schedules found (confirmed empty, not a stall) — skipping retries")
                            break
                        else:
                            print("⚠️ Empty response without 'Data not found.' marker — likely a stall, will retry")

                    except Timeout:
                        print(f"⏱️ Timeout after {REQUEST_TIMEOUT}s")

                    except RequestException as e:
                        print(f"❌ Request failed: {e}")

                    if attempt < MAX_ATTEMPTS:
                        print(f"⏸️ Waiting {RETRY_DELAY}s before retry...")
                        time.sleep(RETRY_DELAY)

                # Sleep between combos (like Hapag)
                time.sleep(random.uniform(*DELAY_RANGE))

        # Update row summary (Hapag-style)
        quotes.at[idx, "attempted_pairs"] = attempted_pairs
        quotes.at[idx, "success_pairs"] = success_pairs
        quotes.at[idx, "status"] = "done" if success else "no_records"

    print("🏁 All done.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# === AFTER MAIN LOOP ===
batch_transform_processing_dir(PROCESSING_DIR, HTML_DIR)

# --- Collect canonical records (CSV is built separately to preserve current format) ---
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("EMC_"):
        continue
    full_path = os.path.join(PROCESSING_DIR, file)
    rec = build_canonical_record(full_path)
    if rec is not None:
        all_canonical.append(rec)

try:
    # --- STEP 1: CSV (unchanged format — same column order as HPL/MSK/CMA) ---
    df = build_schedule_dataframe(PROCESSING_DIR)
    output_file = get_unique_filename(CSV_DIR / f"EMC_{filename_timestamp}.csv")
    safe_to_csv(df, output_file, index=False, encoding="utf-8-sig")

    print(f"✅ Combined CSV created: {output_file}")
    print(df.head())

    # --- STEP 2: Canonical JSONs (one per query) with rollback on failure ---
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(CANONICAL_DIR / f"EMC_{pol5}_{last5}_{filename_timestamp}.json")
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