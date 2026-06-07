"""Auto-pick pilot schedules into watchlist.json.

Selection: for each carrier, one of each routing complexity (by route_ports length):
  - direct      (2 ports, no transshipment)
  - single-TS   (3 ports, 1 transshipment)
  - double-TS   (4 ports, 2 transshipments)
within an ETD range, leg-0 vessel named (so it's trackable from the start). Carriers
missing a type just contribute fewer. Then prints a vessel/port resolution report.

Run (defaults to today .. today+45 days):
    python -m alerts.seed_watchlist
    python -m alerts.seed_watchlist --etd-min 2026-06-10 --etd-max 2026-06-13
    python -m alerts.seed_watchlist --exclude CMA          (skip carrier code(s), CSV)
"""

import sys
import uuid
import datetime
from collections import defaultdict

from .db import client
from . import store
from .legs import is_tbn, is_non_vessel, strip_voyage
from .resolve import resolve_vessel_id, resolve_port

TYPE_LABEL = {0: "direct", 1: "single-TS", 2: "double-TS"}


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _ts_count(x) -> int:
    """Transshipments = ports - 2  (direct=0, single=1, double=2, ...)."""
    return len(x["route_ports"]) - 2


def _candidates(etd_min, etd_max):
    """All schedules in the ETD window (paged past the 1000-row PostgREST cap)."""
    c = client()
    rows, start, page = [], 0, 1000
    while True:
        r = (
            c.table("schedules_latest")
            .select("schedule_hash,carrier_code,port_of_loading,port_of_discharge,"
                    "etd,eta,route_ports,vessel_sequence")
            .gte("etd", etd_min)
            .lte("etd", etd_max)
            .range(start, start + page - 1)
            .execute()
        )
        batch = r.data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        start += page

    out = []
    for x in rows:
        vs, rp = x.get("vessel_sequence"), x.get("route_ports")
        if not (isinstance(vs, list) and isinstance(rp, list) and vs and len(rp) >= 2):
            continue
        if is_non_vessel(vs[0]):          # leg-0 must be a real, trackable vessel
            continue                      # (skip TBN / FEEDER / BARGE starts)
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
    today = datetime.date.today()
    etd_min = _arg("--etd-min", today.isoformat())
    etd_max = _arg("--etd-max", (today + datetime.timedelta(days=45)).isoformat())
    exclude = {c.strip().upper() for c in (_arg("--exclude", "") or "").split(",") if c.strip()}

    cands = _candidates(etd_min, etd_max)

    # group by carrier, then by routing type (0/1/2 transshipments)
    by_carrier = defaultdict(lambda: {0: [], 1: [], 2: []})
    for x in cands:
        if x["carrier_code"].upper() in exclude:
            continue
        tc = _ts_count(x)
        if tc in (0, 1, 2):
            by_carrier[x["carrier_code"]][tc].append(x)

    picks = []
    for carrier in sorted(by_carrier):
        for tc in (0, 1, 2):
            if by_carrier[carrier][tc]:
                picks.append(by_carrier[carrier][tc][0])

    watchlist = [_to_shipment(x) for x in picks]
    store.save_watchlist(watchlist)

    excl_note = f" (excluding {', '.join(sorted(exclude))})" if exclude else ""
    print(f"[seed] ETD window {etd_min} .. {etd_max}: {len(cands)} candidate(s) "
          f"across {len(by_carrier)} carrier(s){excl_note}")
    print(f"[seed] picked {len(watchlist)} shipment(s) "
          f"({sum(1 for p in picks if _ts_count(p)==0)} direct, "
          f"{sum(1 for p in picks if _ts_count(p)==1)} single-TS, "
          f"{sum(1 for p in picks if _ts_count(p)==2)} double-TS) "
          f"-> {store.config.WATCHLIST_PATH}\n")

    print("[seed] resolution coverage:")
    for s in watchlist:
        tc = len(s["route_ports"]) - 2
        print(f"  {s['carrier_code']:4} [{TYPE_LABEL.get(tc, f'{tc}-TS')}] "
              f"{s['pol']} -> {s['pod']} (ETD {s['etd']})")
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
