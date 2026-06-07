"""Distance + rough ETA helpers.

Distances use geopy.geodesic (WGS84 ellipsoid) — verified to match the PostGIS
geography distance used by the is_near / nearest_ports RPCs.

ETA projection is intentionally crude: straight-line distance / current speed-over-
ground. It ignores actual sea routing (real path is longer) and speed variation, so
treat it as a coarse "is this plausibly on time" estimate, not a precise ETA.
"""

import datetime

from geopy.distance import geodesic

KN_TO_KMH = 1.852


def distance_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    return geodesic(a, b).miles


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return geodesic(a, b).km


def is_within(vessel: tuple[float, float], port: tuple[float, float], radius_mi: float) -> bool:
    return distance_mi(vessel, port) <= radius_mi


def project_eta(
    vessel: tuple[float, float],
    port: tuple[float, float],
    sog_kn: float,
    speed_floor_kn: float,
    now: datetime.datetime | None = None,
) -> datetime.datetime | None:
    """Rough ETA = now + great-circle distance / SOG. None if too slow/stopped."""
    if sog_kn is None or sog_kn < speed_floor_kn:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hours = distance_km(vessel, port) / (sog_kn * KN_TO_KMH)
    return now + datetime.timedelta(hours=hours)


def days_between(earlier: datetime.datetime, later: datetime.datetime) -> float:
    """Signed days (later - earlier)."""
    return (later - earlier).total_seconds() / 86400.0
