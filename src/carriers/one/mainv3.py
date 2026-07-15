import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# ONE v3 ORCHESTRATOR (single run):
#
#   1. inland scrape   (origins × inland yards)        -> exact inland canonicals
#   2. derive ocean    (truncate at discharge port)    -> port-to-port canonicals
#   3. missing-ports   (port universe − observed PODs) -> per-origin pure-ocean gaps
#   4. secondary scrape(origins × missing ports)       -> the pure-ocean ports
#   5. push everything to Supabase, at the very end
#
# Ocean Network Express = a clean JSON GET API
# (ecomm.one-line.com/api/v1/schedule/point-to-point) keyed by porCode/delCode —
# no browser, no session bootstrap. Ports come back as names (podName), so NO
# location-code enrichment is needed. coverage.json is READ-ONLY (only type=="port"
# / type=="inland" keys are read). City codes come from one_cities.json (flat).
#
# LA/Long Beach & Seattle/Tacoma: only Los Angeles and Tacoma are in the yard set
# (coverage). utils.normalize_pod keeps Los Angeles/Long Beach and Seattle/Tacoma
# distinct, so any Long Beach/Seattle voyages arrive as distinct derive-only
# discharge ports and are never queried separately.

import json
import time
import random
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    build_canonical_record,
    get_unresolved,
)
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
RAW_DIR = TEMP_DIR / "raw"
CANONICAL_DIR = TEMP_DIR / "canonicals"
OCEAN_DIR = TEMP_DIR / "ocean"
RAW_SECONDARY_DIR = TEMP_DIR / "raw_secondary"
SECONDARY_DIR = TEMP_DIR / "secondary"
LOG_DIR = TEMP_DIR

for _d in (RAW_DIR, CANONICAL_DIR, OCEAN_DIR, RAW_SECONDARY_DIR, SECONDARY_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)
today = run_timestamp.date()
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"ONEv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"ONE_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"
cities_file = ASSETS_DIR / "one_cities.json"

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
inland_dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
port_universe = {name for name, meta in coverage.items() if meta.get("type") == "port"}

with open(cities_file, "r", encoding="utf-8") as f:
    one_cities = json.load(f)


def get_locations(city_name):
    """Return [{"name", "code"}] for a city; [] if unknown or code-less.
    one_cities.json is flat (one entry per key); wrapped in a list to match the
    matrix-scrape shape used by the other v3 carriers."""
    entry = one_cities.get(city_name)
    if not entry or not entry.get("code"):
        return []
    return [{"name": entry.get("name") or city_name, "code": entry["code"]}]


# --- Dynamic dates / API ---
fromDate = today_iso
toDate = (today + timedelta(days=14)).strftime("%Y-%m-%d")
DELAY_RANGE = (2.5, 5)                        # between distinct pairs within a sweep
MAX_SWEEPS = 6                                # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]     # seconds to wait before each requeue sweep
URL = "https://ecomm.one-line.com/api/v1/schedule/point-to-point"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://ecomm.one-line.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
COOKIES = {"sessLocale": "en", "usrCntCd": "US", "AKA_A2": "A"}


def make_params(pol_code, pod_code):
    return {
        "porCode": pol_code,
        "delCode": pod_code,
        "rcvTermCode": "Y",
        "deTermCode": "Y",
        "tsFlag": "",
        "fromDate": fromDate,
        "toDate": toDate,
        "polCode": "",
        "podCode": "",
        "polYardCode": "",
        "podYardCode": "",
        "standardizationEtaEtb": "false",
        "cargoNature": "GP",
        "searchType": "List",
    }


# =========================================================================
# Reusable scrape + canonical build
# =========================================================================
def _scrape_pair(pol_name, pod_name, row, raw_dir, label):
    """Attempt one (origin, dest) pair ONCE. Save a wrapped raw file on success.

    Returns a status string:
      'done'              -> schedules saved
      'no_records'        -> HTTP 200 but empty (a real "nothing here" answer)
      'skipped_not_found' -> no city code for POL or POD
      'pending'           -> TRANSIENT failure (429 / 5xx / timeout / conn error)
                             — leave pending so the next sweep retries it
      'error_<code>'      -> non-transient HTTP/parse error (surface, don't requeue)
    """
    pol_locations = get_locations(pol_name)
    pod_locations = get_locations(pod_name)
    if not pol_locations or not pod_locations:
        print(f"⚠️ [{label}] Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found"

    # ONE has one code per name; the loop is kept for shape-parity with other carriers.
    last_status = "pending"
    for pol in pol_locations:
        for pod in pod_locations:
            params = make_params(pol["code"], pod["code"])
            try:
                resp = requests.get(URL, headers=HEADERS, params=params, cookies=COOKIES, timeout=60)
            except requests.RequestException as e:
                print(f"💥 [{label}] {pol_name} → {pod_name}: {e} (transient → requeue)")
                last_status = "pending"
                continue

            code = resp.status_code
            print(f"📡 [{label}] {pol_name}({pol['code']}) → {pod_name}({pod['code']}): {code}")

            if code == 429 or code >= 500:          # server busy / overloaded → transient
                last_status = "pending"
                continue
            if code != 200:                          # 4xx (e.g. 403) → non-transient, surface
                last_status = f"error_{code}"
                continue

            try:
                data = resp.json()
            except json.JSONDecodeError:
                print(f"⚠️ [{label}] Bad JSON for {pol_name} → {pod_name}")
                last_status = "error_badjson"
                continue

            schedules = data.get("scheduleLines", [])
            if not schedules:
                print(f"⚪ [{label}] No schedules for {pol_name} → {pod_name}")
                last_status = "no_records"
                continue

            wrapped = {
                "query_date": query_timestamp,
                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                "PortofLoading": pol_name,
                "LastCY": pod_name,
                "OFQ": row.get("ID"),
                "FinalDestination": row.get("Final Destination"),
                "schedules": schedules,
            }
            pol_short = pol_name.replace(" ", "")[:5]
            pod_short = pod_name.replace(" ", "")[:5]
            out = get_unique_path(raw_dir / f"ONE_{pol_short}_{pod_short}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(wrapped, f, ensure_ascii=False, indent=2)
            print(f"✅ [{label}] Saved {len(schedules)} schedule line(s) → {out}")
            return "done"

    return last_status


def scrape_matrix(quotes, raw_dir, label):
    """Multi-sweep drain of the pair matrix (handles ONE's load-based 429s).

    Sweep 1 attempts every pending pair, harvesting all the easy wins. Any pair
    that hits a transient failure (429 / 5xx / timeout) stays 'pending' and is
    re-attempted on the next sweep, after an escalating cooldown that gives ONE's
    servers room to recover. Successes are written to raw_dir the moment they
    happen. Stops when nothing is pending, after MAX_SWEEPS, or when a whole
    sweep resolves nothing (server saturated) — leftover pairs stay 'pending'
    and simply produce no raw file."""
    raw_dir.mkdir(parents=True, exist_ok=True)

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- [{label}] sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")

        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(row["Port of Loading"], row["LastCY"], row, raw_dir, label)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- [{label}] sweep {sweep} done: {resolved} resolved, {still} still throttled ---")

        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 [{label}] zero progress this sweep — ONE looks saturated; "
                  f"stopping with {still} pair(s) left pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 [{label}] cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    left = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
    if left:
        print(f"⚠️ [{label}] {left} pair(s) still throttled after {MAX_SWEEPS} sweeps "
              f"(left pending; no raw file written for them).")


def _drop_conflated(rec):
    """Secondary-pass guard: drop DIRECT schedules (eta==pod_eta) that discharge at a
    port other than the queried one — the carrier resolved the query to a different
    port's voyages, already covered. Keeps pure-ocean ports (discharge==queried) and
    rail-served ports (eta!=pod_eta). Returns (kept_schedules, n_dropped)."""
    lc = rec.get("last_cy")
    scheds = rec.get("schedules", [])
    kept = [s for s in scheds
            if s.get("port_of_discharge") == lc or s.get("eta") != s.get("pod_eta")]
    return kept, len(scheds) - len(kept)


def build_canonicals(raw_dir, canonical_dir, drop_conflated=False):
    """build_canonical_record for every raw ONE_*.json -> canonical_dir.
    drop_conflated=True (secondary pass) applies the conflated-port guard."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    written, dropped = [], 0
    for file in os.listdir(raw_dir):
        if file.startswith("ONE_") and file.endswith(".json"):
            rec = build_canonical_record(os.path.join(raw_dir, file))
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
            out = get_unique_path(canonical_dir / f"ONE_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
            written.append(out)
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
    scrape_matrix(inland_quotes, RAW_DIR, "inland")
    build_canonicals(RAW_DIR, CANONICAL_DIR)

    # --- 2. derive ocean (no push) ---
    print("\n=== DERIVE OCEAN ===")
    derive_ocean(CANONICAL_DIR, OCEAN_DIR, ports=port_universe)

    # --- 3. missing-ports diff (per origin), read-only on coverage ---
    observed = defaultdict(set)
    for fp in CANONICAL_DIR.glob("ONE_*.json"):
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
        resolvable = [p for p in missing if get_locations(p)]
        unresolved_missing |= (set(missing) - set(resolvable))
        print(f"  {o}: {len(missing)} missing → {len(resolvable)} queryable: {resolvable}")
        sec_rows += [(o, p) for p in resolvable]
    if unresolved_missing:
        print(f"⚠️ missing ports without ONE codes: {sorted(unresolved_missing)}")

    # --- 4. secondary scrape ---
    secondary_quotes = build_quotes(sec_rows, "V3S")
    print(f"\n=== SECONDARY: {len(secondary_quotes)} (origin, pure-ocean port) pairs ===")
    if not secondary_quotes.empty:
        scrape_matrix(secondary_quotes, RAW_SECONDARY_DIR, "secondary")
        build_canonicals(RAW_SECONDARY_DIR, SECONDARY_DIR, drop_conflated=True)
    else:
        print("  (no missing ports — nothing to scrape)")

    unresolved = get_unresolved()
    if unresolved:
        uf = get_unique_filename(LOG_DIR / f"ONEv3_unresolved_ports_{today_str}.csv")
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
        ingest_new_canonicals("ONE", canonical_dir=cdir, ledger_path=TEMP_DIR / ledger)
    except Exception as e:
        print(f"⚠️ Supabase push failed for {cdir.name} (non-fatal): {e}")

print("✅ v3 run complete.")
