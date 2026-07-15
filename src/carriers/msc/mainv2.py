#!/usr/bin/env python3
import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# MSC v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like COS/EMC/HPL v2: origins x type=="port"
# coverage, the v3 multi-sweep retry, a run-stats summary in the log, and a
# LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# MSC is PORT-TO-PORT ONLY (no inland/rail schedules) — so the v3 script was already
# a plain origins x ports loop; v2 just swaps in the sweep retry + run stats.
# Mechanics: pure requests.Session + a cookie bootstrap (GET the search page); the
# session is re-primed once per sweep on a 401/403. Cities come from
# msc_citiesv3.json (LA/Long Beach and Miami/Port Everglades exploded into separate
# PortId keys — MSC calls each distinctly). coverage.json is a bare keys+type utility
# (all 18 ports distinct; no siblings). v1 (main.py + msc_cities.json) is untouched.

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

progress_file = get_unique_filename(LOG_DIR / f"MSCv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"MSC_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage — independent of v3's coverage.json
cities_file = ASSETS_DIR / "msc_citiesv2.json"      # v2's OWN exploded cities (copy) — independent of v3

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
coverage = json.loads(coverage_file.read_text(encoding="utf-8"))["coverage"]
port_universe = [name for name, meta in coverage.items() if meta.get("type") == "port"]
with open(cities_file, "r", encoding="utf-8") as f:
    msc_cities = json.load(f)

# --- Config / API (mirrors main.py) ---
DELAY_RANGE = (1, 2.5)                          # between pairs within a sweep (tunable)
MAX_SWEEPS = 6                                  # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]       # seconds before each requeue sweep
FROM_DATE = (today + timedelta(days=1)).strftime("%Y-%m-%d")  # MSC rejects same-day departures
snapshot_date = assign_snapshot(today_iso)

MSC_URL = "https://www.msc.com/api/feature/tools/SearchSailingRoutes"
MSC_SEARCH_PAGE = "https://www.msc.com/en/search-a-schedule"
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
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
    """Open a Session and GET the search page so MSC sets the cookies its API expects."""
    session = requests.Session()
    page_headers = {
        **_BROWSER_HEADERS,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none", "sec-fetch-user": "?1", "upgrade-insecure-requests": "1",
    }
    try:
        r = session.get(MSC_SEARCH_PAGE, headers=page_headers, timeout=30)
        print(f"🍪 Bootstrap GET → {r.status_code}, cookies: {len(session.cookies)}")
    except Exception as e:
        print(f"⚠️ Bootstrap GET failed: {e}")
    return session


def get_ports(city_name):
    """Resolve a city to its MSC port entries. In msc_citiesv3.json every key is a
    single dict (co-located ports already exploded), so this returns a 1-item list."""
    entry = msc_cities.get("Ports", {}).get(city_name)
    if not entry:
        return None
    return entry if isinstance(entry, list) else [entry]


def make_payload(pol_id, pod_id):
    return {"FromDate": FROM_DATE, "fromPortId": pol_id, "toPortId": pod_id,
            "language": "en", "dataSourceId": DATA_SOURCE}


# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(state, pol_name, pod_name, row, sweep_state, stats):
    """One MSC POST for a pair; save a wrapped raw file on success. On a 401/403 the
    session is re-primed once per sweep and the call retried; anything that still
    fails goes back to 'pending' for the next sweep.

    Returns (status, out_path). status is one of:
      'done'              -> routes saved
      'no_records'        -> API IsSuccess=false / empty Data (a real empty answer)
      'skipped_not_found' -> POL or POD has no MSC PortId
      'pending'           -> TRANSIENT failure (401/403/429/5xx / exception) — requeue
      'error_<code>'      -> a non-transient 4xx (surfaced, not requeued)
    """
    pol_entries = get_ports(pol_name)
    pod_entries = get_ports(pod_name)
    if not pol_entries or not pod_entries:
        print(f"⚠️ Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found", None

    pol, pod = pol_entries[0], pod_entries[0]      # exploded -> one PortId each
    payload = make_payload(pol["PortId"], pod["PortId"])

    for attempt in (1, 2):
        stats["calls"] += 1
        try:
            resp = state["session"].post(MSC_URL, headers=HEADERS, json=payload, timeout=30)
        except Exception as e:
            print(f"💥 {pol_name} → {pod_name}: {e} (transient → requeue)")
            return "pending", None

        code = resp.status_code
        print(f"📡 {pol_name} → {pod_name}: {code}")
        if code != 200:
            if code in (401, 403) and attempt == 1 and not sweep_state["reprimed"]:
                sweep_state["reprimed"] = True
                print("🔄 re-priming MSC session...")
                state["session"] = bootstrap_session()
                continue
            return ("pending" if code in (401, 403, 429) or code >= 500 else f"error_{code}"), None

        try:
            data = resp.json()
        except ValueError:
            print(f"⚠️ Non-JSON for {pol_name} → {pod_name}: {resp.text[:150]!r}")
            return "error_badjson", None

        if not data.get("IsSuccess") or isinstance(data.get("Data"), str):
            msg = data.get("Message") or data.get("ErrorMessage") or str(data.get("Data"))[:120]
            print(f"🚫 No results for {pol_name} → {pod_name}  (msg={msg!r})")
            return "no_records", None

        routes = data.get("Data") or []
        if not routes:
            print(f"⚪ Empty Data for {pol_name} → {pod_name}")
            return "no_records", None

        wrapped = {
            "query_date": query_timestamp,
            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "PortOfLoading": pol_name,
            "LastCY": pod.get("LocationName"),        # e.g. "LONG BEACH" / "PORT EVERGLADES"
            "OFQ": row.get("ID"),
            "FinalDestination": row.get("Final Destination"),
            "Data": routes,
        }
        pol_short = (pol_name or "").replace(" ", "")[:5]
        pod_short = (wrapped["LastCY"] or "").replace(" ", "")[:5]
        out = get_unique_path(RAW_DIR / f"MSC_{pol_short}_{pod_short}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, ensure_ascii=False, indent=2)
        print(f"✅ Saved {len(routes)} routes → {out}")
        return "done", out

    return "pending", None


def scrape_matrix(state, quotes):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). The session
    is re-primed at most once per sweep on a 401/403; transient failures requeue.
    A whole sweep that resolves nothing aborts."""
    stats = {"calls": 0}                       # total HTTP requests to the endpoint
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        sweep_state = {"reprimed": False}      # one session re-prime allowed per sweep
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status, out = _scrape_pair(state, row["Port of Loading"], row["LastCY"], row, sweep_state, stats)
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
            print(f"🛑 zero progress this sweep — MSC unresponsive; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats["calls"], elapsed)


def _log_run_stats(quotes, calls, elapsed):
    """Run summary to the log: totals, wall-clock, throughput. `elapsed` is full
    scrape wall-clock (includes any mid-run cooldowns / re-primes)."""
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
state = {"session": bootstrap_session()}

try:
    scrape_matrix(state, quotes)

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"MSCv2_unresolved_ports_{today_str}.csv")
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
    if file.startswith("MSC") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is None:
            continue
        pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
        last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
        out = get_unique_path(CANONICAL_DIR / f"MSC_{pol5}_{last5}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        written.append(out)

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("MSC", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
