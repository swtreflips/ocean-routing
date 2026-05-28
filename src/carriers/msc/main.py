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
LOG_DIR = PROJECT_ROOT / "src" / "data" / "msc" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "msc" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "msc"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "msc" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "msc" / "canonical"
CARRIER_DIR = Path(__file__).resolve().parent  # src/carriers/msc

# ensure dirs exist
for d in (DATA_DIR, LOG_DIR, RAW_DIR, PROCESSING_DIR, TABLES_DIR, CSV_DIR, CANONICAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

# === TIMESTAMP / NAMES ===
# One UTC moment per run — every filename, query_date, and snapshot derives from it.
run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")                              # for assign_snapshot
today_str = today.strftime("%m.%d.%y")                              # for log/progress filenames
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")      # ISO 8601 UTC ('Z' = Zulu)
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")      # NTFS-safe (no ':')


# === Logging (redirect stdout/stderr to log file) ===
logfile = get_unique_filename(LOG_DIR / f"MSC_run_{today_str}.log")
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
voronoi_file = CARRIER_DIR / "assets" / "msc_yards.geojson"
if not voronoi_file.exists():
    raise FileNotFoundError(f"Voronoi file not found: {voronoi_file}")

gdf_voronoi = gpd.read_file(voronoi_file)

# geocoder
geolocator = Nominatim(user_agent="voronoi_lookup")

# === Step 1: Load or initialize progress ===
# load_progress should create the initial progress_file if none exists
progress_file = LOG_DIR / f"MSC_{today_str}.csv"
quotes_progress = load_progress(quotes_file, progress_file)

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


print("✅ Geocoding complete.")
print(quotes_progress[["ID", "Final Destination", "LastCY", "status"]])


# === Load msc cities JSON (groupable entries allowed) ===
cities_file = CARRIER_DIR / "assets" / "msc_cities.json"
if not cities_file.exists():
    raise FileNotFoundError(f"msc_cities.json not found at {cities_file}")

with open(cities_file, "r", encoding="utf-8") as f:
    msc_cities = json.load(f)

# === CONFIG ===
DELAY_RANGE = (1, 2.5)
MAX_RETRIES = 2
# MSC's UI rejects same-day departures ("date cannot be in the future"), so the
# API payload uses tomorrow's date. query_date / snapshot_date / filename stay
# anchored to the run's UTC moment.
EARLIEST = today_iso
FROM_DATE = (today + timedelta(days=1)).strftime("%Y-%m-%d")

MSC_URL = "https://www.msc.com/api/feature/tools/SearchSailingRoutes"
MSC_SEARCH_PAGE = "https://www.msc.com/en/search-a-schedule"

# Match current Chrome 148 fingerprint as closely as possible. MSC's Akamai layer
# rejects requests missing the sec-fetch / sec-ch-ua / accept-language headers.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
_BROWSER_HEADERS = {
    "accept-language": "en-US,en;q=0.9",
    "user-agent": _USER_AGENT,
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
}
# Headers added on the API POST (on top of session defaults set after bootstrap)
HEADERS = {
    **_BROWSER_HEADERS,
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.msc.com",
    "referer": MSC_SEARCH_PAGE,
    "x-requested-with": "XMLHttpRequest",
}
DATA_SOURCE = "{E9CCBD25-6FBA-4C5C-85F6-FC4F9E5A931F}"


def bootstrap_session():
    """
    Open a Session and GET the search page so MSC sets ASP.NET_SessionId,
    AKA_A2, and the OptanonConsent / msccargo#lang cookies their API expects.
    Returns the primed session.
    """
    session = requests.Session()
    page_headers = {
        **_BROWSER_HEADERS,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    try:
        r = session.get(MSC_SEARCH_PAGE, headers=page_headers, timeout=30)
        print(f"🍪 Bootstrap GET {MSC_SEARCH_PAGE} → {r.status_code}, cookies: {len(session.cookies)}")
    except Exception as e:
        print(f"⚠️ Bootstrap GET failed: {e}")
    return session


SESSION = bootstrap_session()


# === HELPERS ===
def get_ports(city_name: str):
    ports_dict = msc_cities.get("Ports", {})
    entry = ports_dict.get(city_name)
    if not entry:
        return None
    return entry if isinstance(entry, list) else [entry]

def make_payload(pol_id: int, pod_id: int):
    return {
        "FromDate": FROM_DATE,
        "fromPortId": pol_id,
        "toPortId": pod_id,
        "language": "en",
        "dataSourceId": DATA_SOURCE
    }

def fetch_route(payload, pol_name, pod_name, max_retries=3):
    """Call the MSC API via the primed session. Returns parsed data only if valid results exist."""
    global SESSION
    for attempt in range(max_retries):
        try:
            response = SESSION.post(MSC_URL, headers=HEADERS, json=payload, timeout=30)

            if response.status_code != 200:
                print(f"⚠️ Attempt {attempt+1}: {response.status_code} from MSC for {pol_name} → {pod_name}")
                # 403/401 typically means the session cookies expired or Akamai flagged us — re-prime.
                if response.status_code in (401, 403):
                    print("🔄 Re-priming session...")
                    SESSION = bootstrap_session()
                time.sleep(random.uniform(2, 5))
                continue

            try:
                data = response.json()
            except ValueError:
                snippet = response.text[:300].replace("\n", " ")
                print(f"⚠️ Non-JSON response for {pol_name} → {pod_name}: {snippet!r}")
                return None

            # No results — log the actual response shape so we can diagnose
            if not data.get("IsSuccess") or isinstance(data.get("Data"), str):
                msg = data.get("Message") or data.get("ErrorMessage") or str(data.get("Data"))[:200]
                print(f"🚫 No results for {pol_name} → {pod_name}  (IsSuccess={data.get('IsSuccess')}, msg={msg!r})")
                return None

            if data.get("Data"):
                print(f"✅ {pol_name} → {pod_name}: {len(data['Data'])} route(s) found")
                return data

            # 200 OK, IsSuccess=True, but empty Data — log it
            print(f"🚫 Empty Data for {pol_name} → {pod_name}  (IsSuccess={data.get('IsSuccess')})")
            return None

        except Exception as e:
            print(f"⚠️ Error on attempt {attempt+1} for {pol_name} → {pod_name}: {e}")
            time.sleep(random.uniform(3, 6))

    print(f"❌ All retries failed for {pol_name} → {pod_name}")
    return None

# === MAIN LOOP ===
# Statuses that block a row from re-fetching this run. Everything else (no_records,
# error_*, error_exception, pending) is treated as resumable.
_TERMINAL_STATUSES = {"done", "completed"}

try:
    for idx, row in quotes_progress.iterrows():
        status = row.get("status", "pending")
        if status in _TERMINAL_STATUSES:
            continue
        if status != "pending":
            print(f"🔁 Retrying row {idx} (previous status: {status})")

        pol_name = row.get("Port of Loading")
        pod_name = row.get("LastCY")

        pol_entries = get_ports(pol_name)
        pod_entries = get_ports(pod_name)

        if not pol_entries or not pod_entries:
            quotes_progress.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ Skipped (not found): {pol_name} or {pod_name}")
            continue

        success = False

        for pol in pol_entries:
            for pod in pod_entries:
                pol_id = pol.get("PortId")
                pod_id = pod.get("PortId")

                payload = make_payload(pol_id, pod_id)
                data = fetch_route(payload, pol_name, pod_name)

                if data is None:
                    quotes_progress.at[idx, "status"] = "no_records"
                    continue

                # ✅ Only save valid responses
                snapshot_date = assign_snapshot(EARLIEST)

                wrapped = {
                    "query_date": query_timestamp,
                    "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                    "PortOfLoading": pol_name,  # from CSV
                    "LastCY": pod.get("LocationName"),  # from msc_cities.json entry
                    "OFQ": row.get("ID"),
                    "FinalDestination": row.get("Final Destination"),
                    "Data": data.get("Data", [])
                }

                # Compact filenames (avoid spaces, keep identifiable)
                pol_short = (pol_name or "").replace(" ", "")[:5]
                pod_short = (wrapped["LastCY"] or "").replace(" ", "")[:5]
                filename = PROCESSING_DIR / f"MSC_{pol_short}_{pod_short}_{filename_timestamp}.json"

                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(wrapped, f, ensure_ascii=False, indent=2)

                print(f"✅ Saved {len(data.get('Data', []))} routes → {filename}")
                time.sleep(random.uniform(*DELAY_RANGE))

        quotes_progress.at[idx, "status"] = "done" if success else "no_records"

    print("🏁 Finished all routes.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes_progress, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise

# === Data Structuring: build CSV + canonical JSONs from raw MSC_*.json files ===
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("MSC"):
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
            df[col] = (
                pd.to_datetime(df[col], errors="coerce")
                  .dt.strftime("%Y-%m-%d")
                  .fillna("")
            )

    csv_out = get_unique_filename(CSV_DIR / f"MSC_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"MSC_{pol5}_{last5}_{filename_timestamp}.json"
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

