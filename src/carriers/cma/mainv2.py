import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# CMA v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Works like the other v2 carriers: origins x
# type=="port" coverage, the v3 multi-sweep retry, a run-stats summary in the log,
# and a LOCAL-date query window. Pushes the port canonicals to Supabase.
#
# CMA mechanics (HTML carrier, like EMC/WHL): a requests.Session whose cookies/UA are
# bootstrapped from a real Chrome (undetected_chromedriver). POST the routing-finder
# form -> save the HTML -> batch_transform_processing_dir parses HTML->JSON -> canonical.
#
# ⚠️ SESSION LIMIT: a CMA auth session only lasts ~20 requests before it starts 403ing.
# So v2 PROACTIVELY re-creates the session every CALLS_PER_SESSION (17) calls, and also
# reactively on a 403. Each re-creation launches a fresh Chrome to re-auth.
#
# Cities from cma_cities.json (shared read-only); co-located ports are nested under one
# key (LA/Long Beach, Miami/Port Everglades, Seattle/Tacoma) so each gets its own call;
# duplicate same-code entries are deduped so they don't burn the session budget.
# coverage_v2.json is v2's OWN keys+type coverage (14 ports).

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
    get_locations,
    batch_transform_processing_dir,
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

TEMP_DIR = ASSETS_DIR / "temp"            # v2 output (local)
WORK_DIR = TEMP_DIR / "work"              # HTML + parsed JSON during processing
HTML_DIR = TEMP_DIR / "html"              # archived raw HTML
CANONICAL_DIR = TEMP_DIR / "canonicals"
LOG_DIR = TEMP_DIR

for _d in (WORK_DIR, HTML_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)   # UTC — query_date / filenames (audit trail)
today = date.today()                         # LOCAL date -> query window (SearchDate)
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"CMAv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"CMA_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"     # v2's OWN coverage

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

# --- API / session config ---
CMA_URL = "https://www.cma-cgm.com/ebusiness/schedules/routing-finder"
fromDate = today.strftime("%d-%b-%Y")          # SearchDate (LOCAL). %b is locale-dependent (EN Windows OK).
DELAY_RANGE = (0.9, 2.4)                        # between calls — tightened ~3.3x from v1's (3,8), same proportion as other v2s
MAX_SWEEPS = 6                                 # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]      # seconds before each requeue sweep
CALLS_PER_SESSION = 17                         # re-auth before CMA's ~20-call session limit


def create_new_cma_session(headless=False):
    """Bootstrap a requests.Session with cookies/UA from a real Chrome (CMA auth)."""
    options = uc.ChromeOptions()
    if headless:
        options.headless = True
        options.add_argument("--window-size=1920,1080")
    driver = uc.Chrome(options=options, version_main=148)
    try:
        driver.get("https://www.cma-cgm.com/")
        time.sleep(4)
        driver.get(CMA_URL)
        time.sleep(7)
        headers = driver.execute_script(
            'return {"user-agent": navigator.userAgent, "accept-language": navigator.language || "en-US"};')
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "cache-control": "max-age=0",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.cma-cgm.com",
            "referer": CMA_URL,
        })
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    finally:
        driver.quit()
    session = requests.Session()
    session.headers.update(headers)
    for k, v in cookies.items():
        session.cookies.set(k, v)
    return session


def _new_session(state, stats, reason):
    """(Re)create the CMA session and reset the per-session call counter."""
    print(f"🔄 (Re)creating CMA session — {reason} (auth ~20-call limit)...")
    try:
        if state.get("session") is not None:
            state["session"].close()
    except Exception:
        pass
    state["session"] = create_new_cma_session()
    state["calls"] = 0
    stats["sessions"] += 1


def _dedup(locs):
    """Dedup location entries by placeCode — keeps distinct co-located ports (LA vs
    Long Beach) but collapses duplicate same-code entries (e.g. Nhava Sheva x2) so they
    don't waste the tight session budget."""
    seen, out = set(), []
    for l in locs:
        key = l.get("placeCode") or l.get("description")
        if key in seen:
            continue
        seen.add(key)
        out.append(l)
    return out


def _pace():
    s = random.uniform(*DELAY_RANGE)
    print(f"⏳ Sleeping {s:.1f}s...")
    time.sleep(s)


# --- Build the query matrix: origins x PORTS -------------------------------
matrix_rows = []
_qid = 1
for pol in origins:
    for pod in port_dests:
        matrix_rows.append({
            "ID": f"V2-{_qid:04d}",
            "Port of Loading": pol,
            "Final Destination": None,
            "LastCY": pod,
            "status": "pending",
            "result_file": None,
        })
        _qid += 1

quotes = pd.DataFrame(matrix_rows)
quotes["result_file"] = quotes["result_file"].astype("string")

print(f"✅ Query matrix built: {len(origins)} origins x {len(port_dests)} ports = {len(quotes)} pairs.")
print(quotes[["ID", "Port of Loading", "LastCY", "status"]])

_unresolved = [p for p in port_dests if not get_locations(p)]
if _unresolved:
    print(f"⚠️ {len(_unresolved)} port(s) without a CMA location (will be skipped): {_unresolved}")


# =========================
# Scrape (v3 multi-sweep retry + CMA session refresh)
# =========================
def _scrape_pair(pol_name, pod_name, row, state, stats):
    """Query one (origin, port) pair over every distinct (origin_code, port_code) combo
    — CMA nests co-located ports (LA/Long Beach, Miami/Port Everglades, Seattle/Tacoma)
    under one key, so each gets its own call. Save the HTML per combo. The session is
    re-created every CALLS_PER_SESSION calls (proactive) and on a 403 (reactive).

    Returns status:
      'done'              -> at least one combo saved a 200 HTML (schedules parsed later)
      'no_records'        -> every combo returned a non-200 non-transient (rare)
      'skipped_not_found' -> POL or POD has no CMA location
      'pending'           -> a combo hit a TRANSIENT failure (403 / 429 / 5xx / timeout) — requeue
    """
    pol_locs = _dedup(get_locations(pol_name))
    pod_locs = _dedup(get_locations(pod_name))
    if not pol_locs or not pod_locs:
        print(f"⚠️ Missing CMA locations for {pol_name} or {pod_name}")
        return "skipped_not_found"

    transient = False
    for pol in pol_locs:
        for pod in pod_locs:
            safe_pol = pol_name.replace(" ", "_").replace(",", "").lower()
            safe_pod = (pod.get("placeName") or "").replace(" ", "_").replace(",", "").lower()
            html_path = WORK_DIR / f"cma_{safe_pol}_{safe_pod}_{filename_timestamp}.html"
            if html_path.exists():
                continue                          # already scraped this combo this run (idempotent)

            if state["calls"] >= CALLS_PER_SESSION:
                _new_session(state, stats, f"reached {state['calls']} calls")

            payload = {
                "ActualPOLDescription": pol["description"], "ActualPODDescription": pod["description"],
                "ActualPOLType": pol["type"], "ActualPODType": pod["type"],
                "polDescription": pol["description"], "podDescription": pod["description"],
                "IsDeparture": "True", "SearchDate": fromDate, "searchRange": "5",
            }
            if pod["type"].startswith("Ramp"):
                payload["podType"] = "Ramp"

            stats["calls"] += 1
            state["calls"] += 1
            try:
                resp = state["session"].post(CMA_URL, data=payload, timeout=30)
            except requests.RequestException as e:
                print(f"💥 {pol_name} → {pod.get('placeName')}: {e} (transient → requeue)")
                transient = True
                _pace()
                continue

            code = resp.status_code
            print(f"📡 {pol_name} → {pod.get('placeName')} ({pod.get('placeCode')}): {code}")
            if code == 200:
                meta_comment = (
                    "<!-- "
                    f"POL={pol_name} | LastCY={pod.get('placeName')} | "
                    f"FinalDestination={row.get('Final Destination')} | OFQ={row.get('ID')} | "
                    f"snapshot_date={snapshot_date} | query_date={query_timestamp}"
                    " -->\n"
                )
                html_path.write_text(meta_comment + resp.text, encoding="utf-8")
                print(f"  ✅ Saved HTML → {html_path.name}")
            elif code == 403:
                print("  ⛔ 403 — CMA blocked; re-creating session and requeuing pair")
                transient = True
                _new_session(state, stats, "403 block")
            elif code == 429 or code >= 500:
                transient = True
            # else: other 4xx — skip this combo

            _pace()

    if transient:
        return "pending"
    any_saved = any((WORK_DIR / f"cma_{pol_name.replace(' ', '_').replace(',', '').lower()}"
                     f"_{(pod.get('placeName') or '').replace(' ', '_').replace(',', '').lower()}"
                     f"_{filename_timestamp}.html").exists()
                    for pol in pol_locs for pod in pod_locs)
    return "done" if any_saved else "no_records"


def scrape_matrix(quotes, state):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Transient
    failures (403 / 429 / 5xx / timeout) requeue for the next sweep after an escalating
    cooldown; a whole sweep that resolves nothing aborts. Pacing is per API call inside
    _scrape_pair (a pair can fan out to several co-located-port calls)."""
    stats = {"calls": 0, "sessions": 1}        # sessions: incremented on each re-create (1 = the initial)
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            status = _scrape_pair(row["Port of Loading"], row["LastCY"], row, state, stats)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — CMA blocking; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats, elapsed)


def _log_run_stats(quotes, stats, elapsed):
    """Run summary to the log: totals, wall-clock, throughput, session re-creations."""
    def _n(pred):
        return int(sum(1 for i in quotes.index if pred(quotes.at[i, "status"])))

    calls = stats["calls"]
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
    print(f"  API calls       : {calls}  (a pair can fan out to several co-located-port calls)")
    print(f"  sessions used   : {stats['sessions']}  (re-created every {CALLS_PER_SESSION} calls / on 403)")
    print(f"  scrape elapsed  : {elapsed:.1f}s  ({mins:.2f} min)")
    print(f"  throughput      : {per_min:.1f} calls/min")
    print(f"                    {min_per_100:.2f} min per 100 calls")
    print("=" * 48)

    if pending:
        print(f"⚠️ {pending} pair(s) still pending after {MAX_SWEEPS} sweeps (no HTML saved for them).")


# =========================================================================
# ORCHESTRATION
# =========================================================================
state = {"session": create_new_cma_session(), "calls": 0}   # initial CMA auth session

try:
    scrape_matrix(quotes, state)
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise
finally:
    try:
        state["session"].close()
    except Exception:
        pass


# === AFTER LOOP: parse HTML -> JSON, build canonicals, push ===
batch_transform_processing_dir(WORK_DIR, HTML_DIR)

written = []
for file in os.listdir(WORK_DIR):
    if file.startswith("cma_") and file.endswith(".json"):
        rec = build_canonical_record(os.path.join(WORK_DIR, file))
        if rec is None:
            continue
        pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
        last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
        out = get_unique_path(CANONICAL_DIR / f"CMA_{pol5}_{last5}_{filename_timestamp}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        written.append(out)

unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"CMAv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

print(f"✅ Wrote {len(written)} canonical JSON(s) → {CANONICAL_DIR}")

# --- Push the port canonicals to Supabase (env-based, ledger-tracked) ---
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from ingest.ingest import ingest_new_canonicals

try:
    ingest_new_canonicals("CMA", canonical_dir=CANONICAL_DIR,
                          ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
except Exception as e:
    print(f"⚠️ Supabase push failed (non-fatal): {e}")

print("✅ All scraping done.")
