"""Adaptive per-shipment check cadence.

Mirrors the proven AIS scraper's `compute_next_check`: vessels in port operations
(moored / at anchor) or close to their destination are rechecked often; vessels far
out mid-ocean rarely. This naturally splits the watchlist into a high-frequency group
(near port ops) and a low-frequency group (mid-sea) without hard-coded groupings.

Inputs come from the position endpoint (navigationalStatus) + voyage endpoint
(arrivalTimestamp), plus a near_port flag derived from our own geofence.
"""

import datetime

from . import config


def compute_next_check(now, status, arrival_ts, near_port=False):
    """Return the next-check datetime for a vessel given its current observation."""
    c = config.CADENCE

    # In port operations -> check most often.
    if status in config.MOORED_STATUSES:
        return now + datetime.timedelta(minutes=c["moored_min"])

    # Geographically near its destination port (approaching) -> check often.
    if near_port:
        return now + datetime.timedelta(hours=c["near_or_today_h"])

    # No ETA and not near a port -> low frequency.
    if not arrival_ts:
        return now + datetime.timedelta(hours=c["no_eta_h"])

    eta = datetime.datetime.fromtimestamp(arrival_ts, tz=datetime.timezone.utc)
    days_to_eta = (eta - now).total_seconds() / 86400.0

    if eta.date() == now.date():
        return now + datetime.timedelta(hours=c["near_or_today_h"])
    if days_to_eta < 4:
        return now + datetime.timedelta(hours=c["lt4_h"])
    if days_to_eta < 7:
        return now + datetime.timedelta(hours=c["lt7_h"])
    if days_to_eta > 10:
        return now + datetime.timedelta(hours=c["gt10_h"])
    return now + datetime.timedelta(hours=c["default_h"])
