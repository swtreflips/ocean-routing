"""
utilsv2.py — HMM v2-only canonical builder.

Mirrors utils.build_canonical_record but enriches each schedule with the rail
leg + discriminator. Survey showed HMM is the COS pattern: the OCEAN SERVICE
LOOP (`mthLoopCd`) pins rail time to +-1 day (122/125 exact), while the rail
ramp does not — so `ocean_service` is the lookup key.

    rail_transit_days   eta (yard) - pod_eta (last ocean leg / port). HMM exposes
                        the rail move as an explicit RR leg, but the canonical
                        already collapses to pod_eta/eta, so the same definition.
    ocean_service       mthLoopCd (service loop, e.g. "PS3") — the discriminator.
    discharge_terminal  podFcltyCd (diagnostic).
    rail_ramp           pvyFcltyCd (the inland ramp; diagnostic — does NOT drive
                        rail time, kept for analysis).
    vessel_carrier      mthVslCarrCd (alliance slot-share carrier; informational).

Kept separate from v1's build_canonical_record so v1 + Supabase stay untouched.
"""

from datetime import date
import json

from utils import (
    _hmm_legs,
    _hmm_legs_summary,
    _iso_date_or_none,
    normalize_pod,
    normalize_ports,
    _schedule_uuid,
    _query_uuid,
)


def _rail_days(pod_eta_iso, eta_iso):
    """Rail leg = yard availability (eta) - sea-port arrival (pod_eta). None if unusable."""
    if not pod_eta_iso or not eta_iso:
        return None
    days = (date.fromisoformat(eta_iso) - date.fromisoformat(pod_eta_iso)).days
    return days if days >= 0 else None


def build_canonical_record_v2(file_path):
    """One canonical record per query, HMM schema + v2 rail enrichment. None if empty."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    pol = data.get("PortOfLoading")
    last_cy = data.get("LastCY")
    final_destination = data.get("FinalDestination")
    query_date = data.get("query_date")
    snapshot_date = data.get("snapshot_date")

    schedules = []
    for sched in data.get("schedules", []) or []:
        legs = _hmm_legs(sched)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = \
            _hmm_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        etd_iso = _iso_date_or_none(legs[0]["etd"])
        eta_iso = _iso_date_or_none(legs[-1]["eta"])
        cutoff_iso = _iso_date_or_none(sched.get("gctCtofDt"))

        total_hrs = sched.get("totTrstmHrs")
        transit_days = int(round(total_hrs / 24)) if isinstance(total_hrs, (int, float)) else None

        schedules.append({
            "id": _schedule_uuid("HMM", pol, pod, etd_iso, mother_vessel, sched.get("grmNo")),
            "port_of_discharge": pod,
            "cutoff_date": cutoff_iso,
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": transit_days,
            "transport_type": transport_type,
            "mother_vessel": mother_vessel,
            "ts_ports": normalize_ports(ts_ports),
            "ts_vessels": ts_vessels,
            "route_ports": normalize_ports(route_ports),
            "vessel_sequence": vessel_sequence,
            # --- v2 rail enrichment + discriminator ---
            "rail_transit_days": _rail_days(pod_eta, eta_iso),
            "ocean_service": sched.get("mthLoopCd"),
            "discharge_terminal": sched.get("podFcltyCd"),
            "rail_ramp": sched.get("pvyFcltyCd"),
            "vessel_carrier": sched.get("mthVslCarrCd"),
        })

    if not schedules:
        return None

    return {
        "schema_version": 2,
        "id": _query_uuid("HMM", pol, last_cy, query_date),
        "carrier": {"code": "HMM", "name": "HMM (Hyundai Merchant Marine)"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
