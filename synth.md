# synth.md — Building a carrier's full schedule set from one inland scrape

The method, per carrier: **scrape origin→inland directly**, then **derive the
port-to-port schedules from the ocean legs inside those results**, and
**secondary-scrape only the ports that don't rail inland**. The inland scrape is
the primary, exact data; ports fall out of it almost for free.

Reference implementation is **COS** (COSCO): `mainv3.py` (inland scrape) +
`derive_ocean.py` (derive ports). Both under `src/carriers/cos/`.

## Why this approach

Scraping inland directly makes the two hard problems of the old
"scrape-ports-and-estimate-inland" model **disappear**:

- **Exact rail times.** The inland schedule carries the carrier's *actual* rail
  transit. No estimation, no discriminator to hunt, no ±1-day error.
- **Origin-specific routing for free.** You observe exactly which port each
  origin actually routes through to each yard (Nhava Sheva→Chicago via Norfolk;
  Qingdao→Chicago via LA/Vancouver). You can't invent a routing the carrier
  doesn't offer, because you never synthesize — you scrape it.

And it's simpler: no rail-stats coverage table, no service-nested lookups, no
synthesis step, no calibration/production split.

**The one cost:** more calls per refresh — inland yards outnumber ports, and you
scrape inland every refresh. Affordable if `origins × inland yards + a few
secondary ports` fits your time/rate budget. Run that number before committing.

## The two data shapes

```
Inland schedule (scraped):   origin → (ocean → discharge port P) → (rail → inland yard Y)
                             pod_eta = arrival at P ;  eta = availability at Y
Derived port-to-port:        truncate at P → last_cy = P, eta = pod_eta (drop the rail leg)
                             transit = pod_eta − etd        # ocean only
```

Port dwell/availability days at P are ignored (`eta = pod_eta`) — good enough for
ocean-only options. Dedup derived schedules by ocean sailing: many inland yards
share the same origin→P voyage.

---

## Phase 1 — coverage.json (the carrier's universe)

`assets/coverage.json` is the carrier's **full port universe** (from the external
port-discovery project) **plus the inland yards** (from `assets/<code>_yards.geojson`,
property `CityYard`). Each key is typed `"port"` or `"inland"`.

- **inland** = what you scrape directly (Phase 2).
- **port** = the reference set for the missing-ports diff (Phase 4).

Classification need not be perfect — v3 is robust to it. A coastal city marked
`inland` just gets inland-scraped; one marked `port` that the carrier actually
rail-serves gets caught by the secondary scrape. Either path covers it.

---

## Phase 2 — Inland scrape (origins × inland yards) — PRIMARY

Matrix = every origin in `data/origins.csv` × every `type=="inland"` key. Each
result is an **exact, origin-specific** inland schedule. Build enriched canonicals:
`pod_eta` = last ocean leg arrival, `eta` = final (yard) arrival, plus the usual
fields. No estimation, nothing to tune.

Reference: [src/carriers/cos/mainv3.py](src/carriers/cos/mainv3.py).

**✅ Checkpoint:** files landed and parse; note any yard that returned nothing
(the carrier may not rail there from your origins — fine, just empty).

---

## Phase 3 — Derive port-to-port

For each inland schedule, truncate at its discharge port: `last_cy = P`,
`eta = pod_eta`, drop the rail leg, recompute `transit = pod_eta − etd`. Dedup by
ocean sailing `(origin, P, etd, mother_vessel, vessel_sequence)`.

Reference: [src/carriers/cos/derive_ocean.py](src/carriers/cos/derive_ocean.py)
(stdlib-only, re-runnable, no scrape).

---

## Phase 4 — Find missing ports

Collect the set of discharge PODs observed across the inland scrape (the
**railing** ports). Diff against the port universe (`type=="port"` in coverage):

```
missing_ports = port_universe − observed_PODs
```

These are ports the carrier calls but rails nothing inland from (pure-ocean
ports) — or rail-served coastal points reached via a hub. Either way the
secondary scrape handles them. The missing set is **per-origin** (origins reach
different ports), so compute it from each origin's just-scraped data.

---

## Phase 5 — Secondary scrape (origins × missing ports)

Scrape just the missing ports directly (port-to-port). This fills the
pure-ocean ports the inland scrape can't reach, plus any rail-served coastal
point (the carrier returns whatever routing it offers — direct ocean or
ocean+rail — and you store it as-is). No reclassification, no synthesis.

---

## Phase 6 — Push to Supabase

Push the three sets — inland canonicals, derived port canonicals, secondary port
canonicals — via the shared env-based ingest (`src/ingest/ingest.py`,
`ingest_new_canonicals(code, canonical_dir, ledger_path)`). Idempotent (upsert on
`schedule_hash`), best-effort, ledger-tracked, separate ledger per set.

---

## Operational gotchas (carrier-agnostic)

### Junk / unrelated results (usually a missing location code)

Some carriers return schedules **unrelated to the query** — foreign fragments
that neither start at the origin nor end at the destination (e.g. an EMC
`→ Vancouver` query returning routes to Penang / Mombasa / Vietnam). Root cause
is usually a destination whose **query code is missing/empty**: the carrier
can't resolve it and returns garbage instead of an honest "not found."

- **Detect:** a schedule's final destination isn't a US/CA location
  (`"City, XX"`, XX a state/province). Compare a known-good port (≈100%
  on-destination) against the suspect (≈0%) — e.g. EMC Seattle 130/130 valid vs
  Vancouver 0/58 before its code was fixed.
- **Fix #1 — real location code.** ⚠️ When adding a port/gateway, verify the query
  returns schedules that **actually reach the destination**, not merely that
  *some* results come back. A missing code returns results, just the wrong ones.
- **Fix #2 — NA-destination filter** in the canonical builder: drop any schedule
  whose final destination isn't `"City, <US-state|CA-province>"`. Kills foreign
  junk while keeping legit adjacent-port cross-results (Seattle query → Tacoma).
- **Fix #3 — pre-flight** warns on an empty location code, not just a missing key.
- **DB note:** junk pushed before the filter stays (upsert doesn't delete) — clean
  with a targeted delete on non-NA `port_of_discharge`.

### Gateways & normalization

- **Discovered gateways** (Tacoma, Vancouver, Prince Rupert) show up as discharge
  PODs in the inland scrape even though they're not US inland yards — they belong
  in the port universe; make sure they're there and queryable for the secondary
  scrape.
- **Normalize port names.** Carriers format ports inconsistently — US as
  `"City, ST"`, Canadian as `"City, Bc, Canada"` (3-part). Make `normalize_pod`
  fall back to a bare-city lookup so `Vancouver, Bc, Canada → Vancouver, BC`.
  Consistent names matter so the missing-ports diff and dedup line up.

### Other

- **No-data yards/ports** — leave them be; the carrier may genuinely not serve
  that lane from your origins.
- **Derived-port completeness** — in principle, deriving ports from inland could
  miss origin→P *voyages that never rail inland*. Observed so far the carriers
  reuse the same voyages and just layer rail on top (derived ≈ direct), so it's a
  non-issue — but if port-direct completeness ever matters, confirm per carrier
  with a one-off derived-vs-direct sailing count on a busy gateway.
- **Call volume** is the real budget line — `origins × inland yards` each refresh.
  Trim origins for surveys; size it before scaling.

## Reference file map (COS, v3)

| Phase | File |
|------|------|
| 1, 4 | `assets/coverage.json` (port universe + inland classification) |
| 2 | `mainv3.py` (inland scrape) |
| 3 | `derive_ocean.py` (derive port-to-port) |
| 5 | secondary missing-ports scrape (per-origin diff → scrape) |
| 6 | `src/ingest/ingest.py` (shared push) |
