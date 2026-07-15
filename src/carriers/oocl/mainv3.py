import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# OOCL v3 ORCHESTRATOR (single run, single Playwright session):
#
#   1. inland scrape   (origins × inland yards)        -> exact inland canonicals
#   2. derive ocean    (truncate at discharge port)    -> port-to-port canonicals
#   3. missing-ports   (port universe − observed PODs) -> per-origin pure-ocean gaps
#   4. secondary scrape(origins × missing ports)       -> the pure-ocean ports
#   5. push everything to Supabase, at the very end
#
# OOCL = patchright/Playwright browser driven: navigate the sailing-schedules
# page and intercept the searchHubToHubRoute POST response. Like HMM the session
# IS the live browser, so ONE browser is kept alive across both scrape passes and
# closed once. Runs in the `patch` conda env. Ports come back as facility names,
# so NO location-code enrichment is needed. coverage.json is READ-ONLY (only
# type=="port" / type=="inland" keys are read). Codes come from mapping.json (flat,
# one locationid per name — nothing to explode).
#
# Transient failures (page timeout / navigation / 5xx / 429) are re-queued across
# sweeps (like ONE) so a flaky navigation doesn't silently drop a pair; a whole
# sweep that resolves nothing aborts (site down).
#
# LA/Long Beach & Seattle/Tacoma: only Los Angeles and Seattle are in the yard set;
# normalize_pod keeps each pair distinct, so any Long Beach/Tacoma voyages arrive
# as distinct derive-only discharge ports and are never queried separately.

import json
import time
import random
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_oocl_code,
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

progress_file = get_unique_filename(LOG_DIR / f"OOCLv3_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"OOCL_v3_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage.json"

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
inland_dests = [name for name, meta in coverage.items() if meta.get("type") == "inland"]
port_universe = {name for name, meta in coverage.items() if meta.get("type") == "port"}

# --- API / browser config ---
LANDING_URL = (
    "https://moc.oocl.com/nj_prs_wss/#/sailing_schedules/search"
    "?PREFER_LANGUAGE=en-US&originId={origin_id}&destinationId={destination_id}"
)
# Warmup uses any known valid pair (Cartagena -> Newark from ooclLoopflow defaults).
WARMUP_URL = LANDING_URL.format(origin_id=461796493418770, destination_id=461802935877065)
API_MATCH = "searchHubToHubRoute"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

DELAY_RANGE = (3, 8)                          # between distinct pairs within a sweep
MAX_SWEEPS = 6                                # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]     # seconds to wait before each requeue sweep
# Statuses treated as transient (re-queued for a later sweep).
_TRANSIENT = {"error_timeout", "error_navigation", "error_http_429",
              "error_http_500", "error_http_502", "error_http_503", "error_http_504"}


def fetch_oocl_schedules(page, origin_id, destination_id):
    """Drive the OOCL sailing-schedules page and intercept the searchHubToHubRoute
    response. Returns (status, schedules) where status is 'ok', 'no_records', or an
    'error_*' string."""
    target = LANDING_URL.format(origin_id=origin_id, destination_id=destination_id)
    try:
        with page.expect_response(
            lambda r: API_MATCH in r.url and r.request.method == "POST",
            timeout=30_000,
        ) as resp_info:
            page.goto("about:blank")
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        response = resp_info.value
    except PlaywrightTimeoutError:
        print(f"  Timeout waiting for {API_MATCH} response")
        return "error_timeout", []
    except Exception as e:
        print(f"  Exception during OOCL fetch: {e}")
        return "error_navigation", []

    if response.status != 200:
        print(f"  {API_MATCH} returned {response.status}")
        return f"error_http_{response.status}", []

    try:
        body = response.json()
    except Exception as e:
        print(f"  Could not parse JSON body: {e}")
        return "error_navigation", []

    if not body.get("success", False):
        err = body.get("errorInfo") or body.get("errorInfoDTO") or "unknown"
        print(f"  API success=false ({err})")
        return "no_records", []

    data = body.get("data") or {}
    schedules = data.get("standardRoutes") or []
    return ("ok" if schedules else "no_records"), schedules


# =========================================================================
# Reusable scrape + canonical build
# =========================================================================
def _scrape_pair(page, pol_name, pod_name, row, raw_dir, label):
    """Attempt one (origin, dest) pair ONCE. Save a wrapped raw file on success.

    Returns a status string:
      'done'              -> schedules saved
      'no_records'        -> the query returned nothing (a real empty answer)
      'skipped_not_found' -> no OOCL locationid for POL or POD
      'pending'           -> TRANSIENT failure (timeout / navigation / 5xx / 429)
                             — leave pending so the next sweep retries it
      'error_*'           -> non-transient failure (surface, don't requeue)
    """
    pol_code = get_oocl_code(pol_name)
    pod_code = get_oocl_code(pod_name)
    if not pol_code or not pod_code:
        miss = []
        if not pol_code:
            miss.append(f"POL '{pol_name}'")
        if not pod_code:
            miss.append(f"POD '{pod_name}'")
        print(f"⚠️ [{label}] No OOCL locationid for {', '.join(miss)}")
        return "skipped_not_found"

    print(f"[{label}] {pol_name} ({pol_code}) -> {pod_name} ({pod_code})")
    status, schedules = fetch_oocl_schedules(page, pol_code, pod_code)

    if status == "ok":
        wrapped = {
            "query_date": query_timestamp,
            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "LastCY": pod_name,
            "OFQ": row.get("ID"),
            "FinalDestination": row.get("Final Destination"),
            "PortOfLoading": pol_name,
            "schedules": schedules,
        }
        pol_short = pol_name.replace(" ", "")[:5]
        pod_short = pod_name.replace(" ", "")[:5]
        out = get_unique_path(
            raw_dir / f"OOCL_{pol_short}_{pod_short}_{pol_code}_{pod_code}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, ensure_ascii=False, indent=2)
        print(f"  ✅ [{label}] Saved {len(schedules)} schedules -> {out}")
        return "done"

    if status == "no_records":
        print(f"  ⚪ [{label}] No schedules for {pol_code} -> {pod_code}")
        return "no_records"

    if status in _TRANSIENT:
        print(f"  ↻ [{label}] transient {status} for {pol_code} -> {pod_code} → requeue")
        return "pending"

    return status   # non-transient error (e.g. error_http_403) — surface, don't requeue


def scrape_matrix(page, quotes, raw_dir, label):
    """Multi-sweep drain of the pair matrix. Sweep 1 attempts every pending pair;
    any that hits a transient failure (timeout / navigation / 5xx / 429) stays
    'pending' and is re-attempted on the next sweep after an escalating cooldown.
    Successes are written to raw_dir as they happen. Stops when nothing is pending,
    after MAX_SWEEPS, or when a whole sweep resolves nothing (site down) — leftover
    pairs stay 'pending' and simply produce no raw file."""
    raw_dir.mkdir(parents=True, exist_ok=True)

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- [{label}] sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")

        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(page, row["Port of Loading"], row["LastCY"], row, raw_dir, label)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- [{label}] sweep {sweep} done: {resolved} resolved, {still} still pending ---")

        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 [{label}] zero progress this sweep — OOCL looks unreachable; "
                  f"stopping with {still} pair(s) left pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 [{label}] cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    left = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
    if left:
        print(f"⚠️ [{label}] {left} pair(s) still pending after {MAX_SWEEPS} sweeps "
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
    """build_canonical_record for every raw OOCL_*.json -> canonical_dir.
    drop_conflated=True (secondary pass) applies the conflated-port guard."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    written, dropped = [], 0
    for file in os.listdir(raw_dir):
        if file.startswith("OOCL_") and file.endswith(".json"):
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
            out = get_unique_path(canonical_dir / f"OOCL_{pol5}_{last5}_{filename_timestamp}.json")
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
# ORCHESTRATION (one browser, kept alive across both passes)
# =========================================================================
inland_quotes = build_quotes([(o, d) for o in origins for d in inland_dests], "V3")
secondary_quotes = None

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=False)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
    )
    page = context.new_page()

    print("Warming OOCL session...")
    page.goto(WARMUP_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)

    try:
        # --- 1. inland scrape ---
        print(f"\n=== INLAND: {len(origins)} origins × {len(inland_dests)} yards = {len(inland_quotes)} pairs ===")
        scrape_matrix(page, inland_quotes, RAW_DIR, "inland")
        build_canonicals(RAW_DIR, CANONICAL_DIR)

        # --- 2. derive ocean (no push) ---
        print("\n=== DERIVE OCEAN ===")
        derive_ocean(CANONICAL_DIR, OCEAN_DIR, ports=port_universe)

        # --- 3. missing-ports diff (per origin), read-only on coverage ---
        observed = defaultdict(set)
        for fp in CANONICAL_DIR.glob("OOCL_*.json"):
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
            resolvable = [p for p in missing if get_oocl_code(p)]
            unresolved_missing |= (set(missing) - set(resolvable))
            print(f"  {o}: {len(missing)} missing → {len(resolvable)} queryable: {resolvable}")
            sec_rows += [(o, p) for p in resolvable]
        if unresolved_missing:
            print(f"⚠️ missing ports without an OOCL code: {sorted(unresolved_missing)}")

        # --- 4. secondary scrape (same browser) ---
        secondary_quotes = build_quotes(sec_rows, "V3S")
        print(f"\n=== SECONDARY: {len(secondary_quotes)} (origin, pure-ocean port) pairs ===")
        if not secondary_quotes.empty:
            scrape_matrix(page, secondary_quotes, RAW_SECONDARY_DIR, "secondary")
            build_canonicals(RAW_SECONDARY_DIR, SECONDARY_DIR, drop_conflated=True)
        else:
            print("  (no missing ports — nothing to scrape)")

        unresolved = get_unresolved()
        if unresolved:
            uf = get_unique_filename(LOG_DIR / f"OOCLv3_unresolved_ports_{today_str}.csv")
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
    finally:
        browser.close()    # close smoothly, once


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
        ingest_new_canonicals("OOCL", canonical_dir=cdir, ledger_path=TEMP_DIR / ledger)
    except Exception as e:
        print(f"⚠️ Supabase push failed for {cdir.name} (non-fatal): {e}")

print("✅ v3 run complete.")
