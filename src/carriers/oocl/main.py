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
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from utils import (
    load_progress,
    geocode_city,
    resolve_missing_locations,
    build_voronoi_lookup,
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_oocl_code,
    build_schedule_dataframe,
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
LOG_DIR = PROJECT_ROOT / "src" / "data" / "oocl" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "oocl" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "oocl"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "oocl" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "oocl" / "canonical"

for d in (CSV_DIR, CANONICAL_DIR):
    d.mkdir(parents=True, exist_ok=True)
# --- Carrier-specific folder (cosco/) ---
CARRIER_DIR = Path(__file__).resolve().parent


# --- Run timestamp (UTC) — all dates/filenames for this run derive from this single moment ---
run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")                              # for assign_snapshot
today_str = today.strftime("%m.%d.%y")                              # for log/progress filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")      # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")      # Windows-safe (no ':')
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"oocl{today_str}.csv")



# Log set up
logfile = get_unique_filename(LOG_DIR / f"oocl_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "oocl_yards.geojson"

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

# === OOCL API CONFIGURATION ===
LANDING_URL = (
    "https://moc.oocl.com/nj_prs_wss/#/sailing_schedules/search"
    "?PREFER_LANGUAGE=en-US&originId={origin_id}&destinationId={destination_id}"
)
# Warmup uses any known valid pair (Cartagena -> Newark from ooclLoopflow defaults).
WARMUP_URL = LANDING_URL.format(
    origin_id=461796493418770, destination_id=461802935877065
)
API_MATCH = "searchHubToHubRoute"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

DELAY_RANGE = (3, 8)


def fetch_oocl_schedules(page, origin_id, destination_id):
    """
    Drive the OOCL sailing-schedules page for the given origin/destination
    locationids and intercept the searchHubToHubRoute response.

    Returns (status, schedules):
      status      one of "ok", "no_records", "error_timeout",
                  "error_navigation", or f"error_http_{n}"
      schedules   list of route dicts (empty unless status == "ok")
    """
    target = LANDING_URL.format(origin_id=origin_id, destination_id=destination_id)
    try:
        with page.expect_response(
            lambda r: API_MATCH in r.url and r.request.method == "POST",
            timeout=30_000,
        ) as resp_info:
            page.goto("about:blank")
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        response = resp_info.value
    except PlaywrightTimeoutError:
        print(f"  Timeout waiting for {API_MATCH} response")
        return "error_timeout", []
    except Exception as e:
        print(f"  Exception during OOCL fetch: {e}")
        return "error_navigation", []

    if response.status != 200:
        print(f"  {API_MATCH} returned {response.status}")
        return f"error_http_{response.status}", []

    try:
        body = response.json()
    except Exception as e:
        print(f"  Could not parse JSON body: {e}")
        return "error_navigation", []

    if not body.get("success", False):
        err = body.get("errorInfo") or body.get("errorInfoDTO") or "unknown"
        print(f"  API success=false ({err})")
        return "no_records", []

    data = body.get("data") or {}
    schedules = data.get("standardRoutes") or []
    return ("ok" if schedules else "no_records"), schedules


# === MAIN LOOP ===
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
    )
    page = context.new_page()

    print("Warming OOCL session...")
    page.goto(WARMUP_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    try:
        for idx, row in quotes.iterrows():
            if row["status"] != "pending":
                continue

            pol_name = row["Port of Loading"]
            pod_name = row["LastCY"]

            pol_code = get_oocl_code(pol_name)
            pod_code = get_oocl_code(pod_name)

            if not pol_code or not pod_code:
                quotes.at[idx, "status"] = "skipped_not_found"
                missing = []
                if not pol_code:
                    missing.append(f"POL '{pol_name}'")
                if not pod_code:
                    missing.append(f"POD '{pod_name}'")
                print(f"WARNING: No OOCL locationid for {', '.join(missing)} -- skipped")
                continue

            print(f"Processing: {pol_name} ({pol_code}) -> {pod_name} ({pod_code})")

            status, schedules = fetch_oocl_schedules(page, pol_code, pod_code)

            if status == "ok":
                wrapped = {
                    "query_date": query_timestamp,
                    "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                    "LastCY": pod_name,
                    "OFQ": row.get("ID"),
                    "FinalDestination": row.get("Final Destination"),
                    "PortOfLoading": pol_name,
                    "schedules": schedules,
                }

                pol_short = pol_name.replace(" ", "")[:5]
                pod_short = pod_name.replace(" ", "")[:5]
                filename = PROCESSING_DIR / f"oocl_{pol_short}_{pod_short}_{pol_code}_{pod_code}_{filename_timestamp}.json"
                filename = get_unique_path(filename)
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(wrapped, f, ensure_ascii=False, indent=2)

                quotes.at[idx, "result_file"] = str(filename)
                quotes.at[idx, "status"] = "done"
                print(f"  Saved schedules -> {filename}")
            elif status == "no_records":
                print(f"  No schedules for {pol_code} -> {pod_code}")
                quotes.at[idx, "status"] = "no_records"
            else:
                quotes.at[idx, "status"] = status

            time.sleep(random.uniform(*DELAY_RANGE))
    except (Exception, KeyboardInterrupt) as e:
        crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
        safe_to_csv(quotes, crash_file, index=False)
        print(f"💥 Run failed: {e}")
        print(f"📋 Partial progress saved to: {crash_file}")
        raise

    browser.close()

print("🏁 All done.")


# === Data Structuring: build CSV + canonical JSONs from raw oocl_*.json files ===
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
    if "Query Date" in df.columns:
        df["Query Date"] = pd.to_datetime(df["Query Date"], errors="coerce").dt.strftime("%m/%d/%Y %H:%M:%S")
    for col in ["ETD", "ETA", "POD ETA", "Cut-Off Date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    csv_out = get_unique_filename(CSV_DIR / f"OOCL_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"OOCL_{pol5}_{last5}_{filename_timestamp}.json"
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


