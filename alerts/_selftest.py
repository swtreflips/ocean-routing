"""Synthetic unit checks for engine.py (no DB, no browser). Run:
    python -m alerts._selftest
"""

import datetime
from . import engine, config

UTC = datetime.timezone.utc


def leg(i, frm, to, vid, coords_from, coords_to, vessel="V", last=False, ts=False):
    return {
        "index": i, "from_port": frm, "to_port": to, "vessel": vessel,
        "vessel_clean": vessel, "tbn": vid is None, "is_first": i == 0,
        "is_last": last, "to_is_transshipment": ts, "vessel_id": vid,
        "from": {"canonical": frm, "coords": coords_from},
        "to": {"canonical": to, "coords": coords_to},
    }


POL = (0.0, 0.0)
TS = (10.0, 10.0)
POD = (20.0, 20.0)
FAR = (40.0, 40.0)   # > 50 mi from everything here


def two_leg():
    return [
        leg(0, "POL", "TS", 1, POL, TS, "A", last=False, ts=True),
        leg(1, "TS", "POD", 2, TS, POD, "B", last=True, ts=False),
    ]


def ship():
    return {"shipment_id": "S1", "schedule_hash": "h", "carrier_code": "X",
            "pol": "POL", "pod": "POD", "etd": "2026-06-10", "eta": "2026-07-01"}


def run(states_positions):
    """Feed (now, positions) ticks through the engine; return (state, all_alerts)."""
    legs = two_leg()
    state = None
    alerts = []
    for now, positions in states_positions:
        state, a, _ = engine.evaluate_shipment(ship(), state, legs, positions, now)
        alerts += a
    return state, alerts


def types(alerts):
    return [(a["type"], a["classification"]) for a in alerts]


def t(day):
    return datetime.datetime(2026, 6, day, 12, 0, tzinfo=UTC)


def assert_in(needle, haystack, label):
    ok = needle in haystack
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: expected {needle}")
    if not ok:
        print("        got:", haystack)
    assert ok, label


def main():
    print("MADE (comfortable): A arrives TS, B departs 3d later (MCT=2)")
    near = lambda c: {"lat": c[0], "lon": c[1], "speed": 12}
    state, alerts = run([
        (t(15), {1: near(TS), 2: near(TS)}),                 # A arrives TS; B present
        (t(18), {1: near(TS), 2: {**near(FAR), "speed": 12}}),  # B departs TS (3d later)
    ])
    assert_in(("CONNECTION_MADE", "COMFORTABLE"), types(alerts), "made")
    assert state["cursor"] == 1, "cursor advanced"

    print("TIGHT: A arrives TS, B departs 1d later (< MCT 2)")
    state, alerts = run([
        (t(15), {1: near(TS), 2: near(TS)}),
        (t(16), {1: near(TS), 2: {**near(FAR), "speed": 12}}),  # 1d margin
    ])
    assert_in(("CONNECTION_TIGHT", "TIGHT"), types(alerts), "tight")
    assert state["cursor"] == 1, "tight advanced"

    print("MISSED: B departs BEFORE A arrives")
    state, alerts = run([
        (t(14), {1: {**near(FAR), "speed": 12}, 2: near(TS)}),    # A en route; B at TS
        (t(15), {1: {**near(FAR), "speed": 12}, 2: {**near(FAR), "speed": 12}}),  # B leaves, A still away
        (t(17), {1: near(TS), 2: {**near(FAR), "speed": 12}}),    # A arrives AFTER B left
    ])
    assert_in(("CONNECTION_MISSED", "MISSED"), types(alerts), "missed")
    assert state["status"] == "missed_connection", "status missed"

    print("DELIVERED: after made, B reaches POD")
    state, alerts = run([
        (t(15), {1: near(TS), 2: near(TS)}),
        (t(18), {1: near(TS), 2: {**near(FAR), "speed": 12}}),    # made -> cursor 1
        (t(25), {2: near(POD)}),                                  # B arrives POD
    ])
    assert_in(("DELIVERED", "DELIVERED"), types(alerts), "delivered")
    assert state["status"] == "delivered", "status delivered"

    print("\nAll engine self-tests passed.")


if __name__ == "__main__":
    main()
