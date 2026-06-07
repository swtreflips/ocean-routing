"""Read-only viewer for the alert engine's results. No browser, no DB.

  python -m alerts.report                       # status board + 24h timeline (all sev)
  python -m alerts.report --since 48h           # timeline window (e.g. 90m, 12h, 7d)
  python -m alerts.report --severity medium     # only medium+ (info < medium < high)
  python -m alerts.report --shipment <id|prefix>  # filter to one shipment
  python -m alerts.report --all                 # whole timeline, no time window

Reads alerts/data/{watchlist,state,alerts}.json(l).
"""

import sys
import json
import datetime

from . import config, store

SEV_RANK = {"info": 0, "medium": 1, "high": 2}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts):
    if not ts:
        return None
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def _parse_since(s):
    """'24h' / '90m' / '7d' -> timedelta. Default 24h."""
    if not s:
        return datetime.timedelta(hours=24)
    unit = s[-1].lower()
    n = float(s[:-1])
    return {"m": datetime.timedelta(minutes=n),
            "h": datetime.timedelta(hours=n),
            "d": datetime.timedelta(days=n)}.get(unit, datetime.timedelta(hours=24))


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _hours_until(iso, now):
    dt = _parse(iso)
    return (dt - now).total_seconds() / 3600 if dt else None


def _read_alerts():
    if not config.ALERTS_PATH.exists():
        return []
    with config.ALERTS_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def status_board(watchlist, state, now):
    by_id = {s["shipment_id"]: s for s in watchlist}
    print(f"STATUS BOARD                              (as of {now.strftime('%Y-%m-%d %H:%MZ')})")
    counts = {}
    for sid, s in by_id.items():
        st = state.get(sid, {})
        status = st.get("status", "new")
        counts[status] = counts.get(status, 0) + 1
        nlegs = max(len(s.get("route_ports", [])) - 1, 1)
        cursor = st.get("cursor", 0)
        legstate = st.get("legs", {}).get(str(cursor), {}).get("state", "PENDING")
        nc = st.get("next_check")
        h = _hours_until(nc, now) if nc else None
        nxt = f"next {h:+.1f}h" if h is not None else "next  --  "
        route = f"{s['pol']} -> {s['pod']}"
        print(f"  {s['carrier_code']:4} {route:38.38} leg {cursor}/{nlegs}  "
              f"{status:16.16} {legstate:11.11} {nxt}  [{sid[:8]}]")
    tally = " · ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"  {len(by_id)} shipment(s): {tally}\n")


def timeline(alerts, now, since, since_label, min_sev, ship):
    cutoff = None if "--all" in sys.argv else now - since
    rows = []
    for a in alerts:
        if SEV_RANK.get(a.get("severity"), 0) < min_sev:
            continue
        if ship and not a.get("shipment_id", "").startswith(ship):
            continue
        ts = _parse(a.get("logged_at"))
        if cutoff and ts and ts < cutoff:
            continue
        rows.append(a)

    window = "all time" if cutoff is None else f"last {since_label}"
    label = [k for k, v in SEV_RANK.items() if v == min_sev][0]
    print(f"ALERT TIMELINE  ({window}, severity >= {label})")
    if not rows:
        print("  (none)\n")
    for a in rows:
        t = (_parse(a.get("logged_at")) or now).strftime("%m-%d %H:%M")
        sev = a.get("severity", "info")[:4].upper()
        print(f"  {t}  [{sev:4}] {a['carrier_code']:4} "
              f"{a['type']}/{a['classification']}  {a['message']}")

    summary = {}
    for a in alerts:
        summary[a.get("severity", "info")] = summary.get(a.get("severity", "info"), 0) + 1
    s = " · ".join(f"{k} {summary.get(k, 0)}" for k in ("info", "medium", "high"))
    print(f"\nSUMMARY (all logged)   {s}")


def main():
    now = _now()
    since_label = _arg("--since", "24h")
    since = _parse_since(since_label)
    min_sev = SEV_RANK.get(_arg("--severity", "info"), 0)
    ship = _arg("--shipment")

    watchlist = store.load_watchlist()
    state = store.load_state()
    alerts = _read_alerts()

    if not watchlist:
        print("No watchlist — run `python -m alerts.seed_watchlist`.")
        return

    status_board(watchlist, state, now)
    timeline(alerts, now, since, since_label, min_sev, ship)


if __name__ == "__main__":
    main()
