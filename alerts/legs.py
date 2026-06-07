"""Decompose a schedule's plan into ordered legs.

A schedule with N route_ports has N-1 legs:
    leg i = { from_port, to_port, vessel }  where vessel = vessel_sequence[i]

route_ports and vessel_sequence come straight from the schedules table.
Vessel strings carry a "/voyage" suffix (e.g. "AGIOS DIMITRIOS/IV624A").
"""


def strip_voyage(vessel_name: str | None) -> str:
    """'AGIOS DIMITRIOS/IV624A' -> 'AGIOS DIMITRIOS'. Safe on None/empty."""
    if not vessel_name:
        return ""
    return vessel_name.split("/", 1)[0].strip()


def is_tbn(vessel_name: str | None) -> bool:
    return "TBN" in (vessel_name or "").upper()


def build_legs(route_ports: list[str], vessel_sequence: list[str]) -> list[dict]:
    """Return ordered legs. Length = len(route_ports) - 1.

    Each leg: {index, from_port, to_port, vessel, vessel_clean, tbn,
               is_first, is_last, to_is_transshipment}.
    A leg's to_port is a transshipment if it is not the final port (i.e. another
    leg follows and the cargo changes vessel there).
    """
    legs = []
    n = len(route_ports)
    for i in range(n - 1):
        vessel = vessel_sequence[i] if i < len(vessel_sequence) else None
        legs.append({
            "index": i,
            "from_port": route_ports[i],
            "to_port": route_ports[i + 1],
            "vessel": vessel,
            "vessel_clean": strip_voyage(vessel),
            "tbn": is_tbn(vessel),
            "is_first": i == 0,
            "is_last": i == n - 2,
            "to_is_transshipment": i < n - 2,   # another leg follows this arrival
        })
    return legs


def onward_vessel(legs: list[dict], leg_index: int) -> dict | None:
    """The next leg (vessel B) after a transshipment arrival, or None if last."""
    nxt = leg_index + 1
    return legs[nxt] if nxt < len(legs) else None
