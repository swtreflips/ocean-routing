import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# EMC v3 ORCHESTRATOR (single run):
#
#   1. inland scrape   (origins × inland yards)        -> exact inland canonicals
#   2. derive ocean    (truncate at discharge port)    -> port-to-port canonicals
#   3. missing-ports   (port universe − observed PODs) -> per-origin pure-ocean gaps
#   4. secondary scrape(origins × missing ports)       -> the pure-ocean ports
#   5. push everything to Supabase, at the very end
#
# Evergreen is HTML/requests (no browser session): POST form -> save HTML ->
# batch parse -> enriched canonical (utilsv2, incl. the NA-destination junk
# filter). coverage.json is READ-ONLY: its v2 stats are kept for analysis; we
# only read type=="port" keys.

import json
import time
import random
import shutil
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from requests.exceptions import Timeout, RequestException
from collections import defaultdict

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    batch_transform_processing_dir,
    get_unresolved,
)
from utilsv2 import build_canonical_record_v2
from derive_ocean import derive_ocean


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


# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

TEMP_DIR = ASSETS_DIR / "temp_v3"
# inland pass
WORK_DIR = TEMP_DIR / "work"
HTML_DIR = TEMP_DIR / "html"
CANONICAL_DIR = TEMP_DIR / "canonicals"
RAW_DIR = TEMP_DIR / "raw"
OCEAN_DIR = TEMP_DIR / "ocean"
# secondary pass
WORK_SECONDARY_DIR = TEMP_DIR / "work_secondary"
HTML_SECONDARY_DIR = TEMP_DIR / "html_secondary"
SECONDARY_DIR = TEMP_DIR / "secondary"
RAW_SECONDARY_DIR = TEMP_DIR / "raw_secondary"
LOG_DIR = TEMP_DIR

for _d in (WORK_DIR, HTML_DIR, CANONICAL_DIR, RAW_DIR, OCEAN_DIR,
           WORK_SECONDARY_DIR, HTML_SECONDARY_DIR, SECONDARY_DIR, RAW_SECONDARY_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"EMCv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"EMC_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"
cities_file = ASSETS_DIR / "emc_cities.json"

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
inland_dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
port_universe = {name for name, meta in coverage.items() if meta.get("type") == "port"}
with open(cities_file, "r", encoding="utf-8") as f:
    cities_evm = json.load(f)

# --- Config ---
URL = "https://ss.shipmentlink.com/tvs2/jsp/TVS2_InteractiveScheduleRouting.jsp"
HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"}
COOKIES = {}
REQUEST_TIMEOUT = 10
MAX_ATTEMPTS = 8
RETRY_DELAY = 20
DELAY_RANGE = (0, 4)


# =========================================================================
# Evergreen helpers
# =========================================================================
def build_evergreen_dates(start_date=None):
    if start_date is None:
        start_date = datetime.today()
    return {
        "departureMonth": start_date.strftime("%m"),
        "departureDay": start_date.strftime("%d"),
        "departureYear": start_date.strftime("%Y"),
        "departureDate": start_date.strftime("%Y%m%d"),
        "arrivalDate": start_date.strftime("%m%d"),
        "departureDateShow": start_date.strftime("%b-%d-%Y").upper(),
    }


def get_locationss(city_name, cities_map):
    entries = cities_map.get(city_name)
    if not entries:
        return []
    out = []
    for e in entries:
        out.append({
            "code": e.get("unlocode") or e.get("businessLocode"),
            "name": e.get("display_name") or e.get("businessLocationName"),
            "short_name": e.get("short_name"),
        })
    return out


def count_schedules(html):
    soup = BeautifulSoup(html, "lxml")
    return len([t for t in soup.select("thead.Corner") if t.select_one("td.ec-text-center")])


def is_no_results_page(html):
    return "Data not found." in html


def save_schedule_html(html, row_id, pol_code, pod_code, work_dir, meta_comment=""):
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    filepath = work_dir / f"EMC_{row_id}_{pol_code}_{pod_code}_{ts}.html"
    with open(filepath, "w", encoding="utf-8") as f:
        if meta_comment:
            f.write(meta_comment)
        f.write(html)
    return str(filepath)


# =========================================================================
# Reusable scrape + canonical build
# =========================================================================
def scrape_matrix(quotes, cities, work_dir, label):
    """POST the EMC form per (origin, dest) row; save HTML to work_dir. Mutates status."""
    work_dir.mkdir(parents=True, exist_ok=True)
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue
        pol_name, pod_name = row["Port of Loading"], row["LastCY"]
        pol_locations = get_locationss(pol_name, cities)
        pod_locations = get_locationss(pod_name, cities)
        if not pol_locations or not pod_locations:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ [{label}] Missing mapping: {pol_name} → {pod_name}")
            continue

        success = False
        for pol in pol_locations:
            for pod in pod_locations:
                oriLocation, oriLocationName = pol["code"], pol["name"]
                desLocation, desLocationName = pod["code"], pod["name"]
                payload = {
                    "oriCountry": "", "groupRadioOri": "ALL", "desCountry": "", "groupRadioDes": "ALL",
                    **build_evergreen_dates(),
                    "arrivalMonth": "", "arrivalDay": "", "arrivalYear": "",
                    "durationWeek": "14", "reeferCargo": "N",
                    "oriLocation": oriLocation, "oriLocationName": oriLocationName,
                    "desLocation": desLocation, "desLocationName": desLocationName,
                    "carrier": "V", "serviceMode": "", "isReefer": "N", "func": "getSearchResult",
                    "oriUSCA": "", "desUSCA": "", "oriEastWest": "", "desEastWest": "ALL",
                    "oriUseMode": "I", "desUseMode": "I",
                }
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        print(f"🚢 [{label}] {pol_name}({oriLocation}) → {pod_name}({desLocation}) | Attempt {attempt}")
                        resp = requests.post(URL, data=payload, headers=HEADERS,
                                             cookies=COOKIES, timeout=REQUEST_TIMEOUT)
                        resp.raise_for_status()
                        if count_schedules(resp.text) > 0:
                            meta = ("<!-- "
                                    f"carrier=EVERGREEN | POL={pol_name} | LastCY={pod_name} | "
                                    f"OFQ={row.get('ID')} | snapshot_date={snapshot_date} | "
                                    f"query_date={query_timestamp} | -->\n")
                            save_schedule_html(resp.text, row["ID"], oriLocation, desLocation, work_dir, meta)
                            success = True
                            print(f"✅ [{label}] Saved schedules for {pol_name} → {pod_name}")
                            break
                        elif is_no_results_page(resp.text):
                            print(f"ℹ️ [{label}] No schedules (confirmed empty) — skipping retries")
                            break
                        else:
                            print(f"⚠️ [{label}] Empty without marker — likely a stall, will retry")
                    except Timeout:
                        print(f"⏱️ [{label}] Timeout after {REQUEST_TIMEOUT}s")
                    except RequestException as e:
                        print(f"❌ [{label}] Request failed: {e}")
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(RETRY_DELAY)
                time.sleep(random.uniform(*DELAY_RANGE))

        quotes.at[idx, "status"] = "done" if success else "no_records"


def _drop_conflated(rec):
    """Secondary-pass guard: drop DIRECT schedules (eta==pod_eta) that discharge at a
    port other than the queried one — the carrier resolved the query to a different
    port's voyages (e.g. a Long Beach query returning LA voyages), already covered.
    Keeps pure-ocean ports (discharge==queried) and rail-served ports (eta!=pod_eta,
    e.g. EMC's Savannah->Jacksonville). Returns (kept_schedules, n_dropped)."""
    lc = rec.get("last_cy")
    scheds = rec.get("schedules", [])
    kept = [s for s in scheds
            if s.get("port_of_discharge") == lc or s.get("eta") != s.get("pod_eta")]
    return kept, len(scheds) - len(kept)


def build_canonicals(work_dir, html_dir, canonical_dir, raw_dir, drop_conflated=False):
    """Parse HTML -> JSON (archive HTML), build enriched canonicals, archive parsed JSON.
    drop_conflated=True (secondary pass) applies the conflated-port guard."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    batch_transform_processing_dir(work_dir, html_dir)        # HTML -> JSON + archive HTML
    written, dropped = [], 0
    for file in os.listdir(work_dir):
        if file.startswith("EMC_") and file.endswith(".json"):
            rec = build_canonical_record_v2(os.path.join(work_dir, file))
            if rec is None:
                continue
            if drop_conflated:
                kept, n = _drop_conflated(rec)
                dropped += n
                if not kept:
                    continue
                rec["schedules"] = kept
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(canonical_dir / f"EMC_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
            written.append(out)
    for file in os.listdir(work_dir):                          # archive parsed JSON
        if file.startswith("EMC_") and file.endswith(".json"):
            shutil.move(str(work_dir / file), str(get_unique_path(raw_dir / file)))
    msg = f"✅ Wrote {len(written)} canonical(s) → {canonical_dir}"
    if drop_conflated:
        msg += f"  (dropped {dropped} conflated direct schedules)"
    print(msg)
    return written


def build_quotes(rows, prefix):
    out = [{"ID": f"{prefix}-{i+1:04d}", "Port of Loading": o, "LastCY": d,
            "status": "pending", "result_file": None}
           for i, (o, d) in enumerate(rows)]
    df = pd.DataFrame(out)
    if not df.empty:
        df["result_file"] = df["result_file"].astype("string")
    return df


# =========================================================================
# ORCHESTRATION
# =========================================================================
inland_quotes = build_quotes([(o, d) for o in origins for d in inland_dests], "V3")
secondary_quotes = None

try:
    # --- 1. inland scrape ---
    print(f"\n=== INLAND: {len(origins)} origins × {len(inland_dests)} yards = {len(inland_quotes)} pairs ===")
    scrape_matrix(inland_quotes, cities_evm, WORK_DIR, "inland")
    build_canonicals(WORK_DIR, HTML_DIR, CANONICAL_DIR, RAW_DIR)

    # --- 2. derive ocean (no push) ---
    print("\n=== DERIVE OCEAN ===")
    derive_ocean(CANONICAL_DIR, OCEAN_DIR, ports=port_universe)

    # --- 3. missing-ports diff (per origin), read-only on coverage ---
    observed = defaultdict(set)
    for fp in CANONICAL_DIR.glob("EMC_*.json"):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        o = rec.get("port_of_loading")
        for s in rec.get("schedules", []):
            pod = s.get("port_of_discharge")
            if pod:
                observed[o].add(pod)

    sec_rows, unresolved_missing = [], set()
    print("\n=== MISSING PORTS (port universe − observed PODs) ===")
    for o in origins:
        missing = sorted(port_universe - observed.get(o, set()))
        resolvable = [p for p in missing if any(loc.get("name") for loc in get_locationss(p, cities_evm))]
        unresolved_missing |= (set(missing) - set(resolvable))
        print(f"  {o}: {len(missing)} missing → {len(resolvable)} queryable: {resolvable}")
        sec_rows += [(o, p) for p in resolvable]
    if unresolved_missing:
        print(f"⚠️ missing ports not usable in emc_cities.json: {sorted(unresolved_missing)}")

    # --- 4. secondary scrape ---
    secondary_quotes = build_quotes(sec_rows, "V3S")
    print(f"\n=== SECONDARY: {len(secondary_quotes)} (origin, pure-ocean port) pairs ===")
    if not secondary_quotes.empty:
        scrape_matrix(secondary_quotes, cities_evm, WORK_SECONDARY_DIR, "secondary")
        build_canonicals(WORK_SECONDARY_DIR, HTML_SECONDARY_DIR, SECONDARY_DIR, RAW_SECONDARY_DIR, drop_conflated=True)
    else:
        print("  (no missing ports — nothing to scrape)")

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"EMCv3_unresolved_ports_{today_str}.csv")
        safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
        print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(inland_quotes, crash_file, index=False)
    if secondary_quotes is not None:
        safe_to_csv(secondary_quotes, get_unique_filename(crash_file.with_stem(crash_file.stem + "_secondary")), index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}  (no push)")
    raise


# --- 5. push everything, at the very end (clean finish only) ---
print("\n=== PUSH (end of run) ===")
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

for cdir, ledger in [
    (CANONICAL_DIR, "ingest_ledger_inland.json"),
    (OCEAN_DIR, "ingest_ledger_ocean.json"),
    (SECONDARY_DIR, "ingest_ledger_secondary.json"),
]:
    try:
        ingest_new_canonicals("EMC", canonical_dir=cdir, ledger_path=TEMP_DIR / ledger)
    except Exception as e:
        print(f"⚠️ Supabase push failed for {cdir.name} (non-fatal): {e}")

print("✅ v3 run complete.")
