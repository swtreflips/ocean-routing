import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# EMC v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search).
#
#   MODE = "ports"     PRODUCTION / final. Matrix = origins x type=="port" keys
#                      (origin -> port only). Pushes the port canonicals to Supabase.
#   MODE = "calibrate" Retained (not deleted) for the separate port<->inland-yard
#                      relationships project: matrix = origins x assets/pending.json
#                      inland yards, writes enriched inland canonicals for
#                      extract_connections.py to pool — NO Supabase push.
#
# Evergreen HTML pipeline either way: POST form -> save HTML -> parse -> enriched
# canonical (utilsv2). Output under assets/temp/: work/, html/, canonicals/, raw/.
# Cities come from emc_citiesv2.json (exploded, one code per key). coverage.json is
# a bare keys+type utility. Scrape uses the v3 multi-sweep retry: transient failures
# (timeout / stall / 403/429/5xx) are re-queued across sweeps with an escalating
# cooldown; a whole sweep that resolves nothing aborts.

# ports = go live; calibrate = scrape the pending.json yards (relationships project).
# ⚠️ Set back to "ports" before the next production run.
MODE = "ports"

import json
import time
import random
import shutil
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import re
from bs4 import BeautifulSoup
from requests.exceptions import Timeout, RequestException

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    batch_transform_processing_dir,
    get_unresolved,
)
from utilsv2 import build_canonical_record_v2


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


# --- Project root (ocean-routing/) ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"

# --- Carrier-specific folder (emc/) ---
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

# --- v2 output: local temp only (no DB) ---
TEMP_DIR = ASSETS_DIR / "temp"
PROCESSING_DIR = TEMP_DIR / "work"        # HTML + parsed JSON during processing
HTML_DIR = TEMP_DIR / "html"              # archived raw HTML (discriminator hunt)
CANONICAL_DIR = TEMP_DIR / "canonicals"   # enriched v2 records
RAW_DIR = TEMP_DIR / "raw"                # archived parsed JSON
LOG_DIR = TEMP_DIR

for d in (PROCESSING_DIR, HTML_DIR, CANONICAL_DIR, RAW_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")

snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"EMCv2_{today_str}.csv")

# Log set up
logfile = get_unique_filename(LOG_DIR / f"EMC_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)  # auto-flush
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"   # v2's OWN coverage — independent of v3's coverage.json
cities_file = ASSETS_DIR / "emc_citiesv2.json"   # exploded: one city per key (Seattle/Tacoma,
#                                                  Miami/Port Everglades, Dallas/Fort Worth,
#                                                  Kansas City KS/MO split apart). v1's
#                                                  emc_cities.json is left untouched.

# --- Build the query matrix: origins x (ports | pending inland yards) -------
origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()

with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]

if MODE == "ports":
    dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]
    dest_label = "ports"
elif MODE == "calibrate":
    with open(ASSETS_DIR / "pending.json", "r", encoding="utf-8") as f:
        dests = json.load(f)["pending"]
    dest_label = "pending inland yards"
else:
    raise ValueError(f"Unknown MODE: {MODE!r} (use 'ports' or 'calibrate')")

matrix_rows = []
_qid = 1
for pol in origins:
    for pod in dests:
        matrix_rows.append({
            "ID": f"V2-{_qid:04d}",
            "Port of Loading": pol,
            "LastCY": pod,
            "status": "pending",
            "result_file": None,
        })
        _qid += 1

quotes = pd.DataFrame(matrix_rows)
quotes["result_file"] = quotes["result_file"].astype("string")

print(f"✅ [{MODE}] Query matrix built: {len(origins)} origins x {len(dests)} {dest_label} "
      f"= {len(quotes)} pairs.")
print(quotes[["ID", "Port of Loading", "LastCY", "status"]])


# =========================
# Evergreen helpers
# =========================
def save_schedule_html(html, row_id, pol, pod, meta_comment=""):
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    filename = f"EMC_{row_id}_{pol}_{pod}_{ts}.html"
    filepath = PROCESSING_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        if meta_comment:
            f.write(meta_comment)
        f.write(html)
    return str(filepath)


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
    schedules = [
        thead for thead in soup.select("thead.Corner")
        if thead.select_one("td.ec-text-center")
    ]
    return len(schedules)


def is_no_results_page(html):
    return "Data not found." in html


# =========================
# Config
# =========================
url = "https://ss.shipmentlink.com/tvs2/jsp/TVS2_InteractiveScheduleRouting.jsp"
headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"}
cookies = {}
REQUEST_TIMEOUT = 10
MAX_SWEEPS = 6                                 # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]      # seconds before each requeue sweep
DELAY_RANGE = (0, 1.2)                         # between distinct pairs within a sweep
#                                                (tightened ~3.3x from (0, 4), the same
#                                                proportion COS was cut (2,5)->(0.5,1.5);
#                                                lower to push harder, raise if EMC stalls)

with open(cities_file, "r", encoding="utf-8") as f:
    cities_evm = json.load(f)

# Pre-flight: every destination must resolve to a usable location (a display_name
# is enough for EMC — the unlocode field can be empty). Validate via get_locationss
# so a missing key OR an entry with no name surfaces before a session is spent.
_unresolved = [p for p in dests
               if not any(loc.get("name") for loc in get_locationss(p, cities_evm))]
if _unresolved:
    print(f"⚠️ {len(_unresolved)} {dest_label} not usable in emc_cities.json (will be skipped): "
          f"{_unresolved}")

# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(pol_name, pod_name, row, stats):
    """One EMC POST for a pair; save the schedule HTML on success.

    Returns (status, saved_path). status is one of:
      'done'              -> schedules found + HTML saved
      'no_records'        -> ShipmentLink's confirmed "Data not found." page
      'skipped_not_found' -> POL or POD not usable in emc_citiesv2.json
      'pending'           -> TRANSIENT failure (timeout / empty-stall without the
                             'Data not found.' marker / 403/429/5xx) — the next
                             sweep retries it
      'error_<code>'      -> a non-transient 4xx (surfaced, not requeued)

    emc_citiesv2.json is exploded (one code per key), so there is no pol×pod
    fan-out and no Seattle/Tacoma-style conflation — take the single entry.
    """
    pol_locs = get_locationss(pol_name, cities_evm)
    pod_locs = get_locationss(pod_name, cities_evm)
    if not pol_locs or not pod_locs:
        print(f"⚠️ Missing mapping: {pol_name} → {pod_name}")
        return "skipped_not_found", None

    pol, pod = pol_locs[0], pod_locs[0]
    oriLocation, oriLocationName = pol["code"], pol["name"]
    desLocation, desLocationName = pod["code"], pod["name"]

    payload = {
        "oriCountry": "", "groupRadioOri": "ALL",
        "desCountry": "", "groupRadioDes": "ALL",
        **build_evergreen_dates(),
        "arrivalMonth": "", "arrivalDay": "", "arrivalYear": "",
        "durationWeek": "14", "reeferCargo": "N",
        "oriLocation": oriLocation, "oriLocationName": oriLocationName,
        "desLocation": desLocation, "desLocationName": desLocationName,
        "carrier": "V", "serviceMode": "", "isReefer": "N",
        "func": "getSearchResult", "oriUSCA": "", "desUSCA": "",
        "oriEastWest": "", "desEastWest": "ALL",
        "oriUseMode": "I", "desUseMode": "I",
    }

    stats["calls"] += 1                           # count every HTTP request to ShipmentLink
    try:
        resp = requests.post(url, data=payload, headers=headers, cookies=cookies,
                             timeout=REQUEST_TIMEOUT)
    except (Timeout, RequestException) as e:
        print(f"⏱️ {pol_name}({oriLocation}) → {pod_name}({desLocation}): {e} (transient → requeue)")
        return "pending", None

    code = resp.status_code
    print(f"🚢 {pol_name}({oriLocation}) → {pod_name}({desLocation}): {code}")
    if code != 200:
        return ("pending" if code in (403, 429) or code >= 500 else f"error_{code}"), None

    html = resp.text
    if count_schedules(html) > 0:
        meta_comment = (
            "<!-- "
            f"carrier=EVERGREEN | POL={pol_name} | LastCY={pod_name} | "
            f"OFQ={row.get('ID')} | snapshot_date={snapshot_date} | "
            f"query_date={query_timestamp} | -->\n"
        )
        saved = save_schedule_html(html, row["ID"], oriLocation, desLocation,
                                   meta_comment=meta_comment)
        print(f"✅ Saved {count_schedules(html)} schedules → {saved}")
        return "done", saved
    if is_no_results_page(html):
        print(f"ℹ️ No schedules (confirmed empty) for {pol_name} → {pod_name}")
        return "no_records", None
    print(f"⚠️ Empty without 'Data not found.' marker for {pol_name} → {pod_name} (stall → requeue)")
    return "pending", None


def scrape_matrix(quotes):
    """Multi-sweep drain of the matrix (the v3 retry model). Stateless HTTP, so no
    session re-bootstrap; transient failures just requeue for the next sweep."""
    stats = {"calls": 0}                       # total HTTP requests to ShipmentLink
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status, saved = _scrape_pair(row["Port of Loading"], row["LastCY"], row, stats)
            quotes.at[idx, "status"] = status
            if saved is not None:
                quotes.at[idx, "result_file"] = saved
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — ShipmentLink unresponsive; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Write a run summary to the log: totals, wall-clock, and throughput.
    `elapsed` is the full scrape wall-clock (includes any mid-run cooldowns) — the
    real end-to-end pace, not just active time."""
    def _n(pred):
        return int(sum(1 for i in quotes.index if pred(quotes.at[i, "status"])))

    done = _n(lambda s: s == "done")
    no_records = _n(lambda s: s == "no_records")
    skipped = _n(lambda s: s == "skipped_not_found")
    pending = _n(lambda s: s == "pending")
    errors = _n(lambda s: isinstance(s, str) and s.startswith("error_"))

    mins = elapsed / 60
    per_min = calls / mins if mins > 0 else 0.0
    min_per_100 = (mins / calls * 100) if calls > 0 else 0.0

    print("\n" + "=" * 48)
    print("RUN STATS")
    print("=" * 48)
    print(f"  pairs           : {len(quotes)} total")
    print(f"    done          : {done}")
    print(f"    no_records    : {no_records}")
    print(f"    skipped       : {skipped}")
    print(f"    error         : {errors}")
    print(f"    pending(left) : {pending}")
    print(f"  API calls       : {calls}")
    print(f"  scrape elapsed  : {elapsed:.1f}s  ({mins:.2f} min)")
    print(f"  throughput      : {per_min:.1f} calls/min")
    print(f"                    {min_per_100:.2f} min per 100 calls")
    print("=" * 48)

    if pending:
        print(f"⚠️ {pending} pair(s) still pending after {MAX_SWEEPS} sweeps (no HTML saved for them).")


try:
    scrape_matrix(quotes)
    print("🏁 All done.")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# === AFTER MAIN LOOP: parse HTML -> JSON, build enriched canonicals ===
batch_transform_processing_dir(PROCESSING_DIR, HTML_DIR)

all_canonical = []
for file in os.listdir(PROCESSING_DIR):
    if not file.endswith(".json") or not file.startswith("EMC_"):
        continue
    rec = build_canonical_record_v2(os.path.join(PROCESSING_DIR, file))
    if rec is not None:
        all_canonical.append(rec)

unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"EMCv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

try:
    # --- Write enriched canonical JSONs (one per query), rollback on failure ---
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

    print(f"✅ Wrote {len(written_canonical)} enriched canonical JSON(s) → {CANONICAL_DIR}")

    # --- Archive parsed JSON into RAW_DIR (HTML already archived to HTML_DIR) ---
    for file in os.listdir(PROCESSING_DIR):
        if file.startswith("EMC_") and file.endswith(".json"):
            src = PROCESSING_DIR / file
            dst = get_unique_path(RAW_DIR / file)
            shutil.move(str(src), str(dst))

    print("✅ Parsed JSON archived to RAW_DIR; raw HTML in HTML_DIR.")

    if MODE == "ports":
        # --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
        try:
            sys.path.insert(0, str(PROJECT_ROOT / "src"))
            from ingest.ingest import ingest_new_canonicals
            ingest_new_canonicals(
                "EMC",
                canonical_dir=CANONICAL_DIR,
                ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json",
            )
        except Exception as e:
            print(f"⚠️ Supabase ingestion step failed (non-fatal): {e}")
        print("ℹ️ Next: run  python synthesize_inland.py  to recreate + push inland schedules.")
    else:
        # calibrate: no DB push — these inland canonicals feed extract_connections.py
        print("ℹ️ [calibrate] Next: run  python extract_connections.py  to fill the "
              "pending yards in coverage.json (then trim pending.json).")

except Exception as e:
    print(f"❌ Transform failed. JSONs kept in {PROCESSING_DIR}.")
    print("Error:", e)
