"""Push newly written canonical schedule JSONs into Supabase.

Design (see plan / Schedules SUPA.md):
  * Ledger = efficiency layer. A per-carrier JSON ledger records which canonical
    files have already been pushed, so historical files in canonical/ are never
    re-ingested.
  * DB upsert on_conflict=schedule_hash = correctness layer. Idempotent, so a
    re-push after a crash is a harmless no-op.

Crash safety:
  * Files are pushed one at a time; the ledger is updated (atomically) only after
    a file's upsert succeeds. A crash at any point leaves the DB consistent and
    the failed/un-ledgered files retried on the next run.

The schedule_hash logic here is intentionally byte-identical to the standalone
Schedules/ingest_schedules.py so hashes match the DB UNIQUE(schedule_hash)
constraint. If one side changes, change both.
"""

import os
import json
import hashlib
import datetime
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client

# src/ingest/ingest.py -> parents[2] == project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


# =========================================
# HASHING  (keep identical to Schedules/ingest_schedules.py)
# =========================================

def _norm(value) -> str:
    """Normalize a field for hashing.

    None -> "" so rows sharing NULLs hash identically.
    Lists (ts_ports, ts_vessels) -> order-preserving join, since
    routing/vessel order is meaningful.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return "^".join(_norm(item) for item in value)
    return str(value)


def _schedule_hash(wrapper: dict, schedule: dict) -> str:
    """MD5 over the 14 fields that define schedule uniqueness."""
    hash_fields = [
        wrapper["carrier_code"],            # Carrier
        wrapper["port_of_loading"],         # Port of Loading
        schedule.get("port_of_discharge"),  # Port of Discharge
        wrapper["last_cy"],                 # Last CY
        wrapper["query_date"],              # Query Date (run stamp)
        schedule.get("etd"),                # ETD
        schedule.get("eta"),                # ETA
        schedule.get("pod_eta"),            # POD ETA
        schedule.get("transit_time_days"),  # Transit Time
        schedule.get("transport_type"),     # Transport Type
        schedule.get("ts_ports", []),       # TS Port(s)
        schedule.get("mother_vessel"),      # Mother Vessel
        schedule.get("ts_vessels", []),     # TS Vessel(s)
        schedule.get("cutoff_date"),        # Cut-Off Date
    ]
    hash_input = "|".join(_norm(v) for v in hash_fields)
    return hashlib.md5(hash_input.encode()).hexdigest()


# =========================================
# ROW BUILDING
# =========================================

def _build_rows(data: dict) -> list[dict]:
    """Turn one canonical record into deduped schedules-table rows."""
    carrier = data.get("carrier", {})

    wrapper = {
        "schema_version":  data.get("schema_version"),
        "carrier_code":    carrier.get("code"),
        "carrier_name":    carrier.get("name"),
        "query_date":      data.get("query_date"),
        "snapshot_date":   data.get("snapshot_date"),
        "port_of_loading": data.get("port_of_loading"),
        "last_cy":         data.get("last_cy"),
    }

    deduped: dict[str, dict] = {}
    for schedule in data.get("schedules", []):
        schedule_hash = _schedule_hash(wrapper, schedule)

        row = {
            "schedule_hash": schedule_hash,
            **wrapper,
            "port_of_discharge": schedule.get("port_of_discharge"),
            "cutoff_date":       schedule.get("cutoff_date"),
            "etd":               schedule.get("etd"),
            "eta":               schedule.get("eta"),
            "pod_eta":           schedule.get("pod_eta"),
            "transit_time_days": schedule.get("transit_time_days"),
            "transport_type":    schedule.get("transport_type"),
            "mother_vessel":     schedule.get("mother_vessel"),
            "ts_ports":          schedule.get("ts_ports", []),
            "ts_vessels":        schedule.get("ts_vessels", []),
            "route_ports":       schedule.get("route_ports", []),
            "vessel_sequence":   schedule.get("vessel_sequence", []),
            "raw_schedule":      schedule,
        }

        # Dedup within the file: a single upsert batch cannot touch the same
        # conflict key twice ("ON CONFLICT cannot affect a row a second time").
        deduped[schedule_hash] = row

    return list(deduped.values())


# =========================================
# LEDGER  (atomic to survive mid-write crashes)
# =========================================

def _load_ledger(ledger_path: Path) -> dict:
    if ledger_path.exists():
        with ledger_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_ledger(ledger_path: Path, ledger: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, ledger_path)  # atomic on the same filesystem


# =========================================
# CLIENT
# =========================================

def _get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY not set. Create a .env at the project "
            "root (see .env template) — it is gitignored."
        )
    return create_client(url, key)


# =========================================
# PUBLIC ENTRY POINT
# =========================================

def ingest_new_canonicals(
    carrier_code: str,
    canonical_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> dict:
    """Push only not-yet-ingested canonicals for a carrier into Supabase.

    Paths default to src/data/<code.lower()>/canonical and
    src/data/<code.lower()>/ingest_ledger.json. Never raises on a per-file push
    error — failures are reported and retried on the next run.
    """
    carrier_dir = PROJECT_ROOT / "src" / "data" / carrier_code.lower()
    canonical_dir = canonical_dir or carrier_dir / "canonical"
    ledger_path = ledger_path or carrier_dir / "ingest_ledger.json"

    canonical_dir = Path(canonical_dir)
    ledger_path = Path(ledger_path)

    if not canonical_dir.exists():
        print(f"[ingest] No canonical dir at {canonical_dir}; nothing to do.")
        return {"new_files": 0, "rows_pushed": 0, "skipped": 0, "failed": []}

    ledger = _load_ledger(ledger_path)

    all_files = sorted(canonical_dir.glob("*.json"))
    new_files = [p for p in all_files if p.name not in ledger]
    skipped = len(all_files) - len(new_files)

    print(
        f"[ingest] {carrier_code}: {len(all_files)} canonical(s), "
        f"{skipped} already ingested, {len(new_files)} new."
    )

    client = _get_client()

    rows_pushed = 0
    failed: list[tuple[str, str]] = []

    for path in new_files:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            rows = _build_rows(data)

            if rows:
                client.table("schedules").upsert(
                    rows, on_conflict="schedule_hash"
                ).execute()

            # Mark pushed only AFTER the upsert succeeds, then persist atomically.
            ledger[path.name] = {
                "pushed_at": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "rows": len(rows),
            }
            _save_ledger(ledger_path, ledger)

            rows_pushed += len(rows)
            print(f"[ingest] OK {path.name}: {len(rows)} row(s)")

        except Exception as e:  # noqa: BLE001 - report and continue, never crash the run
            failed.append((path.name, str(e)))
            print(f"[ingest] FAIL {path.name}: {e}")

    # Refresh the materialized view once, only if we pushed anything.
    if rows_pushed:
        try:
            client.rpc("refresh_schedules_latest").execute()
            print("[ingest] Refreshed schedules_latest materialized view.")
        except Exception as e:  # noqa: BLE001 - a refresh failure must not fail the run
            print(f"[ingest] Refresh failed (non-fatal): {e}")

    summary = {
        "new_files": len(new_files),
        "rows_pushed": rows_pushed,
        "skipped": skipped,
        "failed": failed,
    }
    print(
        f"[ingest] {carrier_code} done: {rows_pushed} row(s) from "
        f"{len(new_files) - len(failed)}/{len(new_files)} new file(s), "
        f"{len(failed)} failed."
    )
    return summary


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python src/ingest/ingest.py <CARRIER_CODE>   e.g. COS")
        raise SystemExit(2)

    ingest_new_canonicals(sys.argv[1].upper())
