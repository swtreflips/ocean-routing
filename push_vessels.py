"""Load vessels.json into the Supabase `vessels` table.

Portable + re-runnable (mirrors get_vessels.py): reads SUPABASE_URL / SUPABASE_KEY
from the project-root .env, transforms vessels.json, and upserts on the vessel_id
primary key so a re-run after a fresh pull updates rows in place.

Source shape — a list of single-key dicts:
    { "AGIOS DIMITRIOS": { "url": "/en/ais/details/ships/shipid:755947",
                           "value": "AGIOS DIMITRIOS", "id": 755947 } }
  outer key   -> carrier_name
  inner value -> marinetraffic_name
  inner id    -> vessel_id (PK)
  inner url   -> marinetraffic_url (relative path; prefixed to a full URL)

Dedup: the same vessel_id can appear under multiple carrier spellings (truncation
collisions). We keep one clean carrier_name (the spelling matching marinetraffic_name,
else the longest) and fold every spelling that differs from marinetraffic_name into
the `aliases` JSONB array.

    python push_vessels.py
"""

import os
import json
from pathlib import Path
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client, Client

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

VESSELS_JSON = PROJECT_ROOT / "vessels.json"
BASE_URL = "https://www.marinetraffic.com"
CHUNK_SIZE = 500


def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY not set. Expecting a .env at the "
            "project root (gitignored), same as the ingest flow."
        )
    return create_client(url, key)


def build_rows() -> list[dict]:
    """Transform vessels.json into deduped vessels-table rows (one per vessel_id)."""
    with VESSELS_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # vessel_id -> list of (carrier_spelling, mt_name, url)
    groups: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    for entry in data:
        (carrier_spelling, info), = entry.items()
        groups[info["id"]].append((carrier_spelling, info["value"], info["url"]))

    rows = []
    for vid, entries in groups.items():
        spellings = [e[0] for e in entries]
        mt_names = {e[1] for e in entries}
        urls = {e[2] for e in entries}

        # Cheap insurance: a single vessel_id should map to one MT identity.
        if len(mt_names) > 1:
            print(f"[vessels] WARN vessel_id {vid} has multiple MT names: {mt_names}")
        if len(urls) > 1:
            print(f"[vessels] WARN vessel_id {vid} has multiple URLs: {urls}")

        mt_name = entries[0][1]
        url = entries[0][2]

        carrier_name = mt_name if mt_name in spellings else max(spellings, key=len)
        aliases = sorted({s for s in spellings if s != mt_name})

        rows.append({
            "vessel_id": vid,
            "carrier_name": carrier_name,
            "marinetraffic_name": mt_name,
            "marinetraffic_url": BASE_URL + url,
            "aliases": aliases,
        })

    return rows


def main() -> None:
    client = get_client()

    rows = build_rows()
    with_aliases = sum(1 for r in rows if r["aliases"])
    print(f"[vessels] built {len(rows)} row(s), {with_aliases} with aliases")

    pushed = 0
    for start in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[start:start + CHUNK_SIZE]
        client.table("sched_vessels").upsert(chunk, on_conflict="vessel_id").execute()
        pushed += len(chunk)
        print(f"[vessels] upserted {pushed}/{len(rows)}")

    print(f"[vessels] done: {pushed} row(s) pushed")


if __name__ == "__main__":
    main()
