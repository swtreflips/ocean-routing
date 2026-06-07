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
& $py -m alerts.run --once         # process shipments due now, then exit
& $py -m alerts.run --loop         # keep ONE warm browser; recheck due shipments forever
& $py -m alerts.report             # read-only status board + alert timeline (no browser)
```

## Running unattended (always-on laptop, daily restart)

`--loop` keeps one warm browser and rechecks due shipments forever, but a single
infinite run won't recover if the MarineTraffic session goes stale. The robust pattern
is a **daily restart**: `MAX_RUNTIME_HOURS = 23.5` makes the loop exit cleanly each day,
and a Windows Task Scheduler job relaunches it — a fresh session every cycle. State lives
in `data/`, so it resumes exactly where it left off.

Launcher: [`run_loop.bat`](run_loop.bat) (sets repo root as cwd, UTF-8, logs to
`data/run.log`).

**Task Scheduler setup:**
1. Task Scheduler → *Create Task* (not Basic).
2. General → **Run only when user is logged on** (the visible Chrome needs a desktop).
3. Triggers → **Daily**, pick a time; optionally "Repeat task" off (the 23.5h cap handles
   the run length, the daily trigger handles relaunch).
4. Actions → *Start a program* → `alerts\run_loop.bat` (full path).
5. Settings → enable **"If the task is already running … Do not start a new instance"**
   (the 23.5h cap means it won't be, but this is a safety net).

**Power:** set the power plan to **never sleep on AC**, or the loop freezes when the
laptop sleeps. Keep the user logged in (visible browser needs the desktop session).

To run it by hand instead: `python -m alerts.run --loop` from the repo root.

## Checking results over time

The engine writes three artifacts to `data/`, each answering a different question:

| File | Question | Nature |
|------|----------|--------|
| `alerts.jsonl` | what changed, and when? | append-only **timeline** (one event/line) |
| `state.json` | where is everything now? | current **snapshot** per shipment |
| `debug/<ts>.log` | why did it decide that? | per-pass trace |

Alerts are idempotent (fire once per state change), so every `alerts.jsonl` line is a
real event, not noise. Read it all with **`report.py`** (read-only, no browser/DB):

```powershell
& $py -m alerts.report                      # status board + last 24h timeline
& $py -m alerts.report --since 48h          # window: 90m / 12h / 7d
& $py -m alerts.report --severity medium    # info < medium < high
& $py -m alerts.report --shipment 11b3a8e5  # one shipment (id prefix)
& $py -m alerts.report --all                # whole timeline, no window
```

Typical pilot use: run `--loop` in one terminal; run `report.py` whenever you want a
snapshot.

## How it works (one pass)

1. Load `data/watchlist.json` (the plan) + `data/state.json` (cursor/edges/fired/next_check).
2. Select **due** shipments — non-terminal and `now >= next_check`.
3. Resolve each due leg's vessel → `vessel_id` (`vessels` table, voyage suffix stripped)
   and each port → coords (`ports` table).
4. With **one** warm MarineTraffic browser, fetch each needed vessel's position + voyage
   (active vessel + onward vessel at a transshipment), with a random delay between vessels.
5. `engine.evaluate_shipment(...)` (pure) walks the leg state machine, detects geofence
   edges, classifies timings vs ETD/ETA with buffers, and runs the §2b connection check.
6. Compute each shipment's **next_check** from its active vessel's status + ETA (cadence).
7. Persist state, append alerts to `data/alerts.jsonl`, write a debug trace.

## Adaptive cadence

Each shipment carries its own `next_check`; `--loop` wakes every `POLL_INTERVAL_MIN` and
only fetches shipments that are due. The active vessel's navigational status + ETA
proximity decide the interval (mirrors the proven AIS scraper) — so the watchlist
self-splits into a high-frequency "near port operations" group and a low-frequency
"mid-ocean" group:

| Condition (active vessel) | Next check |
|---|---|
| Moored / At Anchor | 45 min |
| Approaching its port (within `APPROACH_RADIUS_MI`) | 2 h |
| ETA today | 2 h |
| ETA < 4 days | 4 h |
| ETA < 7 days | 8 h |
| ETA 7–10 days | 8 h |
| ETA > 10 days | 12 h |
| No ETA, mid-ocean | 12 h |
| Fetch error | retry in `ERROR_RETRY_MIN` (30 min) |

`--loop` keeps a single browser session alive for the whole run (set
`MAX_RUNTIME_HOURS`, e.g. `23.33`, for a daily-cron restart).

## Modules

| File | Role |
|------|------|
| `config.py` | buffers, geofence radius, cadence buckets, paths |
| `db.py` | shared Supabase client |
| `acquisition.py` | warm-browser position+voyage fetch w/ jitter (the swappable AIS layer) |
| `cadence.py` | adaptive next-check interval from status + ETA |
| `resolve.py` | vessel name→id, port→coords, reconnection candidates |
| `legs.py` | schedule → ordered legs |
| `geo.py` | geopy geofence distance + searoute-based ETA projection |
| `engine.py` | **pure** state machine + connection logic (idempotent alerts) |
| `store.py` | local-file persistence |
| `seed_watchlist.py` | auto-pick pilot schedules |
| `run.py` | the tick loop |
| `report.py` | read-only status board + alert timeline viewer |

## Alert types

`PICKUP_ETD_RISK` · `PICKUP` · `DEPARTED_ORIGIN` · `ARRIVED_PORT` · `FINAL_ETA_RISK` ·
`CONNECTION_MADE` / `CONNECTION_TIGHT` / `CONNECTION_MISSED` / `CONNECTION_PENDING` /
`CONNECTION_UNKNOWN` · `COVERAGE_GAP` · `DELIVERED`.

## Tuning (`config.py`)

Buffers: `ON_TIME_TOLERANCE_DAYS=1`, `MCT_DAYS=2`, `ARRIVE_RADIUS_MI=50`,
`APPROACH_RADIUS_MI=150`, `SPEED_FLOOR_KN=1`, `USE_SEAROUTE=True` (realistic-route ETA
distance; falls back to great-circle automatically).
Cadence: `POLL_INTERVAL_MIN=15`, `FETCH_JITTER_SEC=(5,20)`, `MAX_RUNTIME_HOURS=None`,
and the `CADENCE` bucket dict. Start generous; tighten after calibrating against a few
manual checks.

## Caveats (from §2b / §4)

- **Vessel ≠ container** — AIS tracks the ship; silent capacity-rolling on a *feasible*
  connection is invisible. Missed-connection detection is the high-confidence signal.
- **ETA projection is straight-line ÷ SOG** — coarse "plausibly on time," not a real ETA.
- **Edge detection needs ticks over time** — a single `--once` run shows current status
  and any edges since the last run; arrivals/departures/connections resolve as the loop
  accumulates observations.
- `data/` is gitignored (local pilot state).
