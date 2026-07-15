"""
utilsv2.py — v2-only canonical builder.

Mirrors utils.build_canonical_record but enriches every schedule with the rail
fields needed to synthesize inland schedules from canonicals ALONE (so schedule
creation never has to re-open the raw API responses):

    haulage_mode        inboundTotalTransportModes on the rail leg
                        ("Rail" = on-dock rail-only; "Truck,Rail" = drayage +
                        rail). This is the carrier's own icon and the real
                        discriminator behind multimodal lanes.
    rail_transit_days   yard availability - sea-port arrival (eta - pod_eta).
    discharge_terminal  deliFacilityCode of the rail leg (explains within-mode
                        dwell spread; diagnostic).
    ocean_service       service code of the rail leg (diagnostic).

Kept separate from utils.build_canonical_record so v1 + Supabase ingest stay
untouched. v2 canonicals are never ingested.
"""

from datetime import date

from utils import (
    _group_schedules,
    _iso_date_or_none,
    _to_int_or_none,
    _schedule_uuid,
    _query_uuid,
    normalize_pod,
    normalize_ports,
)
import json


def _rail_days(port_eta_iso, yard_eta_iso):
    """Rail leg in whole days = yard availability - sea-port arrival. None if unusable."""
    if not port_eta_iso or not yard_eta_iso:
        return None
    days = (date.fromisoformat(yard_eta_iso) - date.fromisoformat(port_eta_iso)).days
    return days if days >= 0 else None


def build_canonical_record_v2(file_path):
    """
    Reads a JSON file (IDs already assigned) and returns ONE canonical record
    per raw query — identical to build_canonical_record plus the v2 rail
    enrichment fields on each schedule. Returns None if no usable schedules.
    """
    with open(file_path, "r") as f:
        data = json.load(f)

    pol = data.get("PortofLoading")
    last_cy = data.get("LastCY")
    final_destination = data.get("FinalDestination")
    query_date = data.get("query_date")
    snapshot_date = data.get("snapshot_date")

    schedules = []
    for schedule_id, legs in _group_schedules(data):
        first_leg = legs[0]
        max_leg = legs[-1]
        max_seq = max_leg["legSequence"]

        etd_iso = _iso_date_or_none(first_leg.get("etd"))
        mother_vessel = first_leg.get("vessel")
        pod = normalize_pod(max_leg.get("pod"))

        port_eta = _iso_date_or_none(max_leg.get("eta"))         # arrival at the sea port
        yard_eta = _iso_date_or_none(max_leg.get("available"))   # availability at the inland yard

        schedules.append({
            "id": _schedule_uuid("COS", pol, pod, etd_iso, mother_vessel, schedule_id),
            "port_of_discharge": pod,
            "cutoff_date": _iso_date_or_none(first_leg.get("cutOff")),
            "etd": etd_iso,
            "eta": yard_eta,
            "pod_eta": port_eta,
            "transit_time_days": _to_int_or_none(first_leg.get("transitTime")),
            "transport_type": "Direct" if max_seq == 1 else f"{max_seq-1} TS",
            "mother_vessel": mother_vessel,
            "ts_ports": normalize_ports([leg.get("pod") for leg in legs[:-1]]),
            "ts_vessels": [leg.get("vessel") for leg in legs[1:]],
            "route_ports": normalize_ports([legs[0].get("pol")] + [leg.get("pod") for leg in legs]),
            "vessel_sequence": [leg.get("vessel") for leg in legs],
            # --- v2 rail enrichment (lets inland schedules be synthesized from canonicals) ---
            "haulage_mode": max_leg.get("inboundTotalTransportModes"),
            "rail_transit_days": _rail_days(port_eta, yard_eta),
            "discharge_terminal": max_leg.get("deliFacilityCode"),
            "ocean_service": max_leg.get("service"),
        })

    if not schedules:
        return None

    return {
        "schema_version": 2,
        "id": _query_uuid("COS", pol, last_cy, query_date),
        "carrier": {"code": "COS", "name": "COSCO Shipping Lines"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
