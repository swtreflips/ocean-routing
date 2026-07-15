#!/usr/bin/env python3
import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# MSC v3 ORCHESTRATOR (single run):
#
#   1. port scrape  (origins × port coverage)  -> port-to-port canonicals
#   2. push everything to Supabase, at the very end
#
# MSC is PORT-TO-PORT ONLY — it publishes no inland/rail schedules — so unlike the
# other carriers there is NO inland scrape, NO derive, and NO secondary pass. v3
# just takes each origin and loops it over the port universe (coverage.json).
#
# LA/Long Beach & Miami/Port Everglades: MSC treats each as a separate PortId and
# a separate API call. msc_cities.json (v1) nests the two co-located ports under
# one key; msc_citiesv3.json EXPLODES them so each port is its own key -> its own
# call -> its own name. coverage.json lists all four distinctly. v1 is untouched.

import json
import time
import random
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, timezone

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

TEMP_DIR = ASSETS_DIR / "temp_v3"
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")

progress_file = get_unique_filename(LOG_DIR / f"MSCv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"MSC_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"
cities_file = ASSETS_DIR / "msc_citiesv3.json"      # exploded (LA/LGB, MIA/PEF split)

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
coverage = json.loads(coverage_file.read_text(encoding="utf-8"))["coverage"]
port_universe = [name for name, meta in coverage.items() if meta.get("type") == "port"]
with open(cities_file, "r", encoding="utf-8") as f:
    msc_cities = json.load(f)

# --- Config / API (mirrors main.py) ---
DELAY_RANGE = (1, 2.5)
MAX_RETRIES = 3
EARLIEST = today_iso
FROM_DATE = (today + timedelta(days=1)).strftime("%Y-%m-%d")  # MSC rejects same-day departures
snapshot_date = assign_snapshot(EARLIEST)

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


def fetch_route(state, payload, pol_name, pod_name):
    """POST via the primed session; re-prime on 401/403. Returns data or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = state["session"].post(MSC_URL, headers=HEADERS, json=payload, timeout=30)
            if resp.status_code != 200:
                print(f"⚠️ Attempt {attempt+1}: {resp.status_code} for {pol_name} → {pod_name}")
                if resp.status_code in (401, 403):
                    print("🔄 Re-priming session...")
                    state["session"] = bootstrap_session()
                time.sleep(random.uniform(2, 5))
                continue
            try:
                data = resp.json()
            except ValueError:
                print(f"⚠️ Non-JSON for {pol_name} → {pod_name}: {resp.text[:200]!r}")
                return None
            if not data.get("IsSuccess") or isinstance(data.get("Data"), str):
                msg = data.get("Message") or data.get("ErrorMessage") or str(data.get("Data"))[:150]
                print(f"🚫 No results for {pol_name} → {pod_name}  (msg={msg!r})")
                return None
            if data.get("Data"):
                print(f"✅ {pol_name} → {pod_name}: {len(data['Data'])} route(s)")
                return data
            print(f"🚫 Empty Data for {pol_name} → {pod_name}")
            return None
        except Exception as e:
            print(f"⚠️ Error attempt {attempt+1} for {pol_name} → {pod_name}: {e}")
            time.sleep(random.uniform(3, 6))
    print(f"❌ All retries failed for {pol_name} → {pod_name}")
    return None


# =========================================================================
# Scrape + canonical build
# =========================================================================
def scrape_matrix(state, quotes, raw_dir, label):
    """POST the MSC API per (origin, port) row; save wrapped JSON to raw_dir."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for idx, row in quotes.iterrows():
        if row["status"] != "pending":
            continue
        pol_name, pod_name = row["Port of Loading"], row["LastCY"]
        pol_entries, pod_entries = get_ports(pol_name), get_ports(pod_name)
        if not pol_entries or not pod_entries:
            quotes.at[idx, "status"] = "skipped_not_found"
            print(f"⚠️ [{label}] Missing codes for {pol_name} or {pod_name}")
            continue
        success = False
        for pol in pol_entries:
            for pod in pod_entries:
                data = fetch_route(state, make_payload(pol["PortId"], pod["PortId"]), pol_name, pod_name)
                if data is None:
                    continue
                success = True
                wrapped = {
                    "query_date": query_timestamp,
                    "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                    "PortOfLoading": pol_name,
                    "LastCY": pod.get("LocationName"),   # e.g. "LONG BEACH" / "PORT EVERGLADES"
                    "OFQ": row.get("ID"),
                    "FinalDestination": row.get("Final Destination"),
                    "Data": data.get("Data", []),
                }
                pol_short = (pol_name or "").replace(" ", "")[:5]
                pod_short = (wrapped["LastCY"] or "").replace(" ", "")[:5]
                out = get_unique_path(raw_dir / f"MSC_{pol_short}_{pod_short}_{filename_timestamp}.json")
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(wrapped, f, ensure_ascii=False, indent=2)
                print(f"✅ [{label}] Saved {len(wrapped['Data'])} routes → {out}")
                time.sleep(random.uniform(*DELAY_RANGE))
        quotes.at[idx, "status"] = "done" if success else "no_records"


def build_canonicals(raw_dir, canonical_dir):
    """build_canonical_record for every raw MSC_*.json -> canonical_dir."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for file in os.listdir(raw_dir):
        if file.startswith("MSC") and file.endswith(".json"):
            rec = build_canonical_record(os.path.join(raw_dir, file))
            if rec is None:
                continue
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(canonical_dir / f"MSC_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
            written.append(out)
    print(f"✅ Wrote {len(written)} canonical(s) → {canonical_dir}")
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
port_quotes = build_quotes([(o, p) for o in origins for p in port_universe], "V3")
state = {"session": bootstrap_session()}

try:
    print(f"\n=== PORT SCRAPE: {len(origins)} origins × {len(port_universe)} ports = {len(port_quotes)} pairs ===")
    scrape_matrix(state, port_quotes, RAW_DIR, "ports")
    build_canonicals(RAW_DIR, CANONICAL_DIR)

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"MSCv3_unresolved_ports_{today_str}.csv")
        safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
        print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(port_quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}  (no push)")
    raise


# --- push everything, at the very end (clean finish only) ---
print("\n=== PUSH (end of run) ===")
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("MSC", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_ports.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ v3 run complete.")
