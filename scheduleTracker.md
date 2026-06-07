# Schedule Tracker — Framework

Vessel-level **schedule-adherence monitoring with delay early-warning**. Track a
shipment leg-by-leg against its carrier-published plan, using live AIS positions, and
surface where it's late / on-time at pickup, at transshipment, and at final arrival.

> Status: **framework / concept**. No implementation yet — this is the shared mental
> model to build against.

---

## 1. What the data already gives us

Carrier schedules live in `schedules` / `schedules_latest`. A transshipment row looks like:

```
route_ports:     ["NHAVA SHEVA", "COLOMBO", "SINGAPORE"]   # ordered ports
vessel_sequence: ["AGIOS DIMITRIOS/IV624A", "TBN/TBN"]      # ordered vessels
etd / eta / pod_eta / transit_time_days                      # ENDPOINT dates only
```

Key facts that shape everything:
- **Legs are free:** `legs = zip(route_ports, route_ports[1:], vessel_sequence)`.
  N ports → N−1 legs. Each leg = `{ from_port, to_port, vessel }`.
- Vessel strings carry a **`/voyage` suffix** → strip it before joining to `vessels`.
- **`TBN/TBN`** = vessel not yet nominated → untrackable until a later re-ingest fills it.
- Only **origin ETD + final ETA** exist — there are **no per-leg milestone dates**.

---

## 2. Mental model

### Three layers — never mix them
1. **Plan** (immutable): the legs + the two real dates (ETD on leg 0's departure, ETA on
   the last leg's arrival).
2. **Actuals** (observed): a *time series* of vessel position samples + derived geofence
   events (entered / left a port).
3. **Status** (derived): plan vs actual → on-time / late deltas, projected ETA, alerts.

### Tracking = a state machine walking the legs with a cursor
```
PENDING → APPROACHING_PICKUP → IN_TRANSIT → ARRIVED ──advance cursor──▶ next leg
                                                         (last leg ARRIVED = DELIVERED)
```
- The cursor advances **only** on a geofence **ARRIVAL** at the leg's `to_port`.
- **Time never advances state** — it only *colors* status (early / on-time / late).
- **Transshipment is not a special case.** Every leg uses the same four primitives:
  1. resolve `vessel → vessel_id`
  2. fetch position
  3. `is_near(from_port)`  → departed pickup
  4. `is_near(to_port)`    → arrived (advance cursor)
  A handoff is just *"cursor advanced, active vessel changed."* First vs last leg differ
  only in which planned date they're compared to.

### Which vessel do I plot? — a pure function of the cursor
`active_vessel = vessel_sequence[cursor]` → strip `/voyage` → resolve via `vessels`
(+`aliases`) → `vessel_id` → MarineTraffic position. `TBN` ⇒ flag, can't plot.

### Geofence = edge detection, not a boolean
`is_near` is point-in-time. Sample on a cadence; an **event** is a *transition*:
- `false → true` at `to_port`  = **ARRIVAL**  (advance cursor)
- `true → false` at `from_port` with speed > 0 = **DEPARTURE**

Detecting edges requires position history (you can't see a transition from one sample).

---

## 2b. Transshipment connection-integrity model

This is the answer to *"vessel ≠ container"* at a transshipment. We can't see the box,
but we **can** see whether the connection was physically possible — by comparing the
inbound vessel's arrival against the outbound vessel's departure at the T/S port.

### Necessary, not sufficient — and the asymmetry that follows
The connection-timing check is a **necessary condition, not a sufficient one**:
- **Missed connection** (B left before A could transfer) → *high confidence* the box did
  **not** make it → high confidence of delay. This is the **most valuable, most certain**
  signal the tracker produces, and it fires days early.
- **Feasible connection** (A transferred in time) → confidence **raised, not proven**.
  The box can still be **capacity-rolled** (B was full). AIS can't see that — it's the
  one residual that needs carrier event data. So "high confidence," never "certain."

### The buffer: it's not "before departure", it's "before cutoff"
The box must be **discharged from A, moved across the terminal, and loaded onto B before
B's T/S cutoff** — which is hours-to-days before B's actual departure. So the real test
uses a **minimum connection time (MCT)** buffer (often **1–3 days** at big hubs):

```
A_arrival  +  MCT  ≤  B_departure
```

That yields three states, not two:

| Observed timing | State | Confidence box is on schedule |
|---|---|---|
| A arrived ≥ MCT before B left | **Connection made (comfortable)** | High (residual: capacity roll only) |
| A arrived < MCT before B left | **Connection tight / at-risk** | Medium — likely rolled, flag it |
| B left before A arrived (or before A+discharge) | **Connection missed** | ~0 on-schedule; high confidence of delay |

### It reuses the same primitives (no new infra)
Both edges come straight from the geofence at the T/S port:
- **A arrival** = `is_near(ts_port)` flips false→true for vessel A.
- **B departure** = `is_near(ts_port)` flips true→false (speed>0) for vessel B.

The connection check is just: at a T/S node, compare A's arrival edge vs B's departure
edge with the MCT buffer. It slots into the state machine as the gate that decides
whether the cursor advances *successfully* or trips a **missed-connection** branch. Free
bonus: the A-arrival → B-departure gap is a **T/S dwell metric** vs expected.

### After a miss: candidate next vessels (no AIS needed)
Candidates come from the `schedules` table itself: same carrier, same T/S port → same
final destination, departing *after* A's arrival, ordered by ETD. That's the
re-connection queue to start tracking. It's inference (can't confirm which sailing got
the box without carrier data), but it's a principled "where to look next."

### Net effect
Converts the biggest assumed risk (silent missed connection) into a **detected event**.
Upgrades the tool from "first-leg alert" to **connection-integrity monitor**. Residual
unknowns stay the same: capacity-rolling on a *feasible* connection, and TBN forward legs.

---

## 3. What has to be built (the only genuinely new pieces)

- **Position-history table** — track points over time. *You cannot draw a path, or detect
  arrival/departure events, from a single latest fix.* This is the core new persistence.
- **Tracking-state / watchlist** — one record per shipment being watched: schedule ref,
  cursor (active leg), per-leg state, last events.
- **Scheduled poller** — each tick, for every active shipment: find active leg + vessel →
  fetch position → append to history → evaluate geofence edges → advance cursor →
  recompute status / alerts.
- **Dashboard = pure reader** — active leg's track + plan overlay + status badges.

### Reuses what already exists
| Need | Existing asset |
|------|----------------|
| The plan (legs, dates) | `schedules_latest` |
| Vessel name → id | `vessels` table + `aliases` (strip `/voyage`) |
| Position sampler | `positionVoyage.py` (warm MarineTraffic session) |
| Leg geofence | `is_near(lat, lon, port, threshold_miles)` RPC |
| Live "where is it really" | `nearest_ports(lat, lon, limit, types)` RPC |

---

## 4. Operational value (honest)

### High ROI — it's the alerts, not the map
- **Pickup / ETD-slippage early warning** (2–3 days out): is the first vessel actually
  heading to origin, and will it make the ETD? Highest value, lowest ambiguity — a slip
  at origin cascades through the whole shipment.
- **Final-ETA projection vs carrier ETA**: AIS ground truth vs carrier optimism.
- **Transshipment connection-risk flagging**: T/S is where delays and rolled containers
  concentrate.
- **One AIS-verified pane of glass** across all inbound.

### Hard limitations — don't oversell
1. **Vessel position ≠ your container.** AIS shows the *ship*, not whether your box is
   aboard or got **rolled** at T/S. Proxy for cargo, *not* cargo-level tracking. (#1.)
   *Largely recovered at transshipment by the connection-integrity model (§2b); the
   residual is silent capacity-rolling on an otherwise-feasible connection.*
2. **Forward legs are often `TBN`** — untrackable until nominated.
3. **No per-leg planned dates** — endpoints assessable, midpoints only via estimate
   (e.g. `transit_time_days` proportion).
4. **Vessel-identity matching risk** — name / voyage variants → wrong ship.
5. **AIS coverage / latency** — mid-ocean is satellite (sparse, delayed, sometimes
   dark); a 50-mi geofence tolerates this but blurs exact timing.
6. **Last mile** (inland rail POD → final CY) isn't AIS-trackable.
7. **Acquisition fragility** — a warm-browser scrape doesn't scale to many vessels at
   high cadence (bot-detection); a paid AIS API is the real path if this becomes core.

**Net framing:** *"vessel-level schedule-adherence monitoring with delay early-warning."*
Valuable as exception-detection + ETA-validation over a curated shipment set; not a
container-delivery guarantee.

---

## 5. Recommended phased build

- **Phase 1 — Alerts only, leg 0, no map, no history.** Pickup ETD risk: resolve leg-0
  vessel, fetch position, `is_near(origin)` + rough closing-speed/ETA-to-origin vs ETD.
  Cheapest, highest value; sidesteps the TBN and midpoint-date gaps.
- **Phase 2 — Position-history table + leg state machine + cursor advance** across all
  legs (transshipment handoff handled generically).
- **Phase 3 — Dashboard** (map of active leg track + plan overlay + status badges) and
  full alerting (late-to-T/S, projected-late-to-POD).

---

## 6. Target stack

- **DB / backend:** Supabase (Postgres + PostGIS) — already holds `schedules`, `vessels`,
  `ports`, and the `is_near` / `nearest_ports` RPCs. New tables when built: position
  history + per-shipment tracking state. `geom` is WGS84, so it feeds MapLibre as GeoJSON
  directly.
- **Hosting / API:** Vercel + Node — home for the poller (Vercel Cron) and serverless API
  routes feeding the UI.
- **Frontend:** React + MapLibre GL JS — plot active leg track + plan overlay + status
  badges; reads from Supabase (directly or via Node API routes).

---

## 7. Open decisions (to settle before implementation)

- Scope: curated watchlist vs all schedules; how many vessels tracked in parallel.
- Poll cadence (Vercel Cron tick); whether the warm-browser fetch scales or a paid AIS
  API is needed.
- Exact shape of the two new Supabase tables (position history + tracking state).
- Geofence precision: simple `is_near` radius vs port polygons / dwell-time confirmation
  to reduce pass-by false positives.

---

## 8. Infrastructure model

**The seam: Supabase sits between two halves that never talk directly.** The UI writes
*intent*; the worker writes *observations*; each side only reads what the other wrote.

```
┌─────────────────────────┐         ┌──────────────────────────┐
│  VERCEL (UI + API)       │         │  WORKER / POLLER          │
│  React + MapLibre        │         │  (always-on host, Python) │
│                          │         │                           │
│  • show schedules        │         │  every N hours:           │
│  • create/edit           │         │   1 read active shipments │
│    InboundShipments  ────┼──┐   ┌──┼─> 2 resolve leg + vessel  │
│  • read positions/alerts │  │   │  │   3 fetch AIS position    │
│    → map + status badges │  │   │  │   4 geofence / is_near    │
└─────────────────────────┘  │   │  │   5 advance state machine │
                            writes reads  6 write pos/events/    │
                             │   │  │      alerts ───────────────┼┐
                             ▼   │  └───────────────────────────┘│
                      ┌──────────┴────────────────────────────┐  │
                      │  SUPABASE (Postgres + PostGIS)         │◄─┘
                      │  schedules · vessels · ports           │
                      │  InboundShipments (watchlist)          │
                      │  positions (history) · shipment_state  │
                      │  alerts/events  ·  RPCs: is_near, …    │
                      └────────────────────────────────────────┘
```

**The poller is NOT on Vercel.** Vercel functions are serverless (short-lived,
stateless, no persistent browser); the AIS fetch is a warm, authenticated Chromium
session that must stay alive and keep its `mt_profile` on disk. So the worker runs on a
small always-on host (laptop for the pilot; Railway/Render/Fly/VPS later). Vercel Cron
can trigger lightweight Node checks, but not the browser scrape.

**"Constantly tracking" = a control loop**, not a live stream or one-process-per-shipment:
```
loop forever:
    shipments = InboundShipments WHERE status = 'tracking'
    for s in shipments:
        leg, vessel = active_leg(s)          # from cursor in shipment_state
        pos = fetch_position(vessel_id)      # the browser part
        append positions(pos)
        evaluate geofences (is_near edges)   # arrival/departure
        advance state machine + write alerts
    sleep(interval)                          # AIS is hours-fresh; every few hours is fine
```

**Two rules that keep it production-grade:**
1. **All state in the DB, never worker memory** — cursor, per-leg status, "alert already
   sent." Makes the worker crash/redeploy-safe: it resumes on the next tick.
2. **Split `acquisition` (fragile browser) from `logic` (pure state machine)** — the day
   you buy a paid AIS API you swap only acquisition; everything else is unchanged.

**Pilot → prod is the same code path.** Pilot: watchlist from manual rows/local file,
loop on the laptop. Prod: watchlist comes from users clicking "track" in the UI, loop
runs on a hosted worker. Identical logic — the pilot *is* the engine.

> Phase 1 implements this with **local files** instead of the Supabase
> InboundShipments/positions/state tables (see [`alerts/`](alerts/)). Promoting those
> local files to Supabase tables is the Phase 2 step; the engine logic doesn't change.

---

## 9. Future enhancements

### Realistic-route ETA via `searoute` — **IMPLEMENTED (Phase 1)**

The projected arrival now uses the realistic marine route distance from
[`searoute`](https://pypi.org/project/searoute/) ÷ current speed, not a straight line.
`geo.route_distance_km` routes around land and through canals; `geo.project_eta` uses it,
with an automatic great-circle fallback if searoute is missing or can't route a pair
(`config.USE_SEAROUTE` toggles it). Localized change — only `geo.py` touched; engine /
acquisition / cadence unchanged (the acquisition/logic split paying off). Measured impact:
straight-line was optimistic by ~10–27 h on typical legs (worst where land forces a detour,
e.g. ~+760 km routing around Sri Lanka on Nhava Sheva → Singapore).

Accuracy ladder (where we are / what's left):
1. ~~great-circle distance ÷ current speed (optimistic)~~ — fallback only.
2. **searoute distance ÷ current speed** — *current default*. Fixes the *distance*.
3. **Still open:** also fold in MarineTraffic's `voyage.arrivalTimestamp` (already fetched
   for cadence; bakes in MT's own *speed* model) — searoute fixes distance, MT's value fixes
   distance + speed; they're complementary.

Caveat: searoute is a graph approximation (not AIS-historical lanes) and `distance ÷ speed`
still uses the instantaneous SOG (no weather), so it remains an estimate — just no longer an
optimistic straight-line one.

### Route polyline on the map (Phase 3, not yet)

`searoute` also returns `route.geometry["coordinates"]` — the exact LineString to draw the
vessel's route on the MapLibre map. We compute distance from it today but don't persist the
polyline yet; wire it into the dashboard layer when Phase 3 lands.
