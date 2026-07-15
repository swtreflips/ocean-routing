"""
utilsv2.py — EMC v2-only canonical builder.

Mirrors utils.build_canonical_record but enriches each schedule with the rail
leg + EMC's candidate discriminators, so synthesis/analysis never reopens the
parsed HTML:

    rail_transit_days       eta (yard) - pod_eta (last ocean leg / port).
                            Same definition as COS.
    ocean_service           service code of the discharge (last ocean) leg.
                            COS's discriminator was this; EMC TBD.
    intermodal_legs         the EXPLICIT post-ocean ground legs Evergreen lists
                            (from/to/service/transit_days) — EMC spells the rail
                            leg out as its own "Intermodal" leg, so its transit
                            is a direct candidate discriminator, not an inference.
    intermodal_transit_days sum of those ground-leg transits (explicit rail days).
    waiting_days            sum of "WAITING" leg transits (explicit dwell).

Which of these actually drives rail time is decided in Phase 5 (see /synth.md).
Kept separate from v1's build_canonical_record so v1 + Supabase stay untouched.
"""

from datetime import date
import json

from utils import (
    _iso_date_or_none,
    _to_int_or_none,
    normalize_pod,
    normalize_ports,
    _schedule_uuid,
    _query_uuid,
    _trim_vessel,
    _emc_legs,
    _emc_legs_summary,
    _EMC_SKIP_SERVICES,
    _EMC_GROUND_SERVICES,
)


def _rail_days(pod_eta_iso, eta_iso):
    """Rail leg = yard availability (eta) - sea-port arrival (pod_eta). None if unusable."""
    if not pod_eta_iso or not eta_iso:
        return None
    days = (date.fromisoformat(eta_iso) - date.fromisoformat(pod_eta_iso)).days
    return days if days >= 0 else None


# US states + DC and Canadian provinces/territories. A valid US/CA destination
# string is "City, XX"; foreign junk (Penang, Mombasa, Ho Chi Minh) has no such tag.
_NA_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    "BC", "AB", "SK", "MB", "ON", "QC", "NB", "NS", "PE", "NL", "YT", "NT", "NU",
}


def _is_na_dest(name):
    """True if `name` is a US/CA location ('City, XX'). Filters EMC's foreign junk."""
    if not name or "," not in name:
        return False
    return name.rsplit(",", 1)[1].strip().upper() in _NA_REGIONS


def _svc(leg):
    return (leg.get("service") or "").strip()


def build_canonical_record_v2(file_path):
    """One canonical record per query, EMC schema + v2 rail enrichment. None if empty."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]
    if not items:
        return None
    item = items[0]

    request = item.get("request") or {}
    pol = request.get("POL")
    last_cy = request.get("LastCY")
    final_destination = request.get("FinalDestination")
    query_date = request.get("query_date")
    snapshot_date = request.get("snapshot_date")

    schedules = []
    for schedule in item.get("schedules") or []:
        raw_legs = schedule.get("legs") or []
        legs = _emc_legs(schedule)
        if not legs:
            continue
        # Drop non-North-American junk: EMC sometimes returns foreign feeder
        # fragments that neither start at the origin nor end at the queried US/CA
        # destination (e.g. ... -> Ho Chi Minh / Penang / Mombasa).
        if not _is_na_dest(legs[-1]["pod"]):
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = \
            _emc_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        etd_iso = _iso_date_or_none(legs[0]["etd"])
        eta_iso = _iso_date_or_none(legs[-1]["eta"])

        # --- v2 enrichment / EMC candidate discriminators (from RAW legs) ---
        ocean_raw = [lg for lg in raw_legs
                     if _svc(lg) not in (_EMC_SKIP_SERVICES | _EMC_GROUND_SERVICES)]
        ocean_service = _svc(ocean_raw[-1]) or None if ocean_raw else None

        intermodal_legs = [
            {
                "from": lg.get("from"),
                "to": lg.get("to"),
                "service": _svc(lg),
                "transit_days": _to_int_or_none(lg.get("transit_time_days")),
            }
            for lg in raw_legs if _svc(lg) in _EMC_GROUND_SERVICES
        ]
        intermodal_transit_days = sum((l["transit_days"] or 0) for l in intermodal_legs) or None
        waiting_days = sum(
            _to_int_or_none(lg.get("transit_time_days")) or 0
            for lg in raw_legs if _svc(lg) in _EMC_SKIP_SERVICES
        ) or None

        schedules.append({
            "id": _schedule_uuid("EMC", pol, pod, etd_iso, mother_vessel, schedule.get("schedule_id")),
            "port_of_discharge": pod,
            "cutoff_date": _iso_date_or_none(schedule.get("cutoff_date")),
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": _to_int_or_none(schedule.get("transit_days")),
            "transport_type": transport_type,
            "mother_vessel": _trim_vessel(mother_vessel),
            "ts_ports": normalize_ports(ts_ports),
            "ts_vessels": [_trim_vessel(v) for v in ts_vessels],
            "route_ports": normalize_ports(route_ports),
            "vessel_sequence": [_trim_vessel(v) for v in vessel_sequence],
            # --- v2 rail enrichment + candidate discriminators ---
            "rail_transit_days": _rail_days(pod_eta, eta_iso),
            "ocean_service": ocean_service,
            "intermodal_legs": intermodal_legs,
            "intermodal_transit_days": intermodal_transit_days,
            "waiting_days": waiting_days,
        })

    if not schedules:
        return None

    return {
        "schema_version": 2,
        "id": _query_uuid("EMC", pol, last_cy, query_date),
        "carrier": {"code": "EMC", "name": "Evergreen"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
