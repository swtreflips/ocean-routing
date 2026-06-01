"""Shared Supabase ingestion for the ocean-routing scrapers.

Each carrier's main.py calls `ingest_new_canonicals(<CODE>)` at the end of a run
to push only its newly written canonicals into the Supabase `schedules` table.
"""

from .ingest import ingest_new_canonicals

__all__ = ["ingest_new_canonicals"]
