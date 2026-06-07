"""Shared Supabase client for the alert engine."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from supabase import create_client, Client

from . import config

load_dotenv(config.ENV_PATH)


@lru_cache(maxsize=1)
def client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY not set. Expecting a .env at the project root."
        )
    return create_client(url, key)
