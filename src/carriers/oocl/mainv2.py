import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# OOCL v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like the other v2 carriers: origins x
# type=="port" coverage, the v3 multi-sweep retry, a run-stats summary in the log,
# and a LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# OOCL mechanics: patchright/Playwright browser driven — navigate the sailing-
# schedules page and intercept the searchHubToHubRoute POST response. The session IS
# the live browser, kept alive for the whole sweep and closed once. Runs in the
# `patch` conda env, headless=False. Ports come back as facility names (no location
# enrichment). Codes come from mapping.json (flat, one locationid per name — nothing
# to explode; shared read-only with v1/v3). coverage_v2.json is v2's OWN bare
# keys+type coverage (12 ports) — independent of v3's coverage.json.
#
# Transient failures (page timeout / navigation / 5xx / 429) requeue across sweeps;
# a whole sweep that resolves nothing aborts (site down).

import json
import time
import random
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, date
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from utils import (
    get_unique_filename,
    get_unique_path,
    assign_snapshot,
    get_oocl_code,
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

progress_file = get_unique_filename(LOG_DIR / f"OOCLv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"OOCL_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage — independent of v3's coverage.json

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

# --- API / browser config ---
LANDING_URL = (
    "https://moc.oocl.com/nj_prs_wss/#/sailing_schedules/search"
    "?PREFER_LANGUAGE=en-US&originId={origin_id}&destinationId={destination_id}"
)
WARMUP_URL = LANDING_URL.format(origin_id=461796493418770, destination_id=461802935877065)
API_MATCH = "searchHubToHubRoute"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

DELAY_RANGE = (3, 8)                            # between pairs within a sweep (tunable)
MAX_SWEEPS = 6                                  # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]       # seconds before each requeue sweep
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


# =========================
# Scrape (v3 multi-sweep retry)
# =========================
def _scrape_pair(page, pol_name, pod_name, row, stats):
    """Attempt one (origin, port) pair ONCE; save a wrapped raw file on success.

    Returns a status string:
      'done' | 'no_records' | 'skipped_not_found' | 'pending' (transient) | 'error_*'.
    """
    pol_code = get_oocl_code(pol_name)
    pod_code = get_oocl_code(pod_name)
    if not pol_code or not pod_code:
        miss = []
        if not pol_code:
            miss.append(f"POL '{pol_name}'")
        if not pod_code:
            miss.append(f"POD '{pod_name}'")
        print(f"⚠️ No OOCL locationid for {', '.join(miss)}")
        return "skipped_not_found"

    print(f"{pol_name} ({pol_code}) -> {pod_name} ({pod_code})")
    stats["calls"] += 1
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
            RAW_DIR / f"OOCL_{pol_short}_{pod_short}_{pol_code}_{pod_code}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, ensure_ascii=False, indent=2)
        print(f"  ✅ Saved {len(schedules)} schedules -> {out}")
        return "done"

    if status == "no_records":
        print(f"  ⚪ No schedules for {pol_code} -> {pod_code}")
        return "no_records"

    if status in _TRANSIENT:
        print(f"  ↻ transient {status} for {pol_code} -> {pod_code} → requeue")
        return "pending"

    return status   # non-transient error (e.g. error_http_403) — surface, don't requeue


def scrape_matrix(page, quotes):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Transient
    failures (timeout / navigation / 5xx / 429) requeue for the next sweep after an
    escalating cooldown. A whole sweep that resolves nothing aborts."""
    stats = {"calls": 0}                       # total page navigations / API intercepts
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")

        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(page, row["Port of Loading"], row["LastCY"], row, stats)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1
            time.sleep(random.uniform(*DELAY_RANGE))

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — OOCL unreachable; stopping with {still} pending.")
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
# ORCHESTRATION (one browser, kept alive across the sweep)
# =========================================================================
quotes = build_quotes([(o, p) for o in origins for p in port_dests], "V2")
print(f"✅ Query matrix built: {len(origins)} origins × {len(port_dests)} ports = {len(quotes)} pairs.")

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
        scrape_matrix(page, quotes)

        unresolved = get_unresolved()
        if unresolved:
            uf = get_unique_filename(LOG_DIR / f"OOCLv2_unresolved_ports_{today_str}.csv")
            safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
            print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")
    except (Exception, KeyboardInterrupt) as e:
        crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
        safe_to_csv(quotes, crash_file, index=False)
        print(f"💥 Run failed: {e}")
        print(f"📋 Partial progress saved to: {crash_file}")
        raise
    finally:
        browser.close()


# === AFTER LOOP: build canonical records (one per query) + push ===
written = []
for file in os.listdir(RAW_DIR):
    if file.startswith("OOCL_") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(RAW_DIR, file))
        if rec is None:
            continue
        pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
        last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
        out = get_unique_path(CANONICAL_DIR / f"OOCL_{pol5}_{last5}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        written.append(out)

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("OOCL", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
