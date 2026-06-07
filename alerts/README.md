# alerts/ — Phase 1 schedule-adherence alert engine

A laptop-run engine that watches a small set of schedules and emits alerts for pickup-ETD
risk, departure/arrival milestones, final-ETA risk, and transshipment **connection
integrity** (made / tight / missed vs a minimum-connection-time buffer). No map, no UI.

Framework & rationale: see [`../scheduleTracker.md`](../scheduleTracker.md) (§2b is the
connection model; §8 is the infrastructure model this implements).

## Run

Use the `schedulesenv` venv and force UTF-8 (the browser step prints emoji). Run as a
**module** (it's a package):

```powershell
$env:PYTHONUTF8=1
$py = "C:\Users\Mike\OneDrive - Prime Time Packaging\Schedules\schedulesenv\Scripts\python.exe"

& $py -m alerts.seed_watchlist     # pick ~5-6 pilot schedules -> data/watchlist.json
& $py -m alerts.run --once         # one tick (opens Chrome, fetches, evaluates)
& $py -m alerts.run --loop         # repeat every CADENCE_HOURS
```

## How it works (one tick)

1. Load `data/watchlist.json` (the plan) + `data/state.json` (cursor/edges/fired).
2. Resolve each leg's vessel → `vessel_id` (`vessels` table, voyage suffix stripped) and
   each port → coords (`ports` table).
3. Open **one** warm MarineTraffic browser, fetch every needed vessel position
   (active vessel + onward vessel at a transshipment).
4. `engine.evaluate_shipment(...)` (pure) walks the leg state machine, detects geofence
   edges, classifies timings vs ETD/ETA with buffers, and runs the §2b connection check.
5. Persist new state, append alerts to `data/alerts.jsonl`, write a debug trace.

## Modules

| File | Role |
|------|------|
| `config.py` | buffers, geofence radius, cadence, paths |
| `db.py` | shared Supabase client |
| `acquisition.py` | warm-browser position fetch (the swappable AIS layer) |
| `resolve.py` | vessel name→id, port→coords, reconnection candidates |
| `legs.py` | schedule → ordered legs |
| `geo.py` | geopy distance + rough ETA projection |
| `engine.py` | **pure** state machine + connection logic (idempotent alerts) |
| `store.py` | local-file persistence |
| `seed_watchlist.py` | auto-pick pilot schedules |
| `run.py` | the tick loop |

## Alert types

`PICKUP_ETD_RISK` · `PICKUP` · `DEPARTED_ORIGIN` · `ARRIVED_PORT` · `FINAL_ETA_RISK` ·
`CONNECTION_MADE` / `CONNECTION_TIGHT` / `CONNECTION_MISSED` / `CONNECTION_PENDING` /
`CONNECTION_UNKNOWN` · `COVERAGE_GAP` · `DELIVERED`.

## Tuning (`config.py`)

`ON_TIME_TOLERANCE_DAYS=1`, `MCT_DAYS=2`, `ARRIVE_RADIUS_MI=50`, `SPEED_FLOOR_KN=1`,
`CADENCE_HOURS=6`. Start generous; tighten after calibrating against a few manual checks.

## Caveats (from §2b / §4)

- **Vessel ≠ container** — AIS tracks the ship; silent capacity-rolling on a *feasible*
  connection is invisible. Missed-connection detection is the high-confidence signal.
- **ETA projection is straight-line ÷ SOG** — coarse "plausibly on time," not a real ETA.
- **Edge detection needs ticks over time** — a single `--once` run shows current status
  and any edges since the last run; arrivals/departures/connections resolve as the loop
  accumulates observations.
- `data/` is gitignored (local pilot state).
