"""Pure state-machine logic for one shipment per tick.

No I/O here. The caller (run.py) resolves vessel_ids + port coords, fetches positions,
then hands everything in. The engine returns (new_state, alerts, debug_lines).

State machine per leg:
    PENDING -> APPROACHING -> IN_TRANSIT -> ARRIVED --advance--> next leg
The cursor advances only on an ARRIVAL at the leg's to_port. At a transshipment the
arrival does NOT auto-advance — the §2b connection check (onward vessel B's departure vs
the MCT buffer) decides made / tight / missed.

Alerts are idempotent: each fires once per (type, leg, classification) via `fired`.
"""

import datetime

from . import config, geo


# ---------------- datetime helpers ----------------

def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )


def _sched_dt(date_str: str | None) -> datetime.datetime | None:
    """Schedule date 'YYYY-MM-DD' -> datetime at 12:00 UTC (reduce edge bias)."""
    if not date_str:
        return None
    d = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
    return d.replace(hour=12, tzinfo=datetime.timezone.utc)


# ---------------- state scaffolding ----------------

def _blank_state(now: datetime.datetime) -> dict:
    return {
        "cursor": 0,
        "status": "tracking",
        "legs": {},
        "geofence": {},
        "fired": [],
        "last_checked": _iso(now),
    }


def _leg_state(state: dict, idx: int) -> dict:
    return state["legs"].setdefault(
        str(idx), {"state": "PENDING", "departed_at": None, "arrived_at": None}
    )


def _geofence_edge(state: dict, vid, port_key: str, near_now: bool, now: datetime.datetime):
    """Update near-state for (vessel, port); return 'ENTER' | 'EXIT' | None."""
    key = f"{vid}@{port_key}"
    prev = state["geofence"].get(key)
    prev_near = prev["near"] if prev else False
    edge = None
    if near_now and not prev_near:
        edge = "ENTER"
    elif prev_near and not near_now:
        edge = "EXIT"
    since = now if (prev is None or edge) else _parse(prev["since"])
    state["geofence"][key] = {"near": near_now, "since": _iso(since)}
    return edge


def _emit(alerts, fired, debug, shipment, *, atype, leg, klass, severity, message, **extra):
    """Append an alert if its (type:leg:class) key hasn't fired yet."""
    key = f"{atype}:{leg}:{klass}"
    debug.append(f"    -> {atype} leg={leg} class={klass} sev={severity}: {message}")
    if key in fired:
        return
    fired.append(key)
    alerts.append({
        "shipment_id": shipment["shipment_id"],
        "schedule_hash": shipment.get("schedule_hash"),
        "carrier_code": shipment.get("carrier_code"),
        "type": atype,
        "leg": leg,
        "classification": klass,
        "severity": severity,
        "message": message,
        **extra,
    })


def _classify_vs_target(proj: datetime.datetime, target: datetime.datetime, tol: float):
    """Compare a projected time to a planned target. Returns (class, severity, delta_days)."""
    delta = geo.days_between(target, proj)   # proj - target (positive = late)
    if delta > tol:
        return "LIKELY_LATE", "high", delta
    if delta >= -tol:
        return "AT_RISK", "medium", delta
    return "ON_TRACK", "info", delta


# ---------------- main entry ----------------

def evaluate_shipment(shipment, state, legs, positions, now=None):
    """legs: enriched list with per-leg {vessel_id, from{coords}, to{coords}, ...}.
    positions: {vessel_id: {"lat","lon","speed",...} | None}.
    Returns (new_state, alerts, debug_lines)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    alerts: list[dict] = []
    debug: list[str] = [f"shipment {shipment['shipment_id']} "
                        f"{shipment.get('pol')} -> {shipment.get('pod')} "
                        f"status={state.get('status') if state else 'NEW'}"]

    if state is None:
        state = _blank_state(now)
    fired = state["fired"]

    if state["status"] in ("delivered", "missed_connection", "stopped", "untrackable"):
        debug.append(f"  terminal status={state['status']}; skipping")
        state["last_checked"] = _iso(now)
        return state, alerts, debug

    cursor = state["cursor"]
    if cursor >= len(legs):
        state["status"] = "delivered"
        state["last_checked"] = _iso(now)
        return state, alerts, debug

    leg = legs[cursor]
    ls = _leg_state(state, cursor)
    vid = leg["vessel_id"]
    debug.append(f"  active leg {cursor}: {leg['from_port']} -> {leg['to_port']} "
                 f"on {leg['vessel_clean']} (vid={vid}) legstate={ls['state']}")

    # --- coverage gaps ---
    if leg["tbn"] or vid is None:
        klass = "TBN" if leg["tbn"] else "UNRESOLVED"
        _emit(alerts, fired, debug, shipment, atype="COVERAGE_GAP", leg=cursor,
              klass=klass, severity="medium",
              message=f"Active vessel '{leg['vessel']}' is {klass}; cannot track this leg.")
        state["status"] = "untrackable"
        state["last_checked"] = _iso(now)
        return state, alerts, debug

    pos = positions.get(vid)
    if not pos:
        debug.append("  no position this tick; holding state")
        state["last_checked"] = _iso(now)
        return state, alerts, debug

    vessel = (pos["lat"], pos["lon"])
    sog = pos.get("speed")
    from_c = leg["from"]["coords"] if leg["from"] else None
    to_c = leg["to"]["coords"] if leg["to"] else None

    # --- geofence edges for the active vessel ---
    near_from = geo.is_within(vessel, from_c, config.ARRIVE_RADIUS_MI) if from_c else False
    near_to = geo.is_within(vessel, to_c, config.ARRIVE_RADIUS_MI) if to_c else False
    edge_from = _geofence_edge(state, vid, leg["from_port"], near_from, now) if from_c else None
    edge_to = _geofence_edge(state, vid, leg["to_port"], near_to, now) if to_c else None
    if to_c:
        debug.append(f"  dist_to_dest={geo.distance_mi(vessel, to_c):.0f} mi "
                     f"sog={sog} near_from={near_from} near_to={near_to}")

    # --- PICKUP risk (first leg, before departure/arrival only) ---
    if (leg["is_first"] and ls["state"] in ("PENDING", "APPROACHING")
            and ls["departed_at"] is None and from_c):
        etd = _sched_dt(shipment.get("etd"))
        if near_from:
            ls["state"] = "APPROACHING"
            klass = "AT_PICKUP_LATE" if (etd and now > etd + datetime.timedelta(
                days=config.ON_TIME_TOLERANCE_DAYS)) else "AT_PICKUP"
            _emit(alerts, fired, debug, shipment, atype="PICKUP", leg=cursor, klass=klass,
                  severity=("high" if "LATE" in klass else "info"),
                  message=f"At pickup port {leg['from_port']} (ETD {shipment.get('etd')}).")
        elif etd:
            proj = geo.project_eta(vessel, from_c, sog, config.SPEED_FLOOR_KN, now)
            if proj:
                klass, sev, delta = _classify_vs_target(proj, etd, config.ON_TIME_TOLERANCE_DAYS)
                ls["state"] = "APPROACHING"
                _emit(alerts, fired, debug, shipment, atype="PICKUP_ETD_RISK", leg=cursor,
                      klass=klass, severity=sev,
                      message=(f"Projected arrival at {leg['from_port']} "
                               f"{proj.strftime('%Y-%m-%d')} vs ETD {shipment.get('etd')} "
                               f"({delta:+.1f}d)."),
                      projected_arrival=_iso(proj), delta_days=round(delta, 2))
            else:
                debug.append("  pickup: SOG below floor; skipping ETA projection")

    # --- DEPARTED origin (exit edge of from_port, moving) ---
    if (leg["is_first"] and ls["departed_at"] is None and edge_from == "EXIT"
            and (sog or 0) > config.SPEED_FLOOR_KN):
        ls["departed_at"] = _iso(now)
        ls["state"] = "IN_TRANSIT"
        etd = _sched_dt(shipment.get("etd"))
        late = etd and now > etd + datetime.timedelta(days=config.ON_TIME_TOLERANCE_DAYS)
        _emit(alerts, fired, debug, shipment, atype="DEPARTED_ORIGIN", leg=cursor,
              klass="LATE" if late else "ON_TIME", severity="high" if late else "info",
              message=f"Departed {leg['from_port']} (ETD {shipment.get('etd')}).")
    elif ls["state"] == "PENDING" and not near_from:
        ls["state"] = "IN_TRANSIT"   # already underway when first observed

    # --- FINAL ETA risk (last leg, in transit) ---
    if leg["is_last"] and ls["arrived_at"] is None and to_c and ls["state"] == "IN_TRANSIT":
        eta = _sched_dt(shipment.get("eta"))
        proj = geo.project_eta(vessel, to_c, sog, config.SPEED_FLOOR_KN, now)
        if eta and proj:
            klass, sev, delta = _classify_vs_target(proj, eta, config.ON_TIME_TOLERANCE_DAYS)
            _emit(alerts, fired, debug, shipment, atype="FINAL_ETA_RISK", leg=cursor,
                  klass=klass, severity=sev,
                  message=(f"Projected arrival at {leg['to_port']} "
                           f"{proj.strftime('%Y-%m-%d')} vs ETA {shipment.get('eta')} "
                           f"({delta:+.1f}d)."),
                  projected_arrival=_iso(proj), delta_days=round(delta, 2))

    # --- ARRIVAL at this leg's destination ---
    if edge_to == "ENTER" and ls["arrived_at"] is None:
        ls["arrived_at"] = _iso(now)
        ls["state"] = "ARRIVED"
        _emit(alerts, fired, debug, shipment, atype="ARRIVED_PORT", leg=cursor,
              klass="ARRIVED", severity="info",
              message=f"Arrived {leg['to_port']}.")

    # --- track onward vessel B at the T/S EVERY tick (even before A arrives, so a
    #     "B left before A arrived" miss is still observed) ---
    if not leg["is_last"] and leg["to_is_transshipment"]:
        _track_onward(state, legs, cursor, positions, now, debug)

    # --- advancement / connection decision when A has ARRIVED ---
    if ls["state"] == "ARRIVED":
        if leg["is_last"]:
            state["status"] = "delivered"
            _emit(alerts, fired, debug, shipment, atype="DELIVERED", leg=cursor,
                  klass="DELIVERED", severity="info",
                  message=f"Vessel arrived final port {leg['to_port']} (vessel-level).")
        else:
            _resolve_connection(shipment, state, legs, positions, cursor, now,
                                alerts, fired, debug)

    state["last_checked"] = _iso(now)
    return state, alerts, debug


def _track_onward(state, legs, i, positions, now, debug):
    """Update onward vessel B's geofence at the T/S port; record its departure to _meta."""
    nxt = legs[i + 1]
    b_vid = nxt["vessel_id"]
    if nxt["tbn"] or b_vid is None:
        return
    b_pos = positions.get(b_vid)
    ts_c = nxt["from"]["coords"] if nxt["from"] else None
    if not b_pos or not ts_c:
        return
    ts_port = legs[i]["to_port"]
    near_b = geo.is_within((b_pos["lat"], b_pos["lon"]), ts_c, config.ARRIVE_RADIUS_MI)
    edge = _geofence_edge(state, b_vid, ts_port, near_b, now)
    if edge == "EXIT" and (b_pos.get("speed") or 0) > config.SPEED_FLOOR_KN:
        state.setdefault("_meta", {})[f"{b_vid}@{ts_port}:departed"] = _iso(now)
        debug.append(f"  onward vessel {nxt['vessel_clean']} departed {ts_port}")


def _resolve_connection(shipment, state, legs, positions, i, now, alerts, fired, debug):
    """§2b decision: compare onward vessel B's departure vs A's arrival + MCT."""
    ts_port = legs[i]["to_port"]
    a_arr = _parse(_leg_state(state, i)["arrived_at"])
    nxt = legs[i + 1]
    b_vid = nxt["vessel_id"]
    debug.append(f"  connection @ {ts_port}: onward {nxt['vessel_clean']} (vid={b_vid})")

    if nxt["tbn"] or b_vid is None:
        _emit(alerts, fired, debug, shipment, atype="CONNECTION_UNKNOWN", leg=i,
              klass="TBN" if nxt["tbn"] else "UNRESOLVED", severity="medium",
              message=f"Onward vessel '{nxt['vessel']}' at {ts_port} not trackable.")
        state["status"] = "untrackable"
        return

    b_departed = _parse(state.get("_meta", {}).get(f"{b_vid}@{ts_port}:departed"))

    if b_departed and a_arr:
        margin = geo.days_between(a_arr, b_departed)   # B_dep - A_arr
        if margin < 0:
            _connection_missed(shipment, state, legs, i, a_arr, alerts, fired, debug,
                               reason=f"onward vessel left {ts_port} before arrival")
        elif margin < config.MCT_DAYS:
            _advance(state, i, b_departed)
            _emit(alerts, fired, debug, shipment, atype="CONNECTION_TIGHT", leg=i,
                  klass="TIGHT", severity="medium",
                  message=(f"Tight connection at {ts_port}: {margin:.1f}d "
                           f"< MCT {config.MCT_DAYS}d — roll risk."),
                  margin_days=round(margin, 2))
        else:
            _advance(state, i, b_departed)
            _emit(alerts, fired, debug, shipment, atype="CONNECTION_MADE", leg=i,
                  klass="COMFORTABLE", severity="info",
                  message=f"Connection made at {ts_port}: {margin:.1f}d margin.",
                  margin_days=round(margin, 2))
    else:
        _emit(alerts, fired, debug, shipment, atype="CONNECTION_PENDING", leg=i,
              klass="WAITING", severity="info",
              message=f"Arrived {ts_port}; waiting on onward vessel departure.")


def _advance(state, i, departed_at_iso):
    state["cursor"] = i + 1
    nxt = _leg_state(state, i + 1)
    nxt["state"] = "IN_TRANSIT"
    nxt["departed_at"] = departed_at_iso


def _connection_missed(shipment, state, legs, i, a_arr, alerts, fired, debug, reason):
    state["status"] = "missed_connection"
    _emit(alerts, fired, debug, shipment, atype="CONNECTION_MISSED", leg=i,
          klass="MISSED", severity="high",
          message=f"Missed connection at {legs[i]['to_port']}: {reason}.",
          ts_port=legs[i]["to_port"],
          after_date=_iso(a_arr) if a_arr else None)
