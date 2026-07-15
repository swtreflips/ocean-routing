import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# ZIM v3 ORCHESTRATOR (single run):
#
#   1. inland scrape    origins × inland yards          -> exact inland canonicals
#   2. derive ocean     truncate each at discharge port -> port-to-port canonicals
#   3. missing ports    port universe − observed PODs   -> per-origin pure-ocean gaps
#   4. secondary scrape origins × missing ports         -> the pure-ocean ports
#   5. push everything to Supabase — at the very end, on a clean finish only
#
# ZIM = JSON GET API (apigw.zim.com/digitalSchedules/PointToPoint/v2,
# subscription-key). apigw.zim.com is fronted by Akamai Bot Manager, which
# rejects cookie-less non-browser clients at the TLS level (403 "Access Denied" /
# errors.edgesuite.net) — so like COS we bootstrap cookies with a real Chrome
# (undetected_chromedriver, VISIBLE window: headless gets denied and harvests 0
# cookies), quit Chrome, and reuse the cookies in requests; re-bootstrap on 403.
# v2 response still contains the v1-shaped `routes` (edge legs), so the v1 parser
# (utils.build_canonical_record) works unchanged; v2 adds midPoints/emissions data
# we don't consume. Ports come back as names (portArrivalName), so NO location-code
# enrichment. coverage.json is READ-ONLY (only type=="port" / type=="inland" keys
# are read). zim_cities.json is flat (one entry per key — nothing to explode); we
# use each entry's shortPortName as the effective port name and its stored portCode
# suffix (';10' marine / ';0' land) verbatim, mirroring the live UI.
#
# Transient failures (429 / 5xx / timeout / conn error) are re-queued across sweeps
# (like ONE) with an escalating cooldown; a whole sweep that resolves nothing aborts.
#
# LA/Long Beach & PNW: only Los Angeles is in the yard set (no Long Beach, no
# Seattle/Tacoma); normalize_pod keeps them distinct, so any such voyages appear
# only as derive-only distinct discharge ports (never queried), like HPL Long Beach.

import json
import time
import random
import requests
import pandas as pd
import undetected_chromedriver as uc
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# Silence undetected_chromedriver's __del__ cleanup (see COS v2 for rationale).
uc.Chrome.__del__ = lambda self: None

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

progress_file = get_unique_filename(LOG_DIR / f"ZIMv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"ZIM_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"
cities_file = ASSETS_DIR / "zim_cities.json"

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
inland_dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
port_universe = {name for name, meta in coverage.items() if meta.get("type") == "port"}

with open(cities_file, "r", encoding="utf-8") as f:
    zim_cities = json.load(f)


def get_locations(port_name):
    """Return [{"code", "name"}] for a port; [] if unknown or code-less.
    code = the FULL stored portCode incl. its ';N' suffix (';10' marine, ';0' land)
    — the live UI sends the stored suffix verbatim, so we do too (v1 forced ';10').
    name = shortPortName (the effective port name ZIM uses, mirroring v1).
    zim_cities.json is flat; the list branch is kept only as a defensive fallback."""
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
FROM_DATE_STR = today.strftime("%d-%B-%Y")     # e.g. "05-July-2026"

DELAY_RANGE = (2, 4.5)                         # between distinct pairs within a sweep
MAX_SWEEPS = 6                                 # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]      # seconds to wait before each requeue sweep


def make_params(pol_code, pod_code):
    return {
        "PortCode": pol_code,                  # full stored code incl. ';N' suffix
        "PortDestinationCode": pod_code,
        "Direction": "true",
        "FromDate": FROM_DATE_STR,
        "WeeksAhead": "4",
        "CountryCode": "US",
        "CargoType": "true",
        "EmissionsType": "true",
        "subscription-key": API_KEY,
    }


def get_new_session():
    """Bootstrap Akamai cookies with a real Chrome (COS pattern): launch
    undetected_chromedriver, visit zim.com so Akamai Bot Manager issues its
    cookies (ak_bmsc / bm_sv / bm_sz / _abck), grab cookies + real UA, quit
    Chrome. VISIBLE window on purpose — headless is denied and harvests nothing.
    Returns {"cookies": {name: value}, "headers": {...}}."""
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


# =========================================================================
# Reusable scrape + canonical build
# =========================================================================
def _scrape_pair(pol_name, pod_name, row, raw_dir, label, sweep_state):
    """Attempt one (origin, dest) pair ONCE. Save a wrapped raw file on success.

    On a 403/401 (Akamai cookies expired/rejected) the creds are re-bootstrapped
    — at most once per sweep, tracked in sweep_state — and the request retried;
    a 403 that survives fresh creds goes back to 'pending' for the next sweep.

    Returns a status string:
      'done'              -> schedules saved
      'no_records'        -> HTTP 200 but no routes (a real "nothing here" answer)
      'skipped_not_found' -> no ZIM code for POL or POD
      'pending'           -> TRANSIENT failure (403/401 / 429 / 5xx / timeout)
                             — leave pending so the next sweep retries it
      'error_<code>'      -> non-transient HTTP/parse error (surface, don't requeue)
    """
    pol_locations = get_locations(pol_name)
    pod_locations = get_locations(pod_name)
    if not pol_locations or not pod_locations:
        print(f"⚠️ [{label}] Missing codes for {pol_name} or {pod_name}")
        return "skipped_not_found"

    last_status = "pending"
    for pol in pol_locations:
        for pod in pod_locations:
            pod_display = pod["name"]
            params = make_params(pol["code"], pod["code"])

            code, resp = None, None
            for attempt in (1, 2):
                try:
                    resp = requests.get(API_URL, headers=creds["headers"], params=params,
                                        cookies=creds["cookies"], timeout=40)
                except requests.RequestException as e:
                    print(f"💥 [{label}] {pol_name} → {pod_display}: {e} (transient → requeue)")
                    code = None
                    break
                code = resp.status_code
                print(f"📡 [{label}] {pol_name}({pol['code']}) → {pod_display}({pod['code']}): {code}")
                if code in (401, 403) and attempt == 1 and not sweep_state["rebooted"]:
                    sweep_state["rebooted"] = True
                    print(f"⛔ [{label}] {code} — Akamai rejected creds; re-bootstrapping session...")
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
                print(f"⚠️ [{label}] Bad JSON for {pol_name} → {pod_display}")
                last_status = "error_badjson"
                continue

            if not data.get("routes"):
                print(f"⚪ [{label}] No routes for {pol_name} → {pod_display}")
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
            out = get_unique_path(raw_dir / f"ZIM_{pol_short}_{pod_short}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(wrapped, f, ensure_ascii=False, indent=2)
            print(f"✅ [{label}] Saved {len(data.get('routes'))} route(s) → {out}")
            return "done"

    return last_status


def scrape_matrix(quotes, raw_dir, label):
    """Multi-sweep drain of the pair matrix (handles ZIM rate-limiting / hiccups).

    Sweep 1 attempts every pending pair; any that hits a transient failure
    (429 / 5xx / timeout) stays 'pending' and is re-attempted on the next sweep
    after an escalating cooldown. Successes are written to raw_dir as they happen.
    Stops when nothing is pending, after MAX_SWEEPS, or when a whole sweep resolves
    nothing (server saturated) — leftover pairs stay 'pending' (no raw file)."""
    raw_dir.mkdir(parents=True, exist_ok=True)

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- [{label}] sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")

        sweep_state = {"rebooted": False}   # one Akamai re-bootstrap allowed per sweep
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(row["Port of Loading"], row["LastCY"], row, raw_dir, label, sweep_state)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- [{label}] sweep {sweep} done: {resolved} resolved, {still} still throttled ---")

        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 [{label}] zero progress this sweep — ZIM looks saturated; "
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
    """build_canonical_record for every raw ZIM_*.json -> canonical_dir.
    drop_conflated=True (secondary pass) applies the conflated-port guard."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    written, dropped = [], 0
    for file in os.listdir(raw_dir):
        if file.startswith("ZIM_") and file.endswith(".json"):
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
            out = get_unique_path(canonical_dir / f"ZIM_{pol5}_{last5}_{filename_timestamp}.json")
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

# Bootstrap ONCE; the same creds serve both passes (re-bootstrapped on 403).
creds = get_new_session()

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
    for fp in CANONICAL_DIR.glob("ZIM_*.json"):
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
        print(f"⚠️ missing ports without a ZIM code: {sorted(unresolved_missing)}")

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
        uf = get_unique_filename(LOG_DIR / f"ZIMv3_unresolved_ports_{today_str}.csv")
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
        ingest_new_canonicals("ZIM", canonical_dir=cdir, ledger_path=TEMP_DIR / ledger)
    except Exception as e:
        print(f"⚠️ Supabase push failed for {cdir.name} (non-fatal): {e}")

print("✅ v3 run complete.")
