import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')

# WHL v2 — port-to-port matrix scrape (the final approach; preserves fidelity to a
# normal UI port-to-port search). Same shape as the other v2 carriers: origins x
# type=="port" coverage, the v3 multi-sweep retry, a run-stats summary in the log,
# and the HTML->JSON->canonical->Supabase pipeline.
#
# WHL is Selenium-driven (undetected_chromedriver behind Cloudflare), not requests. One
# browser is warmed up and kept alive across sweeps; each pair navigates fresh
# (open_search_and_set_origin does driver.get(...)), so there is no cross-pair state.
#
# ⚠️ WHL FALLBACK (the carrier quirk): for some origins the pod dropdown lists inland
# destinations but NOT the port that serves them ("coverage to inland but not to port",
# which shouldn't happen). whl_connections.json maps each of the 8 curated US ports to
# the inlands it serves; scrape_with_decision + pick_pod use it so that when a queried
# PORT isn't in the dropdown, the scrape falls back to an inland served via that port —
# preserving port coverage. This logic already lives in utils; v2 just queries the ports
# and the fallback fires automatically.
#
# coverage_v2.json is v2's OWN keys+type coverage (the 8 ports). WHL has no date param,
# so there's no query-window concern; local `today` is used only for snapshot/filenames.

import json
import time
import random
import shutil
import traceback
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, date

from utils import (
    assign_snapshot,
    batch_transform_processing_dir,
    build_canonical_record,
    build_schedule_rows,
    create_wanhai_session,
    get_unique_filename,
    get_unique_path,
    get_unresolved,
    load_connections,
    load_wanhai_locations,
    resolve,
    scrape_with_decision,
    WHLDestinationSkipped,
    WHLDestinationUnmapped,
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

TEMP_DIR = ASSETS_DIR / "temp"            # v2 output (local, self-contained)
WORK_DIR = TEMP_DIR / "work"              # HTML + parsed JSON during processing
HTML_DIR = TEMP_DIR / "html"              # archived raw HTML
CSV_DIR = TEMP_DIR / "csvs"
CANONICAL_DIR = TEMP_DIR / "canonicals"
LOG_DIR = TEMP_DIR

for _d in (WORK_DIR, HTML_DIR, CSV_DIR, CANONICAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

run_timestamp = datetime.now(timezone.utc)
today = date.today()                      # LOCAL date -> snapshot / filenames
today_iso = today.strftime("%Y-%m-%d")
today_str = today.strftime("%m.%d.%y")
query_timestamp = run_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
filename_timestamp = run_timestamp.strftime("%Y-%m-%d_%H%M%S")
snapshot_date = assign_snapshot(today_iso)

progress_file = get_unique_filename(LOG_DIR / f"WHLv2_{today_str}.csv")
logfile = get_unique_filename(LOG_DIR / f"WHL_v2_run_{today_str}.log")
sys.stdout = open(logfile, "w", encoding="utf-8", buffering=1)
sys.stderr = sys.stdout

# --- Inputs ---
origins_file = DATA_DIR / "origins.csv"
coverage_file = ASSETS_DIR / "coverage_v2.json"        # v2's OWN coverage
wanhai_locations_file = ASSETS_DIR / "whl_cities.json"  # shared read-only
connections_file = ASSETS_DIR / "whl_connections.json"  # port<->inland fallback relationship

origins = pd.read_csv(origins_file)["port"].dropna().astype(str).str.strip().tolist()
with open(coverage_file, "r", encoding="utf-8") as f:
    coverage = json.load(f)["coverage"]
port_dests = [name for name, meta in coverage.items() if meta.get("type") == "port"]

wanhai_locations = load_wanhai_locations(wanhai_locations_file)
connections = load_connections(connections_file)

# --- Config ---
DELAY_RANGE = (2, 4)                        # between pairs (WHL is a real browser; keep human)
MAX_SWEEPS = 6                              # initial pass + up to 5 requeue sweeps
SWEEP_COOLDOWNS = [30, 60, 120, 240, 480]   # seconds before each requeue sweep


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


# =========================
# Scrape (v3 multi-sweep retry + WHL Selenium + connections fallback)
# =========================
def _scrape_pair(driver, wait, idx, pol_name, port_name, row, stats):
    """Scrape one (origin, port) pair. The port->inland fallback is automatic inside
    scrape_with_decision/pick_pod (WHL lists inlands but sometimes not the serving port).

    Returns status:
      'done'              -> HTML saved
      'skipped_not_found' -> origin unresolved, or port (and its fallbacks) not in dropdown
      'skipped_unmapped'  -> port not in cities and no fallback-eligible port
      'pending'           -> TRANSIENT selenium failure (timeout / stale / crash) — requeue
      'error'             -> hard failure after an inline retry
    """
    origin_resolved, o_reason = resolve(pol_name, wanhai_locations)
    if origin_resolved is None:
        print(f"      ⚠️ origin unresolved: {o_reason}")
        return "skipped_not_found"

    try:
        html, n_rows, actual_pol, actual_pod, kind, used_dest = scrape_with_decision(
            driver, wait, origin_resolved, port_name, wanhai_locations, connections,
        )
    except WHLDestinationUnmapped as e:
        print(f"      ⏭️  skipped_unmapped: {e}")
        return "skipped_unmapped"
    except WHLDestinationSkipped as e:
        print(f"      ⏭️  skipped_not_found: {e}")
        return "skipped_not_found"
    except Exception as e:
        # Transient (Cloudflare hiccup, stale dropdown, nav timeout): requeue for next sweep.
        print(f"      💥 transient {type(e).__name__}: {e} → requeue")
        stats["transient"] += 1
        return "pending"

    out_path = WORK_DIR / f"wanhai_{idx}_{actual_pol}_{actual_pod}.html"
    meta_comment = (
        "<!-- "
        f"POL={pol_name} | LastCY={port_name} | "
        f"FinalDestination={row.get('Final Destination')} | OFQ={row.get('ID')} | "
        f"snapshot_date={snapshot_date} | query_date={query_timestamp}"
        " -->\n"
    )
    out_path.write_text(meta_comment + html, encoding="utf-8")
    via = "" if kind == "direct" else f"  (via fallback {used_dest})"
    print(f"      ✅ saved {out_path.name}  ({n_rows} rows){via}")
    stats["rows"] += n_rows
    return "done"


def scrape_matrix(quotes, driver, wait):
    """Multi-sweep drain of the origins×ports matrix (the v3 retry model). Transient
    selenium failures requeue for the next sweep after an escalating cooldown; a whole
    sweep that resolves nothing aborts (WHL/Cloudflare is hard-blocking)."""
    stats = {"scrapes": 0, "transient": 0, "rows": 0}
    t0 = time.perf_counter()

    for sweep in range(1, MAX_SWEEPS + 1):
        pending_idx = [i for i in quotes.index if quotes.at[i, "status"] == "pending"]
        if not pending_idx:
            break
        print(f"\n--- sweep {sweep}/{MAX_SWEEPS}: {len(pending_idx)} pending pair(s) ---")
        resolved = 0
        for idx in pending_idx:
            row = quotes.loc[idx]
            pol_name = str(row["Port of Loading"]).strip()
            port_name = str(row["LastCY"]).strip()
            print(f"[{idx}] POL={pol_name}  PORT={port_name}")
            stats["scrapes"] += 1
            status = _scrape_pair(driver, wait, idx, pol_name, port_name, row, stats)
            quotes.at[idx, "status"] = status
            if status != "pending":
                resolved += 1
            if status == "done":
                s = random.uniform(*DELAY_RANGE)
                print(f"      ⏳ Sleeping {s:.2f}s")
                time.sleep(s)

        still = sum(1 for i in quotes.index if quotes.at[i, "status"] == "pending")
        print(f"--- sweep {sweep} done: {resolved} resolved, {still} still pending ---")
        if still == 0:
            break
        if resolved == 0:
            print(f"🛑 zero progress this sweep — WHL blocking; stopping with {still} pending.")
            break
        if sweep < MAX_SWEEPS:
            cd = SWEEP_COOLDOWNS[min(sweep - 1, len(SWEEP_COOLDOWNS) - 1)]
            print(f"😴 cooldown {cd}s before requeue sweep {sweep + 1}...")
            time.sleep(cd)

    elapsed = time.perf_counter() - t0
    _log_run_stats(quotes, stats, elapsed)


def _log_run_stats(quotes, stats, elapsed):
    """Run summary to the log: totals, wall-clock, throughput."""
    def _n(pred):
        return int(sum(1 for i in quotes.index if pred(quotes.at[i, "status"])))

    scrapes = stats["scrapes"]
    done = _n(lambda s: s == "done")
    skipped_nf = _n(lambda s: s == "skipped_not_found")
    skipped_un = _n(lambda s: s == "skipped_unmapped")
    pending = _n(lambda s: s == "pending")
    errors = _n(lambda s: s == "error")

    mins = elapsed / 60
    per_min = scrapes / mins if mins > 0 else 0.0
    min_per_100 = (mins / scrapes * 100) if scrapes > 0 else 0.0

    print("\n" + "=" * 48)
    print("RUN STATS")
    print("=" * 48)
    print(f"  pairs             : {len(quotes)} total")
    print(f"    done            : {done}")
    print(f"    skipped_notfound: {skipped_nf}")
    print(f"    skipped_unmapped: {skipped_un}")
    print(f"    error           : {errors}")
    print(f"    pending(left)   : {pending}")
    print(f"  scrape attempts   : {scrapes}  (transient requeues: {stats['transient']})")
    print(f"  schedule rows     : {stats['rows']}")
    print(f"  scrape elapsed    : {elapsed:.1f}s  ({mins:.2f} min)")
    print(f"  throughput        : {per_min:.1f} scrapes/min")
    print(f"                      {min_per_100:.2f} min per 100 scrapes")
    print("=" * 48)

    if pending:
        print(f"⚠️ {pending} pair(s) still pending after {MAX_SWEEPS} sweeps (no HTML saved for them).")


# =========================================================================
# ORCHESTRATION
# =========================================================================
driver, wait = create_wanhai_session(headless=False)

try:
    scrape_matrix(quotes, driver, wait)
except (Exception, KeyboardInterrupt) as e:
    crash_file = get_unique_filename(progress_file.with_stem(progress_file.stem + "_CRASH"))
    safe_to_csv(quotes, crash_file, index=False)
    print(f"💥 Run failed: {e}")
    print(f"📋 Partial progress saved to: {crash_file}")
    raise
finally:
    try:
        driver.quit()
    except Exception:
        pass


# === AFTER LOOP: parse HTML -> JSON, build rows + canonicals, push ===
batch_transform_processing_dir(WORK_DIR, HTML_DIR)

all_rows, all_canonical = [], []
for file in os.listdir(WORK_DIR):
    if not (file.startswith("wanhai_") and file.endswith(".json")):
        continue
    full_path = os.path.join(WORK_DIR, file)
    all_rows.extend(build_schedule_rows(full_path, connections))
    rec = build_canonical_record(full_path, connections)
    if rec is not None:
        all_canonical.append(rec)

unresolved = get_unresolved()
if unresolved:
    uf = get_unique_filename(LOG_DIR / f"WHLv2_unresolved_ports_{today_str}.csv")
    safe_to_csv(pd.DataFrame({"raw_port": unresolved}), uf, index=False)
    print(f"⚠️ {len(unresolved)} unresolved port(s) → {uf}")

try:
    df = pd.DataFrame(all_rows)
    for col in ["ETD", "ETA", "POD ETA", "Cut-Off Date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    csv_out = get_unique_filename(CSV_DIR / f"WHL_v2_{filename_timestamp}.csv")
    safe_to_csv(df, csv_out, index=False, encoding="utf-8-sig")
    print(f"✅ Combined CSV created: {csv_out}")

    written_canonical = []
    try:
        for rec in all_canonical:
            pol5 = (rec["port_of_loading"] or "").replace(" ", "")[:5]
            last5 = (rec["last_cy"] or "").replace(" ", "")[:5]
            out = get_unique_path(CANONICAL_DIR / f"WHL_{pol5}_{last5}_{filename_timestamp}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
            written_canonical.append(out)
    except Exception:
        for p in written_canonical:
            p.unlink(missing_ok=True)
        raise
    print(f"✅ Wrote {len(written_canonical)} canonical JSON(s) → {CANONICAL_DIR}")

    # Archive parsed JSONs out of the work dir
    RAW_DIR = TEMP_DIR / "raw"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for file in os.listdir(WORK_DIR):
        if file.startswith("wanhai_") and file.endswith(".json"):
            shutil.move(WORK_DIR / file, get_unique_path(RAW_DIR / file))
    print("✅ All JSONs archived.")

    # Push port canonicals to Supabase (ledger-tracked, idempotent on schedule_hash)
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from ingest.ingest import ingest_new_canonicals
        ingest_new_canonicals("WHL", canonical_dir=CANONICAL_DIR,
                              ledger_path=TEMP_DIR / "ingest_ledger_canonicals.json")
    except Exception as e:
        print(f"⚠️ Supabase push failed (non-fatal): {e}")

except Exception as e:
    print(f"❌ Transform failed. JSONs kept in {WORK_DIR}.")
    print("Error:", e)
    traceback.print_exc()

print("✅ All scraping done.")
