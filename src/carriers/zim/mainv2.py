import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# ZIM v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like the other v2 carriers: origins x
# type=="port" coverage, the v3 multi-sweep retry, a run-stats summary in the log,
# and a LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# ZIM mechanics: JSON GET API (apigw.zim.com/digitalSchedules/PointToPoint/v2,
# subscription-key) behind Akamai Bot Manager — cookie-less requests get a 403, so
# get_new_session() bootstraps Akamai cookies with a real Chrome (undetected_
# chromedriver, VISIBLE window — headless is denied). The sweep re-bootstraps the
# session at most once per sweep on a 403/401; a 403 that survives fresh cookies
# requeues. Cities from zim_cities.json (flat, shared read-only); get_locations uses
# each entry's shortPortName as the effective port name (and sends the stored ';N'
# portCode suffix verbatim). coverage_v2.json is v2's OWN keys+type coverage (12
# ports) — independent of v3's coverage.json (Newark, NJ is a PORT here — it's the
# NY/NJ port USNYC / "New York, NY", queried directly with no derive/guard).

import json
import time
import random
import requests
import pandas as pd
import undetected_chromedriver as uc
from pathlib import Path
from datetime import datetime, timezone, date

# Silence undetected_chromedriver's __del__ cleanup (see COS v2 for rationale).
uc.Chrome.__del__ = lambda self: None

from utils import (
    get_unique_filename,
    get_unique_path,
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
today = date.today()                         # LOCAL date -> query window (FromDate)
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"ZIMv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"ZIM_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage — independent of v3's coverage.json
cities_file = ASSETS_DIR / "zim_cities.json"        # flat, shared read-only with v1/v3

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

with open(cities_file, "r", encoding="utf-8") as f:
    zim_cities = json.load(f)


def get_locations(port_name):
    """Return [{"code", "name"}] for a port; [] if unknown. code = the FULL stored
    portCode incl. its ';N' suffix; name = shortPortName (the effective port name)."""
    entry = zim_cities.get(port_name)
    if not entry:
        return []
    entries = entry if isinstance(entry, list) else [entry]
    out = []
    for e in entries:
        pc = e.get("portCode")
        if pc:
            out.append({"code": pc, "name": e.get("shortPortName") or port_name})
    return out


# --- API config ---
API_URL = "https://apigw.zim.com/digitalSchedules/PointToPoint/v2"
API_KEY = "9d63cf020a4c4708a7b0ebfe39578300"
BOOTSTRAP_URL = "https://www.zim.com/"
FROM_DATE_STR = today.strftime("%d-%B-%Y")     # e.g. "12-July-2026" (LOCAL date)
DELAY_RANGE = (2, 4.5)                          # between pairs within a sweep (tunable)
MAX_SWEEPS = 6                                  # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]       # seconds before each requeue sweep


def make_params(pol_code, pod_code):
    return {
        "PortCode": pol_code, "PortDestinationCode": pod_code,
        "Direction": "true", "FromDate": FROM_DATE_STR, "WeeksAhead": "4",
        "CountryCode": "US", "CargoType": "true", "EmissionsType": "true",
        "subscription-key": API_KEY,
    }


def get_new_session():
    """Bootstrap Akamai cookies with a real Chrome (COS pattern): launch
    undetected_chromedriver, visit zim.com so Akamai Bot Manager issues its cookies
    (ak_bmsc / bm_sv / bm_sz / _abck), grab cookies + real UA, quit Chrome. VISIBLE
    window on purpose — headless is denied and harvests nothing."""
    print("🌐 Bootstrapping new ZIM session (Chrome window will open briefly)...")
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1440,900")
    driver = uc.Chrome(version_main=148, options=options)
    try:
        driver.get(BOOTSTRAP_URL)
        time.sleep(8)                           # let Akamai's bm scripts run
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        ua = driver.execute_script("return navigator.userAgent;")
    finally:
        driver.quit()
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "culture": "en-US",
        "origin": "https://www.zim.com",
        "pageid": "16439",
        "priority": "u=1, i",
        "referer": "https://www.zim.com/",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": ua,
    }
    akamai = [n for n in cookies if n in ("ak_bmsc", "bm_sv", "bm_sz", "_abck")]
    print(f"✅ ZIM session established ({len(cookies)} cookies; Akamai: {akamai})")
    return {"cookies": cookies, "headers": headers}


# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(pol_name, pod_name, row, sweep_state, stats):
    """One ZIM query for a pair; save a wrapped raw file on success. On a 403/401
    (Akamai cookies expired/rejected) the creds are re-bootstrapped once per sweep
    and the request retried; a 403 that survives goes back to 'pending' (requeue).

    Returns status: 'done' | 'no_records' | 'skipped_not_found' | 'pending' | 'error_<code>'.
    """
    pol_locations = get_locations(pol_name)
    pod_locations = get_locations(pod_name)
    if not pol_locations or not pod_locations:
        print(f"⚠️ Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found"

    last_status = "pending"
    for pol in pol_locations:
        for pod in pod_locations:
            pod_display = pod["name"]
            params = make_params(pol["code"], pod["code"])

            code, resp = None, None
            for attempt in (1, 2):
                stats["calls"] += 1
                try:
                    resp = requests.get(API_URL, headers=creds["headers"], params=params,
                                        cookies=creds["cookies"], timeout=40)
                except requests.RequestException as e:
                    print(f"💥 {pol_name} → {pod_display}: {e} (transient → requeue)")
                    code = None
                    break
                code = resp.status_code
                print(f"📡 {pol_name}({pol['code']}) → {pod_display}({pod['code']}): {code}")
                if code in (401, 403) and attempt == 1 and not sweep_state["rebooted"]:
                    sweep_state["rebooted"] = True
                    print(f"⛔ {code} — Akamai rejected creds; re-bootstrapping session...")
                    new = get_new_session()
                    creds["cookies"], creds["headers"] = new["cookies"], new["headers"]
                    continue                         # retry same pair with fresh creds
                break

            if code is None:                         # network exception → transient
                last_status = "pending"
                continue
            if code in (401, 403) or code == 429 or code >= 500:   # blocked/throttled → transient
                last_status = "pending"
                continue
            if code != 200:                          # other 4xx → non-transient, surface
                last_status = f"error_{code}"
                continue

            try:
                data = resp.json()
            except json.JSONDecodeError:
                print(f"⚠️ Bad JSON for {pol_name} → {pod_display}")
                last_status = "error_badjson"
                continue

            if not data.get("routes"):
                print(f"⚪ No routes for {pol_name} → {pod_display}")
                last_status = "no_records"
                continue

            wrapped = {
                "query_date": query_timestamp,
                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                "PortOfLoading": pol_name,
                "LastCY": pod_display,               # shortPortName (mirrors v1)
                "OFQ": row.get("ID"),
                "FinalDestination": row.get("Final Destination"),
                "schedules": data,
            }
            pol_short = (pol_name or "").replace(" ", "")[:5]
            pod_short = (pod_name or "").replace(" ", "")[:5]
            out = get_unique_path(RAW_DIR / f"ZIM_{pol_short}_{pod_short}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(wrapped, f, ensure_ascii=False, indent=2)
            print(f"✅ Saved {len(data.get('routes'))} route(s) → {out}")
            return "done"

    return last_status


def scrape_matrix(quotes):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Session is
    re-bootstrapped at most once per sweep on a 403/401; transient failures requeue.
    A whole sweep that resolves nothing aborts."""
    stats = {"calls": 0}                       # total HTTP requests to the endpoint
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        sweep_state = {"rebooted": False}      # one Akamai re-bootstrap allowed per sweep
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(row["Port of Loading"], row["LastCY"], row, sweep_state, stats)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — ZIM saturated/blocked; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Run summary to the log: totals, wall-clock, throughput. `elapsed` is full
    scrape wall-clock (includes any mid-run cooldowns / re-bootstraps)."""
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
quotes = build_quotes([(o, p) for o in origins for p in port_dests], "V2")
print(f"✅ Query matrix built: {len(origins)} origins × {len(port_dests)} ports = {len(quotes)} pairs.")

# Bootstrap Akamai session ONCE; re-bootstrapped inside a sweep on a 403/401.
creds = get_new_session()

try:
    scrape_matrix(quotes)

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"ZIMv2_unresolved_ports_{today_str}.csv")
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
    if file.startswith("ZIM_") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is None:
            continue
        pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
        last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
        out = get_unique_path(CANONICAL_DIR / f"ZIM_{pol5}_{last5}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        written.append(out)

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("ZIM", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
