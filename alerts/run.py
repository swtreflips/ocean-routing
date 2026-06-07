"""The tick loop with adaptive per-shipment cadence.

  python -m alerts.run --once      # process shipments due now, then exit
  python -m alerts.run --loop      # keep one warm browser; recheck due shipments forever

Each shipment carries its own next_check (computed from its active vessel's navigational
status + ETA proximity, see cadence.py). The loop wakes every POLL_INTERVAL_MIN and only
fetches shipments whose next_check has passed — so vessels in port operations are checked
often and mid-ocean vessels rarely. --loop reuses ONE browser session for the whole run.
"""

import sys
import time
import datetime

from . import config, store, engine, cadence, geo
from .legs import build_legs
from .resolve import resolve_vessel_id, resolve_port, find_reconnection_candidates
from .acquisition import PositionFetcher

TERMINAL = {"delivered", "missed_connection", "stopped", "untrackable"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts):
    if not ts:
        return None
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


# ---------------- plan enrichment ----------------

def _enrich_legs(shipment) -> list[dict]:
    """Attach vessel_id + resolved port coords to each leg (cached resolvers)."""
    legs = build_legs(shipment["route_ports"], shipment["vessel_sequence"])
    for leg in legs:
        leg["vessel_id"] = resolve_vessel_id(leg["vessel"])
        for side in ("from", "to"):
            rp = resolve_port(leg[f"{side}_port"])
            leg[side] = {"canonical": rp["canonical_name"], "coords": (rp["lat"], rp["lon"])} \
                if rp else {"canonical": None, "coords": None}
    # POL/POD are already canonical in the schedule — prefer them for the end coords.
    if legs:
        pol, pod = resolve_port(shipment["pol"]), resolve_port(shipment["pod"])
        if pol:
            legs[0]["from"] = {"canonical": pol["canonical_name"], "coords": (pol["lat"], pol["lon"])}
        if pod:
            legs[-1]["to"] = {"canonical": pod["canonical_name"], "coords": (pod["lat"], pod["lon"])}
    return legs


def _needed_vessel_ids(enriched, state) -> set:
    """Active leg's vessel + onward vessel if the active leg is a transshipment."""
    cursor = (state or {}).get("cursor", 0)
    ids = set()
    if cursor < len(enriched):
        ids.add(enriched[cursor]["vessel_id"])
        if enriched[cursor]["to_is_transshipment"] and cursor + 1 < len(enriched):
            ids.add(enriched[cursor + 1]["vessel_id"])
    return {i for i in ids if i is not None}


def _near_active_port(leg, obs) -> bool:
    """Active vessel within the approach ring of its leg's from/to port."""
    if not obs:
        return False
    v = (obs["lat"], obs["lon"])
    for side in ("to", "from"):
        c = leg[side]["coords"] if leg[side] else None
        if c and geo.is_within(v, c, config.APPROACH_RADIUS_MI):
            return True
    return False


def _enrich_missed(alert):
    cands = find_reconnection_candidates(
        alert.get("carrier_code"), alert.get("ts_port"),
        alert.get("after_date", "")[:10] if alert.get("after_date") else None)
    if cands:
        alert["reconnection_candidates"] = cands


# ---------------- one pass over due shipments ----------------

def process_due(fetcher, now=None) -> list[dict]:
    now = now or _now()
    watchlist = store.load_watchlist()
    state_all = store.load_state()
    debug = [f"=== pass {now.isoformat()} ==="]

    if not watchlist:
        print("[run] watchlist empty — run `python -m alerts.seed_watchlist` first.")
        return []

    enriched = {s["shipment_id"]: _enrich_legs(s) for s in watchlist}

    # which shipments are due?
    due = []
    for s in watchlist:
        st = state_all.get(s["shipment_id"], {})
        if st.get("status") in TERMINAL:
            continue
        nc = _parse(st.get("next_check"))
        if nc is None or now >= nc:
            due.append(s)
    debug.append(f"due: {len(due)}/{len(watchlist)} shipment(s)")
    if not due:
        store.write_debug(debug)
        return []

    # fetch only what due shipments need
    need = set()
    for s in due:
        need |= _needed_vessel_ids(enriched[s["shipment_id"]], state_all.get(s["shipment_id"]))
    debug.append(f"fetching {len(need)} vessel(s): {sorted(need)}")
    observations = fetcher.fetch_observations(need) if need else {}

    all_alerts = []
    for s in due:
        sid = s["shipment_id"]
        new_state, alerts, dbg = engine.evaluate_shipment(
            s, state_all.get(sid), enriched[sid], observations, now)
        for a in alerts:
            if a["type"] == "CONNECTION_MISSED":
                _enrich_missed(a)
        all_alerts.extend(alerts)
        debug.extend(dbg)

        # schedule next check (unless now terminal)
        if new_state["status"] in TERMINAL:
            debug.append(f"  {sid} terminal ({new_state['status']}); not rescheduling")
        else:
            cursor = min(new_state["cursor"], len(enriched[sid]) - 1)
            leg = enriched[sid][cursor]
            obs = observations.get(leg["vessel_id"])
            if obs:
                nxt = cadence.compute_next_check(
                    now, obs.get("status"), obs.get("arrival_ts"),
                    _near_active_port(leg, obs))
            else:
                nxt = now + datetime.timedelta(minutes=config.ERROR_RETRY_MIN)
            new_state["next_check"] = _iso(nxt)
            debug.append(f"  {sid} next_check -> {new_state['next_check']} "
                         f"(status={obs.get('status') if obs else 'NO_POS'})")
        state_all[sid] = new_state

    store.save_state(state_all)
    store.append_alerts(all_alerts)
    log = store.write_debug(debug)
    print(f"[run] pass: {len(due)} due, {len(all_alerts)} new alert(s); debug -> {log}")
    for a in all_alerts:
        print(f"  [{a['severity'].upper():6}] {a['type']}/{a['classification']} "
              f"({a['carrier_code']} leg {a.get('leg')}) {a['message']}")
    return all_alerts


def _due_count(now) -> int:
    state_all = store.load_state()
    n = 0
    for s in store.load_watchlist():
        st = state_all.get(s["shipment_id"], {})
        if st.get("status") in TERMINAL:
            continue
        nc = _parse(st.get("next_check"))
        if nc is None or now >= nc:
            n += 1
    return n


def main():
    loop = "--loop" in sys.argv
    if not loop and _due_count(_now()) == 0:
        print("[run] nothing due — skipping browser. (Cadence holds all shipments.)")
        return
    with PositionFetcher() as fetcher:
        if not loop:
            process_due(fetcher)
            return
        start = _now()
        max_rt = (datetime.timedelta(hours=config.MAX_RUNTIME_HOURS)
                  if config.MAX_RUNTIME_HOURS else None)
        while True:
            process_due(fetcher)
            if max_rt and _now() - start >= max_rt:
                print(f"[run] max runtime {config.MAX_RUNTIME_HOURS}h reached; exiting.")
                break
            time.sleep(config.POLL_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
