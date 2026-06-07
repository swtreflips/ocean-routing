# Alerts — Operator's Guide

A plain-language guide to **using** the alert engine: the commands, what each one does,
what output to expect, and how to read the alerts. For module internals and design
rationale, see [README.md](README.md) and [`../scheduleTracker.md`](../scheduleTracker.md).

> **What it is in one line:** it watches your chosen schedules, pulls each vessel's live
> AIS position from MarineTraffic, and tells you whether the shipment is on track at
> pickup, at transshipment, and at final arrival — **vessel-level schedule-adherence
> monitoring with delay early-warning** (not container-level tracking — see *Limits*).

---

## 0. Before you start (once)

- **Run from the repo root** (`...\ocean-routing>`), not from inside `alerts\`.
- Always invoke as a **module**: `python -m alerts.<name>` (never `python alerts\run.py` —
  it's a package, you'd get "no known parent package").
- Use the **`schedulesenv`** venv (has supabase, geopy, patchright). Activate it, or call
  its python by full path.
- A **`.env`** must exist at the repo root with `SUPABASE_URL` / `SUPABASE_KEY`.
- `run` opens a real Chrome window and prints emoji → on cmd set `set PYTHONUTF8=1` first.

---

## 1. The three commands

```cmd
python -m alerts.seed_watchlist     :: 1. choose WHAT to track   -> data/watchlist.json
python -m alerts.run --loop         :: 2. DO the tracking        -> alerts + state
python -m alerts.report             :: 3. SEE results (anytime)  -> status board + timeline
```

Mental model: **seed = subscribe**, **run = engine**, **report = dashboard**. They only
talk to each other through files in `alerts/data/`.

---

## 2. `seed_watchlist` — pick what to track

Run this **once** to populate the watchlist (re-run to add/replace shipments). It does no
AIS — it queries Supabase `schedules_latest`, picks ~6 shipments (transshipment + direct,
near-term ETD), writes them to `data/watchlist.json`, and prints a **coverage report**.

**What to expect:**
```
[seed] candidates: 806 (ts_named=664 ts_tbn=13 direct=129)
[seed] wrote 6 shipments -> ...\alerts\data\watchlist.json

[seed] resolution coverage:
  MSC Nhava Sheva, India -> Singapore (ETD 2026-06-21)
      vessel[0] 'MSC DOUALA VIII': vid=8905859        <- resolved to a MarineTraffic id
      vessel[1] 'MSC TRIESTE': vid=2949
      port  NHAVA SHEVA: OK Nhava Sheva, India
      ...
```
- `vid=...` = the vessel was matched in your `vessels` table → trackable.
- `TBN` / `UNRESOLVED` = not trackable (vessel not nominated yet, or no name match).
- `port ... NO MATCH` only matters for an intermediate port; POL/POD use canonical coords.

To track your own shipments instead of the auto-pick, hand-edit `data/watchlist.json`
(copy an entry, change the fields).

---

## 3. `run` — the engine

`--once` does a single pass then exits; `--loop` keeps one warm browser and rechecks on
the adaptive cadence until `MAX_RUNTIME_HOURS`. Each **pass**:

1. loads the watchlist + prior state,
2. selects **due** shipments (first run: all),
3. opens **one** Chrome and fetches each needed vessel's position + voyage,
4. evaluates the leg state machine → alerts,
5. computes each shipment's **next check**, saves state, appends alerts.

**What to expect:**
```
[acquisition] waiting 11.9s before fetching 8905859...   <- anti-bot pause, then fetch
[acquisition] waiting  7.2s before fetching 2949...
[run] pass: 6 due, 5 new alert(s); debug -> ...\debug\20260607-044544.log
  [INFO  ] PICKUP_ETD_RISK/ON_TRACK (MSC leg 0) Projected arrival at NHAVA SHEVA ...
  [MEDIUM] PICKUP_ETD_RISK/AT_RISK (MSC leg 0) ... (+0.3d).
```
A pass of ~6–7 vessels takes ~1–2 min (mostly the 5–20 s anti-bot pauses). On a re-run
where nothing is due yet, you'll see `nothing due — skipping browser`.

---

## 4. `report` — read the results

Read-only viewer (no browser/DB). Run it anytime, even while `--loop` is going.

```cmd
python -m alerts.report                      :: status board + last 24h timeline
python -m alerts.report --since 48h          :: window: 90m / 12h / 7d
python -m alerts.report --severity medium    :: info < medium < high
python -m alerts.report --shipment 11b3a8e5  :: one shipment (id prefix)
python -m alerts.report --all                :: whole timeline, no window
```
The **status board** shows where each shipment is now (active leg, status, hours to next
check). The **timeline** is the event history. The **summary** counts alerts by severity.

---

## 5. Reading the alerts

| Alert | Meaning |
|-------|---------|
| `PICKUP_ETD_RISK` (`ON_TRACK`/`AT_RISK`/`LIKELY_LATE`) | While the first vessel is *en route* to the load port, an **estimate** (realistic-route distance ÷ speed) of whether it'll arrive by ETD. |
| `PICKUP` (`AT_PICKUP`/`AT_PICKUP_LATE`) | The vessel is **at** the load port — observed fact, no longer an estimate. |
| `DEPARTED_ORIGIN` (`ON_TIME`/`LATE`) | It left the load port = the **actual** departure (vs planned ETD). |
| `ARRIVED_PORT` | Reached this leg's destination. |
| `CONNECTION_MADE` / `CONNECTION_TIGHT` / `CONNECTION_MISSED` | At a transshipment: onward vessel left **after** arrival + buffer (good) / within the buffer (roll risk) / **before** (missed → delay). |
| `CONNECTION_PENDING` | Arrived at the T/S; waiting on the onward vessel to depart. |
| `CONNECTION_UNKNOWN` / `COVERAGE_GAP` | A leg's vessel is `TBN`/unresolvable → can't track it. |
| `FINAL_ETA_RISK` | On the last leg, estimate of arrival vs the scheduled ETA. |
| `DELIVERED` | Final vessel reached the discharge port (vessel-level). |

**Severity:** `info` = milestone/on-track · `medium` = at-risk, watch it · `high` = late /
missed, act on it. Alerts are **idempotent** — they fire once per change, so every line in
the timeline is a real event, not repetition.

**Confidence rises as the vessel gets closer** (see scheduleTracker.md §2b and the pickup
ladder): far at sea = an *estimate*; anchored at the load port = present, awaiting berth;
**moored** at the load port = cargo ops underway. The projected-arrival number stops
mattering once it's physically there.

---

## 6. Cadence — how often it checks (what to expect)

Each shipment is checked at a frequency set by its active vessel's status + ETA:

| Active vessel | Next check |
|---|---|
| Moored / At Anchor | ~45 min |
| Approaching / ETA today | ~2 h |
| ETA < 4 days | ~4 h |
| ETA < 7–10 days | ~8 h |
| ETA > 10 days / no ETA mid-ocean | ~12 h |

So vessels in port operations are watched closely; mid-ocean vessels rarely. In `--loop`
the engine wakes every 15 min but only fetches shipments that are actually due.

---

## 7. What gets written (`data/`, gitignored)

| File | What it is |
|------|-----------|
| `watchlist.json` | the plan — what you're tracking |
| `state.json` | memory between passes — cursor, geofence edges, next_check, fired alerts |
| `alerts.jsonl` | the event timeline (one alert per line) |
| `debug/<ts>.log` | per-pass trace of *why* each decision was made |
| `run.log` | combined stdout when launched via `run_loop.bat` |

`state.json` is what makes it **restartable** — kill it, relaunch, it resumes (this is how
the daily-restart scheduler works).

---

## 8. Running it 24/7 (daily restart)

Recommended for an always-on laptop (`MAX_RUNTIME_HOURS = 23.5` is already set):
- Point **Windows Task Scheduler** at [`run_loop.bat`](run_loop.bat), **Daily**,
  **"Run only when user is logged on"** (the visible Chrome needs a desktop).
- Set the power plan to **never sleep on AC**, and stay logged in.
- Each day it runs ~23.5 h, exits, and the scheduler relaunches it with a fresh session.

Full steps in [README.md](README.md#running-unattended-always-on-laptop-daily-restart).

---

## 9. What to expect — and the limits (read this)

**It's good at:** early warning of pickup slippage, transshipment **missed-connection**
detection (high confidence), and final-ETA drift vs the carrier's optimistic dates.

**It cannot tell you (be honest about these):**
- **Vessel ≠ your container.** AIS shows the *ship*. It can't see if your box was
  **rolled** (left behind) even when the ship sails on time. The connection-integrity
  check recovers most of this at transshipment, but capacity-rolling on a *feasible*
  connection is invisible.
- **Forward legs are often `TBN`** — untrackable until the carrier names the vessel.
- **The projected arrival uses the realistic sea route** (searoute distance ÷ current
  speed), so it routes around land/canals rather than a straight line — but it still
  assumes the *current* speed holds, so it remains an estimate (no weather/speed model).
  It only matters in the far phase; proximity/status replace it as the ship arrives.
- **AIS can be hours stale** mid-ocean (satellite), and a 50-mi geofence blurs exact
  arrival timing. Last-mile inland rail isn't tracked at all.

Treat it as a **decision-support / exception-detection** tool over a curated set of
important shipments — lead with the alerts, not certainty.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `attempted relative import with no known parent package` | You ran `python alerts\run.py`. Use `python -m alerts.run` from the repo root. |
| `UnicodeEncodeError` on cmd | `set PYTHONUTF8=1` before running (only `run` needs it; `report` doesn't). |
| `SUPABASE_URL / SUPABASE_KEY not set` | No `.env` at the repo root, or wrong cwd. |
| `nothing due — skipping browser` | Working as intended — cadence is holding all shipments; re-run later. |
| Many shipments stuck, `run.log` full of fetch errors | MarineTraffic session went stale — the daily restart re-bootstraps it; if it happens within a day, the session needs mid-loop re-bootstrap (future work). |
| A vessel shows `COVERAGE_GAP`/`TBN` | Expected for un-nominated forward legs; nothing to do until the carrier names it. |

---

## 11. Tuning (`config.py`)

`ON_TIME_TOLERANCE_DAYS` (1), `MCT_DAYS` (2, transshipment buffer), `ARRIVE_RADIUS_MI`
(50), `APPROACH_RADIUS_MI` (150), `USE_SEAROUTE` (True — realistic-route ETA distance;
False = straight-line), `POLL_INTERVAL_MIN` (15), `FETCH_JITTER_SEC` (5–20),
`MAX_RUNTIME_HOURS` (23.5), and the `CADENCE` buckets. Start generous; tighten after
comparing a few alerts against reality.
