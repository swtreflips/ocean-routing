import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# MSK v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like COS/EMC/HPL/MSC v2: origins x type=="port"
# coverage, the v3 multi-sweep retry, a run-stats summary in the log, and a
# LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# MSK mechanics: pure requests POST (api.maersk.com/routing-unified, GEO_ID payload,
# static consumer-key header) — no session bootstrap (stateless), so transient
# failures just requeue for the next sweep. v2 has its OWN assets so it never
# touches v3's call set: msk_citiesv2.json (exploded cities) + coverage_v2.json
# (17 ports; Long Beach / Seattle / Port Everglades each their own GEO_ID call — MSK
# uses a per-city GEO_ID, so results are distinct, no siblings). v1's msk_cities.json
# and v3's msk_citiesv3.json / coverage.json are left untouched.
#
# LOCATION ENRICHMENT (from v3): MSK returns some POD / transshipment ports as GEO_ID
# codes, not names. After scraping, enrich_location_codes() resolves each code -> city
# name via Maersk's geography API, cached in city_codes.json (shared with v1), and
# rewrites the codes inside the raw responses BEFORE canonical building — so
# port_of_discharge / ts_ports come out as names.

import json
import time
import random
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, timezone, date

from utils import (
    get_unique_path,
    get_unique_filename,
    assign_snapshot,
    build_canonical_record,
    get_unresolved,
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
            time.sleep(backoff * (attempt + 1))


# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
CARRIER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = CARRIER_DIR / "assets"

TEMP_DIR = ASSETS_DIR / "temp"            # v2 output (separate from v3's temp_v3/)
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)   # UTC — query_date / filenames (audit trail)
today = date.today()                         # LOCAL date -> query window
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"MSKv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"MSK_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage (17 ports) — independent of v3
cities_file = ASSETS_DIR / "msk_citiesv2.json"      # v2's OWN exploded cities — independent of v3

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
coverage = json.loads(coverage_file.read_text(encoding="utf-8"))["coverage"]
port_universe = [name for name, meta in coverage.items() if meta.get("type") == "port"]
with open(cities_file, "r", encoding="utf-8") as f:
    msk_cities = json.load(f)


def get_locations(city_name):
    """Return [{"name", "code"=maerskGeoLocationId}, ...] for a city; [] if unknown.
    msk_citiesv3.json is flat (one city per key)."""
    entry = msk_cities.get(city_name)
    if not entry:
        return []
    entries = entry if isinstance(entry, list) else [entry]
    return [{"name": e.get("localityName") or city_name, "code": e.get("maerskGeoLocationId")}
            for e in entries if e.get("maerskGeoLocationId")]


# --- API config ---
EARLIEST = today_iso
LATEST = (today + timedelta(days=29)).strftime("%Y-%m-%d")
DELAY_RANGE = (1, 3)                            # between pairs within a sweep (tunable)
MAX_SWEEPS = 6                                  # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]       # seconds before each requeue sweep
URL = "https://api.maersk.com/routing-unified/routing/routings-queries"
HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "api-version": "1",
    "consumer-key": "uXe7bxTHLY0yY0e8jnS6kotShkLuAAqG",
}


def make_payload(pol_geo_id, pod_geo_id):
    return {
        "requestType": "DATED_SCHEDULES",
        "includeFutureSchedules": True,
        "routingCondition": "PREFERRED",
        "exportServiceType": "CY",
        "importServiceType": "CY",
        "brandCode": "MSL",
        "startLocation": {
            "dataObject": "CITY",
            "alternativeCodes": [{"alternativeCodeType": "GEO_ID", "alternativeCode": pol_geo_id}],
            "cityCode": "",
        },
        "endLocation": {
            "dataObject": "CITY",
            "alternativeCodes": [{"alternativeCodeType": "GEO_ID", "alternativeCode": pod_geo_id}],
            "cityCode": "",
        },
        "timeRange": {
            "routingsBasedOn": "DEPARTURE_DATE",
            "earliestTime": EARLIEST,
            "latestTime": LATEST,
        },
        "cargo": {"cargoType": "DRY", "isTemperatureControlRequired": False},
        "carriage": {"vessel": {"flagCountryCode": ""}},
        "equipment": {
            "equipmentSizeCode": "40", "equipmentTypeCode": "HDRY",
            "constructionMaterial": "", "isEmpty": False, "isShipperOwned": False,
        },
        "IsUseOfInternetMarkedRoutesOnly": False,
    }


# =========================================================================
# Location-code enrichment (from v3 / v1)
# =========================================================================
CITY_CODES_FILE = CARRIER_DIR / "city_codes.json"      # shared with v1/v3
LOC_BASE_URL = "https://api.maersk.com/synergy/reference-data/geography/locations/"
LOC_DELAY = 0.5
LOC_HEADERS = {
    "Consumer-Key": HEADERS["consumer-key"],
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Referer": "https://www.maersk.com/",
    "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _atomic_write_json(data, path):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def _collect_codes(obj, found):
    """Recursively gather every 'alternativeCode' value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "alternativeCode":
                found.add(v)
            else:
                _collect_codes(v, found)
    elif isinstance(obj, list):
        for it in obj:
            _collect_codes(it, found)


def _apply_codes(obj, mapping):
    """Rewrite 'alternativeCode' values in place using mapping."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "alternativeCode" and v in mapping:
                obj[k] = mapping[v]
            else:
                _apply_codes(v, mapping)
    elif isinstance(obj, list):
        for it in obj:
            _apply_codes(it, mapping)


def enrich_location_codes(raw_dir):
    """Resolve GEO_ID codes -> city names (cached in city_codes.json) and rewrite
    them inside every MSK_*.json in raw_dir, in place, before canonical building."""
    files = [raw_dir / f for f in os.listdir(raw_dir)
             if f.startswith("MSK_") and f.endswith(".json")]
    if not files:
        return

    cache = {}
    if CITY_CODES_FILE.exists():
        with CITY_CODES_FILE.open("r", encoding="utf-8") as f:
            cache = json.load(f)

    # 1. collect every code seen in the raw responses
    codes, docs = set(), {}
    for fp in files:
        with fp.open("r", encoding="utf-8") as f:
            docs[fp] = json.load(f)
        _collect_codes(docs[fp], codes)

    # 2. resolve codes not yet resolved (absent or cached as None — v1 retries None)
    pending = [c for c in codes if cache.get(c) is None]
    print(f"🗺️ {len(codes)} codes seen, {len(pending)} to resolve via geography API")
    for code in pending:
        try:
            r = requests.get(LOC_BASE_URL + str(code), headers=LOC_HEADERS, timeout=10)
            cache[code] = r.json().get("cityName") if r.status_code == 200 else None
        except Exception as e:
            cache[code] = None
            print(f"  ❌ {code}: {e}")
        _atomic_write_json(cache, CITY_CODES_FILE)
        time.sleep(LOC_DELAY)

    # 3. rewrite codes -> names inside each raw file
    for fp, doc in docs.items():
        _apply_codes(doc, cache)
        with fp.open("w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"✅ enriched {len(files)} raw file(s)")


# =========================================================================
# Scrape (v3 multi-sweep retry)
# =========================================================================
def _scrape_pair(pol_name, pod_name, row, stats):
    """One MSK POST for a pair; save a wrapped raw file on success.

    Returns (status, out_path). status is one of:
      'done'              -> routings saved
      'no_records'        -> 200 but no routings (a real empty answer)
      'skipped_not_found' -> POL or POD has no MSK GEO_ID
      'pending'           -> TRANSIENT failure (403/429/5xx / timeout / conn) — requeue
      'error_<code>'      -> a non-transient 4xx (surfaced, not requeued)
    """
    pol_locs = get_locations(pol_name)
    pod_locs = get_locations(pod_name)
    if not pol_locs or not pod_locs:
        print(f"⚠️ Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found", None

    pol, pod = pol_locs[0], pod_locs[0]
    stats["calls"] += 1
    try:
        resp = requests.post(URL, headers=HEADERS, json=make_payload(pol["code"], pod["code"]), timeout=40)
    except requests.RequestException as e:
        print(f"💥 {pol_name} → {pod_name}: {e} (transient → requeue)")
        return "pending", None

    code = resp.status_code
    print(f"📡 {pol_name}({pol['code']}) → {pod_name}({pod['code']}): {code}")
    if code != 200:
        return ("pending" if code in (403, 429) or code >= 500 else f"error_{code}"), None

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print(f"⚠️ Bad JSON for {pol_name} → {pod_name}")
        return "error_badjson", None

    routings = data.get("routings", [])
    if not routings:
        print(f"⚪ No routings for {pol_name} → {pod_name}")
        return "no_records", None

    wrapped = {
        "query_date": query_timestamp,
        "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
        "PortOfLoading": pol_name,
        "LastCY": pod_name,
        "OFQ": row.get("ID"),
        "FinalDestination": row.get("Final Destination"),
        "routings": routings,
    }
    pol_short = pol_name.replace(" ", "")[:5]
    pod_short = pod_name.replace(" ", "")[:5]
    out = get_unique_path(RAW_DIR / f"MSK_{pol_short}_{pod_short}_{filename_timestamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(wrapped, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved {len(routings)} routings → {out}")
    return "done", out


def scrape_matrix(quotes):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Stateless
    HTTP, so no session bootstrap; transient failures just requeue for the next sweep
    after an escalating cooldown. A whole sweep that resolves nothing aborts."""
    stats = {"calls": 0}                       # total HTTP requests to the endpoint
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status, out = _scrape_pair(row["Port of Loading"], row["LastCY"], row, stats)
            quotes.at[idx, "status"] = status
            if out is not None:
                quotes.at[idx, "result_file"] = str(out)
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — MSK unresponsive; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Run summary to the log: totals, wall-clock, throughput. `elapsed` is full
    scrape wall-clock (includes any mid-run cooldowns)."""
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
        print(f"⚠️ {pending} pair(s) still pending after {MAX_SWEEPS} sweeps (no raw file for them).")


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
quotes = build_quotes([(o, p) for o in origins for p in port_universe], "V2")
print(f"✅ Query matrix built: {len(origins)} origins × {len(port_universe)} ports = {len(quotes)} pairs.")

try:
    scrape_matrix(quotes)

    # --- enrich GEO_ID codes -> names in the raw responses, before canonical build ---
    print("\n=== ENRICH LOCATION CODES ===")
    enrich_location_codes(RAW_DIR)

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"MSKv2_unresolved_ports_{today_str}.csv")
        safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
        print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise


# === AFTER LOOP: build canonical records (one per query) + push ===
written = []
for file in os.listdir(RAW_DIR):
    if file.startswith("MSK_") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is None:
            continue
        pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
        last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
        out = get_unique_path(CANONICAL_DIR / f"MSK_{pol5}_{last5}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        written.append(out)

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("MSK", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
