"""Pull every vessel name out of the `schedules_latest` materialized view in
Supabase, dedup them, and write the unique set to ~/Downloads/unique_vessels.json.

Portable + re-runnable: run it again any time after ingesting new data and it
re-reads the whole MV and rewrites the file. Reads the same SUPABASE_URL /
SUPABASE_KEY from the project-root .env that the ingest flow uses.

    python get_vessels.py
"""

import os
import json
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

VIEW = "schedules_latest"
COLUMN = "vessel_sequence"
PAGE_SIZE = 1000  # Supabase/PostgREST caps a single response; page through it.
OUTPUT_PATH = Path.home() / "Downloads" / "unique_vessels.json"


def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY not set. Expecting a .env at the "
            "project root (gitignored), same as the ingest flow."
        )
    return create_client(url, key)


def fetch_vessel_sequences(client: Client) -> list:
    """Return every vessel_sequence value (each a list) from the MV, paged."""
    rows: list = []
    start = 0
    while True:
        resp = (
            client.table(VIEW)
            .select(COLUMN)
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def dedup_vessels(rows: list) -> list[str]:
    """Flatten the per-row lists, drop blanks, and dedup (case-insensitive),
    keeping the first-seen spelling. Returns a sorted list."""
    seen: dict[str, str] = {}
    for row in rows:
        sequence = row.get(COLUMN) or []
        if not isinstance(sequence, list):
            sequence = [sequence]
        for name in sequence:
            if name is None:
                continue
            cleaned = str(name).strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            seen.setdefault(key, cleaned)
    return sorted(seen.values(), key=str.casefold)


def main() -> None:
    client = get_client()

    rows = fetch_vessel_sequences(client)
    print(f"[vessels] fetched {len(rows)} row(s) from {VIEW}")

    vessels = dedup_vessels(rows)
    print(f"[vessels] {len(vessels)} unique vessel name(s) after dedup")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(vessels, f, indent=2, ensure_ascii=False)

    print(f"[vessels] wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
