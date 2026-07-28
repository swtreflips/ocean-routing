import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# v3 ORCHESTRATOR (single run, single browser bootstrap):
#
#   1. inland scrape   (origins × inland yards)        -> exact inland canonicals
#   2. derive ocean    (truncate at discharge port)    -> port-to-port canonicals
#   3. missing-ports   (port universe − observed PODs) -> per-origin pure-ocean gaps
#   4. secondary scrape(origins × missing ports)       -> the pure-ocean ports
#   5. push everything to Supabase, at the very end
#
# COS's "session" is just cookies/headers; get_new_session() launches uc.Chrome
# only to grab them, then quits Chrome immediately. We bootstrap ONCE and reuse
# the same creds for both scrape passes (re-bootstrapping only on a 403). No
# browser is held alive — that's the light part. coverage.json is READ-ONLY: its
# v2 connection stats are kept for analysis; we only read type=="port" keys.

from utils import (
    chrome_major,
    get_unique_filename,
    get_unique_path,
    assign_ids_inplace,
    get_unresolved,
)
from utilsv2 import build_canonical_record_v2
from derive_ocean import derive_ocean
import pandas as pd
import datetime
from pathlib import Path
import json
import time
import requests
import undetected_chromedriver as uc

# Silence undetected_chromedriver's __del__ cleanup (see v2 for rationale).
uc.Chrome.__del__ = lambda self: None
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random
from collections import defaultdict


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
RAW_DIR = TEMP_DIR / "raw"                       # inland raw
CANONICAL_DIR = TEMP_DIR / "canonicals"          # inland canonicals
OCEAN_DIR = TEMP_DIR / "ocean"                   # derived port-to-port
RAW_SECONDARY_DIR = TEMP_DIR / "raw_secondary"   # secondary (pure-ocean) raw
SECONDARY_DIR = TEMP_DIR / "secondary"           # secondary (pure-ocean) canonicals
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, OCEAN_DIR, RAW_SECONDARY_DIR, SECONDARY_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Single UTC run timestamp (see DATE.md) ---
run_timestamp = datetime.datetime.now(datetime.timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")

progress_file = get_unique_filename(LOG_DIR / f"COSCOv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"cosco_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"
cities_file = ASSETS_DIR / "cos_cities.json"

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
inland_dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
port_universe = {name for name, meta in coverage.items() if meta.get("type") == "port"}
with open(cities_file, "r") as f:
    allcoscocities = json.load(f)

# Dynamic dates / endpoint
fromDate = today_iso
toDate = (today + datetime.timedelta(days=26)).strftime("%Y-%m-%d")
url = "https://elines.coscoshipping.com/ebschedule/public/purpoShipmentWs"


# 🔹 cookie popup
def handle_cookie_popup(driver, timeout=5):
    try:
        wait = WebDriverWait(driver, timeout)
        allow_button = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[.//span[normalize-space()='Allow All']]")
        ))
        allow_button.click()
        print("🍪 Accepted cookies.")
        WebDriverWait(driver, 5).until_not(
            EC.presence_of_element_located((By.CLASS_NAME, "ivu-modal-content"))
        )
    except Exception:
        print("No cookie popup found (or already dismissed).")


# 🔹 Bootstrap cookies+headers (launches Chrome, grabs creds, quits Chrome)
def get_new_session():
    print("🌐 Bootstrapping new COSCO session...")
    options = uc.ChromeOptions()
    options.headless = True
    options.add_argument("--window-size=1920,1080")
    print("  🚗 Launching undetected Chrome (headless)...")
    driver = uc.Chrome(version_main=chrome_major(), options=options)
    print("  ✓ Chrome launched")

    driver.get("https://elines.coscoshipping.com")
    time.sleep(1)
    driver.add_cookie({"name": "cookieClause", "value": "Accepted",
                       "domain": "elines.coscoshipping.com", "path": "/"})
    driver.add_cookie({"name": "cookiePreference", "value": "Accepted",
                       "domain": "elines.coscoshipping.com", "path": "/"})
    driver.get("https://elines.coscoshipping.com/ebusiness/sailingSchedule/searchByCity")
    time.sleep(2)
    handle_cookie_popup(driver)

    print("  🔍 Probing city-select to populate session state...")
    input_el = driver.find_element(By.CSS_SELECTOR, "input.ivu-select-input")
    try:
        input_el.click()
    except Exception as e:
        print(f"  ⚠️ Direct click intercepted ({type(e).__name__}); clicking via JS.")
        driver.execute_script("arguments[0].click();", input_el)
    input_el.send_keys("Los")
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "li.ivu-select-item")))

    cookies = driver.get_cookies()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": driver.current_url,
        "Origin": "https://elines.coscoshipping.com",
        "language": "en_US",
        "sys": "eb",
    }
    driver.quit()
    print(f"✅ COSCO session established ({len(cookies)} cookies)")
    return {"cookies": cookies, "headers": headers}


from datetime import datetime
import calendar


def get_month_periods(year, month):
    start = datetime(year, month, 1)
    mid = datetime(year, month, 15)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day)
    return {'start': start, 'mid': mid, 'end': end}


def assign_snapshot(date_input):
    date_input = datetime.strptime(date_input, '%Y-%m-%d')
    year, month = date_input.year, date_input.month
    periods = get_month_periods(year, month)
    if 1 <= date_input.day <= 5:
        return periods['start'].date()
    if abs((date_input - periods['mid']).days) <= 5:
        return periods['mid'].date()
    if date_input.day >= 28:
        return (datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)).date()
    return date_input.date()


snapshot_date = assign_snapshot(today_iso)


# =========================================================================
# Reusable scrape + canonical build
# =========================================================================
def scrape_matrix(quotes, creds, cities, raw_dir, label):
    """Scrape (origin, dest) rows with the SHARED creds; write raw wrapped JSON to
    raw_dir. Re-bootstraps creds (in place) on a 403. Mutates `quotes` status."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for idx, row in quotes.iterrows():
        if row["status"] in ["completed", "no_data", "not_found"]:
            continue
        pol_name = row["Port of Loading"]
        pod_name = row["LastCY"]
        key = f"{pol_name}__{pod_name}"
        origin_data = cities.get(pol_name)
        destination_data = cities.get(pod_name)
        if not origin_data or not destination_data:
            print(f"⚠️ [{label}] Skipping: city not found for {key}")
            quotes.at[idx, "status"] = "not_found"
            continue

        retried = False
        while True:
            creds["headers"]["X-Client-Timestamp"] = str(int(time.time() * 1000))
            print(f"🔎 [{label}] Fetching {key} ... (retry={retried})")
            resp = requests.post(
                url,
                headers=creds["headers"],
                cookies={c["name"]: c["value"] for c in creds["cookies"]},
                json={
                    "fromDate": fromDate,
                    "toDate": toDate,
                    "pickup": "C",
                    "delivery": "C",
                    "estimateDate": "D",
                    "originCityUuid": origin_data["cityUuid"],
                    "destinationCityUuid": destination_data["cityUuid"],
                    "originCity": origin_data["fullFormate"] + "," + origin_data["unloCode"],
                    "destinationCity": destination_data["fullFormate"] + "," + destination_data["unloCode"],
                    "cargoNature": "GC",
                    "dataSource": "COSCO IRIS4",
                },
                timeout=20,
            )

            if resp.status_code != 200:
                print(f"❌ [{label}] Status {resp.status_code} for {key}")
                if not retried:
                    new = get_new_session()             # re-bootstrap, reuse onward
                    creds["cookies"], creds["headers"] = new["cookies"], new["headers"]
                    retried = True
                    print("🔄 Retrying with new session...")
                    continue
                quotes.at[idx, "status"] = f"error_{resp.status_code}"
                break

            data = resp.json().get("data", {})
            records = data.get("records") or data.get("content", {}).get("data", [])
            if not records:
                print(f"⚠️ [{label}] No schedules found for {key}")
                quotes.at[idx, "status"] = "no_records"
                break

            wrapped = {
                "query_date": query_timestamp,
                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                "LastCY": pod_name,
                "OFQ": row.get("ID"),
                "FinalDestination": row.get("Final Destination"),
                "PortofLoading": pol_name,
                "schedules": records,
            }
            pol_short = pol_name.replace(" ", "")[:5]
            pod_short = pod_name.replace(" ", "")[:5]
            out_file = get_unique_path(raw_dir / f"COS_{pol_short}_{pod_short}_{filename_timestamp}.json")
            with open(out_file, "w") as f:
                json.dump(wrapped, f, indent=2)
            quotes.at[idx, "status"] = "completed"
            quotes.at[idx, "result_file"] = str(out_file)
            print(f"✅ [{label}] Got {len(records)} schedules for {key}")
            break

        sleep_time = random.uniform(2, 5)
        if random.random() < 0.06:
            sleep_time = random.uniform(10, 20)
        time.sleep(sleep_time)


def _drop_conflated(rec):
    """Secondary-pass guard: drop DIRECT schedules (eta==pod_eta) that discharge at a
    port other than the queried one — the carrier resolved the query to a different
    port's voyages (e.g. a Long Beach query returning LA voyages), which are already
    covered. Keeps true pure-ocean ports (discharge==queried) and rail-served ports
    (eta!=pod_eta). Returns (kept_schedules, n_dropped)."""
    lc = rec.get("last_cy")
    scheds = rec.get("schedules", [])
    kept = [s for s in scheds
            if s.get("port_of_discharge") == lc or s.get("eta") != s.get("pod_eta")]
    return kept, len(scheds) - len(kept)


def build_canonicals(raw_dir, canonical_dir, drop_conflated=False):
    """assign_ids + build_canonical_record_v2 for every raw COS_*.json -> canonical_dir.
    drop_conflated=True (secondary pass) applies the conflated-port guard."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for file in os.listdir(raw_dir):
        if file.startswith("COS_") and file.endswith(".json"):
            assign_ids_inplace(os.path.join(raw_dir, file))
    written, dropped = [], 0
    for file in os.listdir(raw_dir):
        if file.startswith("COS_") and file.endswith(".json"):
            rec = build_canonical_record_v2(os.path.join(raw_dir, file))
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
            out = get_unique_path(canonical_dir / f"COS_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w") as f:
                json.dump(rec, f, indent=2, default=str)
            written.append(out)
    msg = f"✅ Wrote {len(written)} canonical(s) → {canonical_dir}"
    if drop_conflated:
        msg += f"  (dropped {dropped} conflated direct schedules)"
    print(msg)
    return written


def build_quotes(rows, prefix):
    """rows = list of (origin, dest). Returns a scrape-matrix DataFrame."""
    out = [{"ID": f"{prefix}-{i+1:04d}", "Port of Loading": o, "Final Destination": None,
            "LastCY": d, "status": "pending", "result_file": None}
           for i, (o, d) in enumerate(rows)]
    df = pd.DataFrame(out)
    if not df.empty:
        df["result_file"] = df["result_file"].astype("string")
    return df


# =========================================================================
# ORCHESTRATION
# =========================================================================
# Pre-flight: inland yards must resolve in cos_cities.json
_unresolved_inland = [p for p in inland_dests if p not in allcoscocities]
if _unresolved_inland:
    print(f"⚠️ {len(_unresolved_inland)} inland yard(s) not in cos_cities.json (skipped): {_unresolved_inland}")

creds = get_new_session()   # ONE bootstrap, reused for both passes
inland_quotes = build_quotes([(o, d) for o in origins for d in inland_dests], "V3")
secondary_quotes = None

try:
    # --- 1. inland scrape ---
    print(f"\n=== INLAND: {len(origins)} origins × {len(inland_dests)} yards = {len(inland_quotes)} pairs ===")
    scrape_matrix(inland_quotes, creds, allcoscocities, RAW_DIR, "inland")
    build_canonicals(RAW_DIR, CANONICAL_DIR)

    # --- 2. derive ocean (no push) ---
    print("\n=== DERIVE OCEAN ===")
    derive_ocean(CANONICAL_DIR, OCEAN_DIR, ports=port_universe)

    # --- 3. missing-ports diff (per origin), read-only on coverage ---
    observed = defaultdict(set)
    for fp in CANONICAL_DIR.glob("COS_*.json"):
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
        resolvable = [p for p in missing if p in allcoscocities]
        unresolved_missing |= (set(missing) - set(resolvable))
        print(f"  {o}: {len(missing)} missing → {len(resolvable)} queryable: {resolvable}")
        sec_rows += [(o, p) for p in resolvable]
    if unresolved_missing:
        print(f"⚠️ missing ports not in cos_cities.json (cannot query): {sorted(unresolved_missing)}")

    # --- 4. secondary scrape (same creds) ---
    secondary_quotes = build_quotes(sec_rows, "V3S")
    print(f"\n=== SECONDARY: {len(secondary_quotes)} (origin, pure-ocean port) pairs ===")
    if not secondary_quotes.empty:
        scrape_matrix(secondary_quotes, creds, allcoscocities, RAW_SECONDARY_DIR, "secondary")
        build_canonicals(RAW_SECONDARY_DIR, SECONDARY_DIR, drop_conflated=True)
    else:
        print("  (no missing ports — nothing to scrape)")

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"COSv3_unresolved_ports_{today_str}.csv")
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
        ingest_new_canonicals("COS", canonical_dir=cdir, ledger_path=TEMP_DIR / ledger)
    except Exception as e:
        print(f"⚠️ Supabase push failed for {cdir.name} (non-fatal): {e}")

print("✅ v3 run complete.")
