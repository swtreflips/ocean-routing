# cosinland.md — COSCO inland rail transit: drivers & calibration

COS-specific notes for v2 synthesis (scrape ports → recreate inland). What
determines the rail leg, why it's sometimes off by a day, and how to calibrate.
For the generic cross-carrier method see [/synth.md](../../../synth.md).

## How the inland schedule is built (v2)

```
yard_eta          = pod_eta + rail_days          # rail_days from coverage.json
total_transit_days = (yard_eta − etd).days
```

`rail_days` is looked up by **(last_cy, port_of_discharge, ocean_service)** —
see `synthesis_policy` in [assets/coverage.json](assets/coverage.json). Measured
during calibration as `rail_days = eta − pod_eta` (yard availability minus sea-
port arrival) and **pooled across all origins** (rail time is origin-independent).

Current accuracy vs. the live carrier (back-test, in-sample): **MAE 0.08 days,
95% exact, 97% within ±1.**

## The primary driver: OCEAN SERVICE

The COSCO **ocean service code** (raw field `service` on the discharge leg;
stored as `ocean_service` in the v2 canonical) is *the* thing that determines
inland rail time. Predictor comparison across all calibration lanes — count of
`(yard, port, key)` groups still spreading ≥3 days:

| lookup key | lanes still spread ≥3 |
|---|---|
| yard + port | 20 |
| yard + port + haulage_mode | 20 |
| yard + port + terminal | 16 |
| **yard + port + ocean_service** | **0** (80% exactly flat) |

Concrete example — **Long Beach → Chicago** (all the same port, all haulage
mode "Rail", yet three different rail times, cleanly split by service):

| ocean_service | rail_days |
|---|---|
| SEA3 | 10 |
| CEN | 12 |
| AAC4 | 14 |

**Why service is the master key:** a COSCO service is a fixed product — fixed
vessel rotation, fixed arrival cadence/day-of-week at the port, a specific
terminal, and a contracted intermodal rail product. It bundles every downstream
rail arrangement, so once you know the service you know the rail leg. Crucially,
the service is present on a **pure port-to-port schedule**, so it's usable for
synthesis without scraping inland.

## Secondary factors (real, but subsumed by service)

- **Discharge terminal** (`deliFacilityCode`, e.g. LGB01 vs LGB08): different
  ramps/cutoffs shift rail time, but service already implies the terminal.
- **Haulage mode** (`inboundTotalTransportModes`: "Rail" = on-dock rail-only vs
  "Truck,Rail" = drayage + rail): logistically meaningful (it's the carrier's
  rail-vs-truck icon) and explains big drayage gaps on some lanes — but it's a
  weak *predictor* (left 20 lanes wide). Service captures it. Kept in coverage as
  display metadata only, not as the lookup key.

Neither needs to be in the lookup key. If a future lane refuses to tighten on
service alone, terminal is the next factor to fold in.

## Why it's sometimes off by ±1 (the calibration target)

The residual error is **not a bug** — it's two real-world effects baked into the
carrier's own numbers:

1. **Day-boundary quantization.** Rail transit is reported in whole days. A
   vessel that discharges late at night effectively starts the rail clock the
   next calendar day → +1. This is why a lane reads e.g. `7–8` or `10–11` rather
   than a single value.
2. **Train-connection dwell.** `rail_days = line-haul + wait-for-next-train`. The
   wait depends on when the box lands relative to the train schedule, which
   fluctuates run to run. That's the within-service spread (e.g. Long Beach
   "Rail" ranging 10–14 across terminals/days).

Both are stochastic. The mode is the single most-likely value; reality lands on
mode ± ~1 depending on the night/dwell luck of a given sailing.

## Calibration levers

When a lane is consistently off, tune it here — all from data already in
`coverage.json` (`services[svc]` carries `rail_days`, `min`, `max`, `n`,
`samples`):

1. **Point-estimate choice.** Default is `rail_days` = mode. If a specific
   service *systematically* overestimates, lower it — set `rail_days` to the
   median, the lower of a two-value cluster, or `min` for an aggressive estimate.
   Edit `services[svc].rail_days` in coverage.json; the synthesizer just reads it.
2. **Grow the sample.** Re-run calibration periodically. Mode stabilizes as `n`
   rises; `samples` are retained so `extract_connections.py` recomputes on each
   pass.
3. **Per-service manual override.** Once you've compared a lane to the live
   COSCO site and confirmed a constant bias, hardcode the corrected `rail_days`
   for that `(yard, port, service)`. It survives re-extraction only if you also
   update the samples — otherwise note it and re-apply.
4. **Show the range, not just the point.** `synthesis_policy.show_range` surfaces
   `min`–`max` in the app so a booking sees the envelope (e.g. "9–13 days"),
   which is honest about the dwell variability instead of pretending it's one day.
5. **Unknown service → fallback.** A service not seen during calibration falls
   through to the port-level `fallback` aggregate (wider). When you see
   `rail_source: "fallback"` show up often for a lane, recalibrate to capture
   that service.

## COS gotchas

- **Canadian gateways** (Vancouver, Prince Rupert) are real discharge ports for
  inland US via rail — `type:"port"`, `gateway:true`. Make sure they resolve in
  `cos_cities.json` with standardized "City, BC" keys.
- **`haulage_mode` can be null** on some routes; harmless — the service-keyed
  lookup doesn't depend on it.
- **Denver** had no calibration data (no schedules returned) → `connections`
  empty; nothing synthesizes for it until recalibrated.
- **Never pool service across carriers** — a COSCO service code means nothing to
  another line; rail products are carrier-specific.

## File map

| concern | file |
|---|---|
| lookup table (services + fallback + policy) | [assets/coverage.json](assets/coverage.json) |
| build the table from calibration canonicals | [extract_connections.py](extract_connections.py) |
| capture `ocean_service` etc. onto canonicals | [utilsv2.py](utilsv2.py) |
| synthesize inland from port scrapes | [synthesize_inland.py](synthesize_inland.py) |
| accuracy back-test | [backtest_synthesis.py](backtest_synthesis.py) |
