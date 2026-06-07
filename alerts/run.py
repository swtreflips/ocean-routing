"""The tick loop: fetch positions for all watched shipments and evaluate alerts.

  python -m alerts.run --once      # single pass (default)
  python -m alerts.run --loop      # repeat every CADENCE_HOURS

One warm browser per tick fetches every needed vessel (active vessel + onward vessel at
transshipments). State lives in alerts/data/, so the loop is restartable.
"""

import sys
import time
import datetime

from . import config, store, engine
from .legs import build_legs
from .resolve import resolve_vessel_id, resolve_port, find_reconnection_candidates
from .acquisition import PositionFetcher


def _enrich_legs(shipment) -> list[dict]:
    """Attach vessel_id + resolved port coords to each leg (cached resolvers)."""
    legs = build_legs(shipment["route_ports"], shipment["vessel_sequence"])
    for leg in legs:
        leg["vessel_id"] = resolve_vessel_id(leg["vessel"])
        for side in ("from", "to"):
            rp = resolve_port(leg[f"{side}_port"])
            leg[side] = {"canonical": rp["canonical_name"], "coords": (rp["lat"], rp["lon"])} \
                if rp else {"canonical": None, "coords": None}
    return leg_list_with_pol_pod(legs, shipment)


def leg_list_with_pol_pod(legs, shipment):
    """POL/POD are already canonical in the schedule — prefer them for the end coords."""
    if legs:
        pol = resolve_port(shipment["pol"])
        pod = resolve_port(shipment["pod"])
        if pol:
            legs[0]["from"] = {"canonical": pol["canonical_name"], "coords": (pol["lat"], pol["lon"])}
        if pod:
            legs[-1]["to"] = {"canonical": pod["canonical_name"], "coords": (pod["lat"], pod["lon"])}
    return legs


def _needed_vessel_ids(shipment, enriched, state) -> set:
    """Active leg's vessel + onward vessel if the active leg is a transshipment."""
    cursor = (state or {}).get("cursor", 0)
    ids = set()
    if cursor < len(enriched):
        ids.add(enriched[cursor]["vessel_id"])
        if enriched[cursor]["to_is_transshipment"] and cursor + 1 < len(enriched):
            ids.add(enriched[cursor + 1]["vessel_id"])
    return {i for i in ids if i is not None}


def _enrich_missed(alert):
    """Attach candidate next sailings to a CONNECTION_MISSED alert."""
    cands = find_reconnection_candidates(
        alert.get("carrier_code"), alert.get("ts_port"),
        alert.get("after_date", "")[:10] if alert.get("after_date") else None,
    )
    if cands:
        alert["reconnection_candidates"] = cands


def tick() -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    watchlist = store.load_watchlist()
    state_all = store.load_state()
    debug = [f"=== tick {now.isoformat()} | {len(watchlist)} shipment(s) ==="]

    if not watchlist:
        print("[run] watchlist empty — run `python -m alerts.seed_watchlist` first.")
        return

    enriched = {s["shipment_id"]: _enrich_legs(s) for s in watchlist}

    # collect all vessel_ids to fetch this tick
    need = set()
    for s in watchlist:
        need |= _needed_vessel_ids(s, enriched[s["shipment_id"]], state_all.get(s["shipment_id"]))
    debug.append(f"fetching {len(need)} vessel position(s): {sorted(need)}")

    positions = {}
    if need:
        with PositionFetcher() as fetcher:
            positions = fetcher.fetch_positions(need)

    all_alerts = []
    for s in watchlist:
        sid = s["shipment_id"]
        new_state, alerts, dbg = engine.evaluate_shipment(
            s, state_all.get(sid), enriched[sid], positions, now)
        state_all[sid] = new_state
        for a in alerts:
            if a["type"] == "CONNECTION_MISSED":
                _enrich_missed(a)
        all_alerts.extend(alerts)
        debug.extend(dbg)

    store.save_state(state_all)
    store.append_alerts(all_alerts)
    log_path = store.write_debug(debug)

    print(f"[run] tick done: {len(all_alerts)} new alert(s); debug -> {log_path}")
    for a in all_alerts:
        print(f"  [{a['severity'].upper():6}] {a['type']}/{a['classification']} "
              f"({a['carrier_code']} {a.get('leg')}) {a['message']}")


def main():
    loop = "--loop" in sys.argv
    tick()
    if loop:
        while True:
            time.sleep(config.CADENCE_HOURS * 3600)
            tick()


if __name__ == "__main__":
    main()
