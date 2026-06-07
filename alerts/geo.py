"""Distance + ETA helpers.

Geofence distances use geopy.geodesic (WGS84 ellipsoid) — verified to match the PostGIS
geography distance used by the is_near / nearest_ports RPCs. A "within radius" geofence is
correctly a straight-line check.

ETA projection uses the REALISTIC marine route distance (searoute) ÷ current speed when
config.USE_SEAROUTE is on — routing around land and through canals, instead of the
optimistic straight line. Falls back to great-circle if searoute is unavailable or can't
route a given pair. Still an estimate (it uses the instantaneous speed, no weather), but
the distance is no longer the weak link.
"""

import datetime

from geopy.distance import geodesic

from . import config

try:
    import searoute as _searoute
except Exception:  # noqa: BLE001 - searoute is optional; fall back to straight-line
    _searoute = None

KN_TO_KMH = 1.852


def distance_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    return geodesic(a, b).miles


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return geodesic(a, b).km


def is_within(vessel: tuple[float, float], port: tuple[float, float], radius_mi: float) -> bool:
    return distance_mi(vessel, port) <= radius_mi


def route_distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Realistic marine route distance (km) via searoute; great-circle fallback.

    a / b are (lat, lon); searoute wants [lon, lat]. Any routing failure (unroutable
    pair, inland sea outside the network, missing lib) silently falls back to the
    straight-line distance so the engine never breaks on a bad coordinate pair.
    """
    if config.USE_SEAROUTE and _searoute is not None:
        try:
            route = _searoute.searoute([a[1], a[0]], [b[1], b[0]], units="km")
            length = route.properties.get("length")
            if length and length > 0:
                return float(length)
        except Exception:  # noqa: BLE001 - fall back to straight-line
            pass
    return distance_km(a, b)


def project_eta(
    vessel: tuple[float, float],
    port: tuple[float, float],
    sog_kn: float,
    speed_floor_kn: float,
    now: datetime.datetime | None = None,
) -> datetime.datetime | None:
    """ETA = now + realistic route distance / SOG. None if too slow/stopped."""
    if sog_kn is None or sog_kn < speed_floor_kn:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hours = route_distance_km(vessel, port) / (sog_kn * KN_TO_KMH)
    return now + datetime.timedelta(hours=hours)


def days_between(earlier: datetime.datetime, later: datetime.datetime) -> float:
    """Signed days (later - earlier)."""
    return (later - earlier).total_seconds() / 86400.0
