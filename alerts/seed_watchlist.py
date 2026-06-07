"""Auto-pick a spread of pilot schedules into watchlist.json.

Picks (near-term ETD, named leg-0 vessel):
  - up to 3 transshipment routes with a named onward vessel (exercise connection logic)
  - 1 transshipment with a TBN onward vessel (exercise the coverage-gap path)
  - up to 2 direct routes
Then prints a vessel/port resolution coverage report.

Run:  python -m alerts.seed_watchlist
"""

import uuid
import datetime

from .db import client
from . import store
from .legs import is_tbn, strip_voyage
from .resolve import resolve_vessel_id, resolve_port

WINDOW_DAYS = 45


def _candidates():
    c = client()
    r = c.table("schedules_latest").select(
        "schedule_hash,carrier_code,port_of_loading,port_of_discharge,"
        "etd,eta,route_ports,vessel_sequence"
    ).execute()
    today = datetime.date.today().isoformat()
    horizon = (datetime.date.today() + datetime.timedelta(days=WINDOW_DAYS)).isoformat()
    out = []
    for x in r.data:
        vs = x.get("vessel_sequence")
        rp = x.get("route_ports")
        etd = x.get("etd")
        if not (isinstance(vs, list) and isinstance(rp, list) and vs and rp):
            continue
        if not etd or etd < today or etd > horizon:
            continue
        if is_tbn(vs[0]):           # leg-0 must be named
            continue
        out.append(x)
    return out


def _to_shipment(x) -> dict:
    return {
        "shipment_id": str(uuid.uuid4()),
        "schedule_hash": x["schedule_hash"],
        "carrier_code": x["carrier_code"],
        "pol": x["port_of_loading"],
        "pod": x["port_of_discharge"],
        "etd": x["etd"],
        "eta": x["eta"],
        "route_ports": x["route_ports"],
        "vessel_sequence": x["vessel_sequence"],
        "status": "tracking",
    }


def main():
    cands = _candidates()
    ts_named, ts_tbn, direct = [], [], []
    for x in cands:
        vs = x["vessel_sequence"]
        if len(vs) == 1:
            direct.append(x)
        elif len(vs) >= 2 and not is_tbn(vs[1]):
            ts_named.append(x)
        elif len(vs) >= 2 and is_tbn(vs[1]):
            ts_tbn.append(x)

    picks = ts_named[:3] + ts_tbn[:1] + direct[:2]
    watchlist = [_to_shipment(x) for x in picks]
    store.save_watchlist(watchlist)

    print(f"[seed] candidates: {len(cands)} "
          f"(ts_named={len(ts_named)} ts_tbn={len(ts_tbn)} direct={len(direct)})")
    print(f"[seed] wrote {len(watchlist)} shipments -> {store.config.WATCHLIST_PATH}\n")

    print("[seed] resolution coverage:")
    for s in watchlist:
        print(f"  {s['carrier_code']} {s['pol']} -> {s['pod']} (ETD {s['etd']})")
        for i, v in enumerate(s["vessel_sequence"]):
            vid = resolve_vessel_id(v)
            tag = "TBN" if is_tbn(v) else (f"vid={vid}" if vid else "UNRESOLVED")
            print(f"      vessel[{i}] {strip_voyage(v) or v!r}: {tag}")
        for p in s["route_ports"]:
            rp = resolve_port(p)
            print(f"      port  {p}: {'OK ' + rp['canonical_name'] if rp else 'NO MATCH'}")
        print()


if __name__ == "__main__":
    main()
