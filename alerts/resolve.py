"""Resolve carrier vessel names to MarineTraffic vessel_ids, and port names to coords.

Vessel match order (case-insensitive, on the voyage-stripped name):
    marinetraffic_name  ->  carrier_name  ->  aliases (jsonb array)

Misses and TBN are surfaced (vessel_id = None) so the engine can log coverage gaps.
"""

import json
from functools import lru_cache

from .db import client
from .legs import strip_voyage, is_tbn


@lru_cache(maxsize=2048)
def resolve_vessel_id(vessel_name: str | None) -> int | None:
    """Voyage-stripped name -> vessel_id, or None if TBN / unmatched / lookup error."""
    if is_tbn(vessel_name):
        return None
    name = strip_voyage(vessel_name)
    if not name:
        return None

    c = client()
    try:
        # exact-ish matches first (marinetraffic_name, then carrier_name)
        for col in ("marinetraffic_name", "carrier_name"):
            r = c.table("vessels").select("vessel_id").ilike(col, name).limit(1).execute()
            if r.data:
                return r.data[0]["vessel_id"]

        # aliases is JSONB -> contains needs a JSON value (a bare list becomes a Postgres
        # array literal and errors). Pass a JSON string: cs.["NAME"].
        r = (
            c.table("vessels").select("vessel_id")
            .contains("aliases", json.dumps([name]))
            .limit(1).execute()
        )
        if r.data:
            return r.data[0]["vessel_id"]
    except Exception:  # noqa: BLE001 - a bad token must yield "unresolved", never crash
        return None

    return None


@lru_cache(maxsize=2048)
def resolve_port(name: str) -> dict | None:
    """Resolve a schedule port token to a ports row.

    Schedule POL/POD are already canonical ("Nhava Sheva, India"); route_ports use
    short uppercase tokens ("COLOMBO"). Try, in order:
      1. exact canonical_name
      2. city `name` exact (case-insensitive)
      3. canonical_name starts-with the token
    Returns {canonical_name, lat, lon} or None.
    """
    if not name:
        return None
    c = client()
    cols = "canonical_name,name,latitude,longitude"

    r = c.table("ports").select(cols).eq("canonical_name", name).limit(1).execute()
    if not r.data:
        r = c.table("ports").select(cols).ilike("name", name).limit(1).execute()
    if not r.data:
        r = c.table("ports").select(cols).ilike("canonical_name", f"{name}%").limit(1).execute()
    if not r.data:
        return None

    row = r.data[0]
    return {
        "canonical_name": row["canonical_name"],
        "lat": float(row["latitude"]),
        "lon": float(row["longitude"]),
    }


def find_reconnection_candidates(carrier_code, ts_port, after_date, limit=3) -> list[dict]:
    """After a missed connection, list the carrier's next sailings through the same T/S
    port to the same destination, departing after the missed arrival. Inference only —
    the schedules table, no AIS needed."""
    if not (carrier_code and ts_port):
        return []
    c = client()
    q = (
        c.table("schedules_latest")
        .select("carrier_code,port_of_discharge,etd,eta,route_ports,vessel_sequence")
        .eq("carrier_code", carrier_code)
        .contains("route_ports", [ts_port])
    )
    if after_date:
        q = q.gte("etd", after_date)
    r = q.order("etd").limit(limit).execute()
    return r.data or []
