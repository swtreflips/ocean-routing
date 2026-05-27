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
import undetected_chromedriver as uc
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
    get_locations,
    parse_locations,
    build_legs,
    parse_transports,
    extract_location,
    parse_summary_panel,
    extract_transport,
    extract_cutoffs,
    extract_request_metadata,
    is_location_li,
    process_html_file,
    transform_html_to_json,
    batch_transform_processing_dir,
    build_canonical_record,
)

def normalize_city_state(value):
    """Normalize 'boston, ma' -> 'Boston, MA'.
    Drops a trailing 2-letter country code ('norfolk, va, US' -> 'Norfolk, VA').
    """
    if not isinstance(value, str) or "," not in value:
        return value
    parts = [p.strip() for p in value.split(",")]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-1].isalpha():
        parts = parts[:-1]
    if len(parts) < 2:
        return value
    city = ", ".join(parts[:-1]).title()
    state = parts[-1].upper()
    return f"{city}, {state}"


def normalize_date(value):
    """Parse mixed CMA date formats and emit MM/DD/YYYY (zero-padded).
    Handles 'Tuesday, 16-JUN-2026', '06/23/2026', '01-MAY-2026, 09:00 PM'.
    Returns '' for empty/None; returns the original string if unparseable.
    """
    if not value:
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return value
    return parsed.strftime("%m/%d/%Y")


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
LOG_DIR = PROJECT_ROOT / "src" / "data" / "cma" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "cma" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "cma"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "cma" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "cma" / "canonical"
HTML_DIR = PROJECT_ROOT / "src" / "data" / "cma" / "html"
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent


run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")   # for assign_snapshot
today_str = today.strftime("%m.%d.%y")   # for log filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")   # Windows-safe

snapshot_date = assign_snapshot(today_iso)
progress_file = get_unique_filename(LOG_DIR / f"CMA{today_str}.csv")





# Log set up
logfile = get_unique_filename(LOG_DIR / f"CMA_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "cma_yards.geojson"

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



# Session generator


def create_new_cma_session(headless=False):
    import undetected_chromedriver as uc
    import requests
    import time

    options = uc.ChromeOptions()
    if headless:
        options.headless = True
        options.add_argument("--window-size=1920,1080")

    driver = uc.Chrome(options=options, version_main=148)

    try:
        driver.get("https://www.cma-cgm.com/")
        time.sleep(4)
        driver.get("https://www.cma-cgm.com/ebusiness/schedules/routing-finder")
        time.sleep(7)

        headers = driver.execute_script("""
            return {
                "user-agent": navigator.userAgent,
                "accept-language": navigator.language || "en-US"
            };
        """)
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "cache-control": "max-age=0",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.cma-cgm.com",
            "referer": "https://www.cma-cgm.com/ebusiness/schedules/routing-finder",
        })

        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}

        # --- create requests.Session() ---
        session = requests.Session()
        session.headers.update(headers)
        for k, v in cookies.items():
            session.cookies.set(k, v)

        return session  # ✅ return a Session, not tuple

    finally:
        driver.quit()

today = date.today()
fromDate = today.strftime("%d-%b-%Y")

session = create_new_cma_session()
cities_file = CARRIER_DIR / "assets" / "cma_cities.json"

CMA_URL = "https://www.cma-cgm.com/ebusiness/schedules/routing-finder"


DELAY_RANGE = (3, 8)
MAX_RETRIES = 1

# CMA's bot detection counts cumulative session requests, not just rate. After ~70
# calls on one session it returns 403s. Take a long break + recreate the session
# every CALLS_PER_COOLDOWN requests, and also when a 403 fires reactively.
CALLS_PER_COOLDOWN = 50
COOLDOWN_RANGE = (45, 75)  # seconds (randomized)

call_counter = 0


def take_cooldown(reason: str):
    """Long pause on the same session to test if cooldown alone dodges cumulative bot detection."""
    global call_counter
    cooldown = random.uniform(*COOLDOWN_RANGE)
    print(f"☕ Cooldown ({reason}): sleeping {cooldown:.1f}s on the same session...")
    time.sleep(cooldown)
    call_counter = 0
    print("✅ Cooldown complete, resuming.")


# === MAIN LOOP ===
try:
    for idx, row in quotes.iterrows():

        if row["status"] != "pending":
            continue

        pol_name = row["Port of Loading"]
        pod_name = row["LastCY"]
        print(f"Looking up POL: '{pol_name}' | POD: '{pod_name}'")
        pol_locations = get_locations(pol_name)
        pod_locations = get_locations(pod_name)
        print("POL locations:", pol_locations)
        print("POD locations:", pod_locations)

        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ Missing CMA locations for {pol_name} or {pod_name}")
            continue

        success = False

        # Loop through all POL/POD combinations
        for pol in pol_locations:
            for pod in pod_locations:

                payload = {
                    "ActualPOLDescription": pol["description"],
                    "ActualPODDescription": pod["description"],
                    "ActualPOLType": pol["type"],
                    "ActualPODType": pod["type"],

                    "polDescription": pol["description"],
                    "podDescription": pod["description"],

                    "IsDeparture": "True",
                    "SearchDate": fromDate,
                    "searchRange": "5",
                }

                # ✅ Conditional key
                if pod["type"].startswith("Ramp"):
                    payload["podType"] = "Ramp"

                print("Calling CMA with:")
                print(payload)
                try:
                    response = session.post(CMA_URL, data=payload, timeout=30)
                    call_counter += 1

                    if response.status_code == 200:
                        safe_pol = pol_name.replace(" ", "_").replace(",", "").lower()
                        safe_pod = pod["placeName"].replace(" ", "_").replace(",", "").lower()

                        out_file = (
                            PROCESSING_DIR /
                            f"cma_{idx}_{safe_pol}_{safe_pod}.html"
                        )

                        meta_comment = (
                            "<!-- "
                            f"POL={pol_name} | "
                            f"LastCY={pod['placeName']} | "
                            f"FinalDestination={row.get('Final Destination')} | "
                            f"OFQ={row.get('ID')} | "
                            f"snapshot_date={snapshot_date} | "
                            f"query_date={query_timestamp}"
                            " -->\n"
                        )

                        out_file.write_text(
                            meta_comment + response.text,
                            encoding="utf-8"
                        )

                        print(f"✅ CMA success: {pol_name} → {pod['placeName']}")
                        success = True
                    elif response.status_code == 403:
                        print(f"⚠️ CMA 403 — likely blocked")
                        take_cooldown("after 403")
                    else:
                        print(f"⚠️ CMA {response.status_code}")

                except Exception as e:
                    print(f"❌ CMA error: {e}")

                finally:
                    if call_counter > 0 and call_counter % CALLS_PER_COOLDOWN == 0:
                        take_cooldown(f"after {call_counter} calls")
                    else:
                        sleep_time = random.uniform(*DELAY_RANGE)
                        print(f"⏳ Sleeping {sleep_time:.2f}s")
                        time.sleep(sleep_time)

        if not success:
            quotes.at[idx, "status"] = "failed"

    print("🏁 All done.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# === AFTER CMA MAIN LOOP ===
batch_transform_processing_dir(PROCESSING_DIR, HTML_DIR)


def build_schedule_dataframe(processing_dir):
    """
    Reads all CMA_*.json files from processing_dir,
    extracts schedule data, and returns a combined DataFrame.
    """
    records = []

    for file in os.listdir(processing_dir):
        if not file.endswith(".json"):
            continue

        full_path = os.path.join(processing_dir, file)
        with open(full_path, "r", encoding="utf-8") as infile:
            data = json.load(infile)

        carrier = "CMA"

        # --- Wrapper-level fields ---
        request = data.get("request", {})
        port_of_loading = request.get("POL")
        last_cy = normalize_city_state(request.get("LastCY"))
        final_destination = request.get("FinalDestination")

        query_date_raw = request.get("query_date")
        snapshot_date_raw = request.get("snapshot_date")

        query_date = (
            pd.to_datetime(query_date_raw).strftime("%m/%d/%Y %H:%M:%S")
            if query_date_raw else ""
        )
        period = (
            pd.to_datetime(snapshot_date_raw).strftime("%m/%d/%Y")
            if snapshot_date_raw else ""
        )

        for schedule in data.get("schedules", []):
            legs = schedule.get("legs", [])
            summary = schedule.get("summary", {})
            cutoffs = schedule.get("cutoffs", {})

            # --- Maritime legs ---
            maritime_legs = [
                l for l in legs
                if l.get("transport", {}).get("mode") == "Maritime"
            ]
            maritime_count = len(maritime_legs)

            # --- Transport Type ---
            if maritime_count == 1:
                transport_type = "Direct"
            elif maritime_count > 1:
                transport_type = f"{maritime_count - 1} TS"
            else:
                transport_type = ""

            # --- Mother Vessel (first maritime leg) ---
            mother_vessel = (
                maritime_legs[0]["transport"].get("vessel")
                if maritime_count >= 1 else ""
            )

            # --- TS Vessel(s) ---
            ts_vessels = ""
            if maritime_count > 1:
                ts_vessels = "-".join(
                    l["transport"].get("vessel")
                    for l in maritime_legs[1:]
                    if l["transport"].get("vessel")
                )

            # --- TS Port(s) ---
            ts_ports = ""
            if maritime_count > 1:
                ts_ports = "-".join(
                    l["location"].get("city")
                    for l in maritime_legs[1:]
                    if l["location"].get("city")
                )

            # --- Last leg logic (POD + POD ETA) ---
            last_leg = legs[-1] if legs else None

            if last_leg and last_leg.get("transport", {}).get("mode") != "Maritime":
                port_of_discharge = normalize_city_state(last_leg.get("location", {}).get("city", ""))
                pod_eta = normalize_date(last_leg.get("location", {}).get("etd", ""))
            else:
                port_of_discharge = last_cy
                pod_eta = normalize_date(summary.get("eta", ""))

            # --- ETD & ETA ---
            etd = summary.get("etd", "")
            eta = summary.get("eta", "")

            # --- Cut-Off Date (Port) ---
            cut_off_date = normalize_date(cutoffs.get("Port", ""))

            # --- Build record ---
            record = {
                "Carrier": carrier,
                "Port of Loading": port_of_loading,
                "Port of Discharge": port_of_discharge,
                "Last CY": last_cy,
                "Final Destination": final_destination,
                "Query Date": query_date,
                "Period": period,
                "ETD": etd,
                "ETA": eta,
                "POD ETA": pod_eta,
                "Transit Time": summary.get("transit_days"),
                "Transport Type": transport_type,
                "TS Port(s)": ts_ports,
                "Mother Vessel": mother_vessel,
                "TS Vessel(s)": ts_vessels,
                "Cut-Off Date": cut_off_date,
            }

            records.append(record)

    # --- Consistent column order (same as HPL) ---
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


# --- Collect canonical records (CSV is built separately to preserve current format) ---
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("cma_"):
        continue
    full_path = os.path.join(PROCESSING_DIR, file)
    rec = build_canonical_record(full_path)
    if rec is not None:
        all_canonical.append(rec)

try:
    # --- STEP 1: CSV (unchanged format — same column order as HPL/MSK) ---
    df = build_schedule_dataframe(PROCESSING_DIR)
    output_file = get_unique_filename(CSV_DIR / f"CMA_{filename_timestamp}.csv")
    safe_to_csv(df, output_file, index=False, encoding="utf-8-sig")

    print(f"✅ Combined CSV created: {output_file}")
    print(df.head())

    # --- STEP 2: Canonical JSONs (one per query) with rollback on failure ---
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(CANONICAL_DIR / f"CMA_{pol5}_{last5}_{filename_timestamp}.json")
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

