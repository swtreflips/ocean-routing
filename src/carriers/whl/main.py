import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

import json
import random
import shutil
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
from geopy.geocoders import Nominatim

from utils import (
    assign_snapshot,
    batch_transform_processing_dir,
    build_canonical_record,
    build_schedule_dataframe,
    build_schedule_rows,
    build_voronoi_lookup,
    create_wanhai_session,
    geocode_city,
    get_unique_filename,
    get_unique_path,
    load_connections,
    load_progress,
    load_wanhai_locations,
    resolve,
    resolve_missing_locations,
    scrape_with_decision,
    WHLDestinationSkipped,
    WHLDestinationUnmapped,
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
LOG_DIR = PROJECT_ROOT / "src" / "data" / "whl" / "log"
RAW_DIR = PROJECT_ROOT / "src" / "data" / "whl" / "raw"
PROCESSING_DIR = PROJECT_ROOT / "src" / "data" / "whl"
TABLES_DIR = PROJECT_ROOT / "src" / "data" / "tables"
CSV_DIR = PROJECT_ROOT / "src" / "data" / "whl" / "csvs"
CANONICAL_DIR = PROJECT_ROOT / "src" / "data" / "whl" / "canonical"
HTML_DIR = PROJECT_ROOT / "src" / "data" / "whl" / "html"
# --- Carrier-specific folder (whl/) ---
CARRIER_DIR = Path(__file__).resolve().parent

LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSING_DIR.mkdir(parents=True, exist_ok=True)
TABLES_DIR.mkdir(parents=True, exist_ok=True)
CSV_DIR.mkdir(parents=True, exist_ok=True)
CANONICAL_DIR.mkdir(parents=True, exist_ok=True)


run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")

snapshot_date = assign_snapshot(today_iso)
progress_file = get_unique_filename(LOG_DIR / f"WHL{today_str}.csv")


# --- Run log ---
logfile = get_unique_filename(LOG_DIR / f"WHL_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs (shared data) ---
quotes_file = DATA_DIR / "quotes.csv"
locations_file = DATA_DIR / "locations.csv"

# --- Inputs (carrier-specific) ---
voronoi_file = CARRIER_DIR / "assets" / "whl_yards.geojson"
wanhai_locations_file = CARRIER_DIR / "assets" / "whl_cities.json"
connections_file = CARRIER_DIR / "assets" / "whl_connections.json"

# --- Load data ---
quotes = pd.read_csv(quotes_file)
locations = pd.read_csv(locations_file)
gdf_voronoi = gpd.read_file(voronoi_file)
wanhai_locations = load_wanhai_locations(wanhai_locations_file)
connections = load_connections(connections_file)

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

# --- Step 5: Hand off to scrape loop (in-memory; no intermediate disk write) ---
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


# --- Step 6: Selenium scrape loop ---
DELAY_RANGE = (2, 4)

driver, wait = create_wanhai_session(headless=False)

try:
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue

        pol_name = str(row["Port of Loading"]).strip()
        fd_name = str(row.get("Final Destination", "")).strip()
        raw_cy = row.get("LastCY")
        cy_name = str(raw_cy).strip() if pd.notnull(raw_cy) else ""
        ofq = row.get("ID")

        print(f"[{idx}] POL={pol_name}  FD={fd_name}  LastCY={cy_name or '(unassigned)'}")

        if not cy_name:
            print("      ⚠️ No Last CY assigned (Voronoi miss or warehouse without coords)")
            quotes.at[idx, "status"] = "skipped_not_found"
            continue

        origin_resolved, o_reason = resolve(pol_name, wanhai_locations)
        if origin_resolved is None:
            print(f"      ⚠️ origin unresolved: {o_reason}")
            quotes.at[idx, "status"] = "skipped_not_found"
            continue

        try:
            html, n_rows, actual_pol, actual_pod, kind, used_dest = scrape_with_decision(
                driver, wait, origin_resolved, cy_name, wanhai_locations, connections,
            )
        except WHLDestinationUnmapped as e:
            # LastCY not in wanhai_locations AND not a fallback-eligible port.
            # Voronoi assignment should prevent this; safety net only.
            print(f"      ⏭️  skipped_unmapped: {e}")
            quotes.at[idx, "status"] = "skipped_unmapped"
            continue
        except WHLDestinationSkipped as e:
            # LastCY just isn't in the dropdown for this origin (and no port
            # fallback applies). Silent skip.
            print(f"      ⏭️  skipped_not_found: {e}")
            quotes.at[idx, "status"] = "skipped_not_found"
            continue
        except Exception as e:
            print(f"      transient {type(e).__name__}; retrying...")
            time.sleep(2)
            try:
                html, n_rows, actual_pol, actual_pod, kind, used_dest = scrape_with_decision(
                    driver, wait, origin_resolved, cy_name, wanhai_locations, connections,
                )
            except WHLDestinationUnmapped as e2:
                print(f"      ⏭️  skipped_unmapped: {e2}")
                quotes.at[idx, "status"] = "skipped_unmapped"
                continue
            except WHLDestinationSkipped as e2:
                print(f"      ⏭️  skipped_not_found: {e2}")
                quotes.at[idx, "status"] = "skipped_not_found"
                continue
            except Exception as e2:
                print(f"      ❌ scrape failed: {type(e2).__name__}: {e2}")
                traceback.print_exc()
                quotes.at[idx, "status"] = "failed"
                continue

        out_path = PROCESSING_DIR / f"wanhai_{idx}_{actual_pol}_{actual_pod}.html"
        meta_comment = (
            "<!-- "
            f"POL={pol_name} | "
            f"LastCY={cy_name} | "
            f"FinalDestination={fd_name} | "
            f"OFQ={ofq} | "
            f"snapshot_date={snapshot_date} | "
            f"query_date={query_timestamp}"
            " -->\n"
        )
        out_path.write_text(meta_comment + html, encoding="utf-8")

        via = "" if kind == "direct" else f"  (via fallback {used_dest})"
        print(f"      ✅ saved {out_path.name}  ({n_rows} rows){via}")

        quotes.at[idx, "status"] = "ok"
        quotes.at[idx, "result_file"] = out_path.name

        sleep_time = random.uniform(*DELAY_RANGE)
        print(f"      ⏳ Sleeping {sleep_time:.2f}s")
        time.sleep(sleep_time)

except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise

finally:
    try:
        driver.quit()
    except Exception:
        pass


# --- Step 7: HTML → JSON ---
batch_transform_processing_dir(PROCESSING_DIR, HTML_DIR)


# --- Step 8: JSON → CSV + canonical, then archive ---
all_rows = []
all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("wanhai_"):
        continue
    full_path = os.path.join(PROCESSING_DIR, file)
    all_rows.extend(build_schedule_rows(full_path, connections))
    rec = build_canonical_record(full_path, connections)
    if rec is not None:
        all_canonical.append(rec)

try:
    df = pd.DataFrame(all_rows)
    for col in ["ETD", "ETA", "POD ETA", "Cut-Off Date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")

    csv_out = get_unique_filename(CSV_DIR / f"WHL_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")
    print(df.head())

    # --- Write canonical JSONs (one per query), with rollback on failure
    written_canonical = []
    try:
        for rec in all_canonical:
            pol5  = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"]         or "").replace(" ", "")[:5]
            fname = f"WHL_{pol5}_{last5}_{filename_timestamp}.json"
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
    traceback.print_exc()
