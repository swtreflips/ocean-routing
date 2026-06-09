# ROLL.md — Validating vessel-as-proxy & measuring silent rolls

> **Status: future development.** Design note for when tracked shipments start resolving
> and we have real outcomes to reconcile against. Nothing here is built yet.

## The question this answers

The engine is **vessel-level** monitoring used as a *proxy* for container outcomes (see
`scheduleTracker.md` §4 / §2b). The honest open question:

> How reliable is "vessel on time / connection feasible" as a predictor of "**my container**
> actually arrived on time"?

We don't guess this — we **measure it** once we have ground truth. The goal is to turn the
"vessel ≠ container" caveat into a **measured error rate**, segmented by where it matters.

## The one blind spot we're really chasing

AIS can't see a **silent capacity roll**: inbound vessel A arrives, onward vessel B departs
with comfortable margin (connection *feasible*), but the box was bumped anyway because B was
full. System says green; reality is late. We **cannot observe the roll itself** from AIS.

Key insight: **measure it by its consequence, not the event.** We never need to see a roll —
we only need each shipment's real outcome, then count how often "system said on-track" ended
"container late."

## How to measure it — the confusion matrix

For each resolved shipment, compare the engine's verdict against the actual outcome:

|                       | Container on time | Container late |
|-----------------------|-------------------|----------------|
| **System: on-track**  | ✅ true reliability | ⚠️ **false confidence** ← silent rolls live here |
| **System: risk/missed** | false alarm       | ✅ true catch   |

- **Top-right (false confidence)** is the headline number — "how often vessel-on-time
  misled me." It contains silent rolls *and* speed-estimate misses; both matter to trust.
- **Bottom-right (true catch)** is the value the system adds.
- **Bottom-left (false alarm)** is the cost (crying wolf) — tune buffers to keep it low.

## The real ROI signal — beat the forwarder

Concordance ("forwarder says next week, system says next week") only *confirms* what we'd
learn anyway. The signal that justifies running this is the **lead-time catch**: the system
flagged a missed/high-risk connection **before** the forwarder told us. Track, per alert:

    detected_at (system)   vs   informed_at (forwarder)   ->   lead time (days early)

A positive average lead time on true catches = net-new operational value.

## Honest priors (don't assume rolling is rare)

- Published **transshipment rollover rates at major hubs run double digits** (commonly cited
  ~15–30% rolled at least once, varying widely by port / carrier / season). So rolling is
  *not* uncommon in general.
- **But** most rolls happen *because* the connection was tight or missed — which the engine
  **already catches**. The rolls that *fool* us are only the subset with **comfortable
  margin that got bumped anyway** (pure overbooking). That subset is smaller than the
  headline rate. Measure that subset, not the industry number.

## What survives even in a high-roll world

Reliability never collapses to zero. Two signals hold regardless of roll rate:
1. **First-leg / pickup signals** — we control the booking and *observe* pickup & departure
   from the POL. Always high-confidence.
2. **Hard missed-connection catches** — "B departed before A arrived" is *observed*, not
   inferred. 100% reliable.

What degrades with rolling is only the **"feasible connection ⇒ delivered on time"**
inference. So the conservative fallback isn't "trust nothing past leg 1" — it's:
> trust leg-1 fully · trust missed-connection alerts fully · **discount** the
> feasible-connection green light by the measured roll rate.

## It won't be one number — segment it

False-confidence rate will vary by **carrier**, **transshipment hub**, and **lane**. Once
outcomes exist, break the matrix down by carrier and T/S port. The useful output is
"feasible-connection false-confidence is X%, concentrated at hubs Y and Z" — telling us
*where* to trust the proxy vs. lean on the forwarder.

## What to build (when data exists)

1. **Ground-truth capture** — a way to record each shipment's actual outcome
   (`actual_arrival` / `actual_status` like `on_time | late | rolled`, and optionally
   `forwarder_informed_at`). Likely a field added to the watchlist/state record, or a small
   `outcomes.json` keyed by `shipment_id`.
2. **Reconciliation report** — a read-only command (sibling of `report.py`) that joins the
   engine's per-shipment verdict + alert history against the recorded outcome and prints:
   - the confusion matrix (overall and **segmented by carrier / T-S port**),
   - the **false-confidence rate** (the silent-roll-inclusive number),
   - average **lead time** of true catches vs. the forwarder,
   - false-alarm rate (for buffer tuning).
3. **Feed it back** — use the measured rates to (a) tune `MCT_DAYS` / tolerance buffers per
   carrier-hub, and (b) decide how much to discount feasible-connection greens.

## Prereqs / notes

- Needs **resolved shipments** (arrived/delivered) with a recorded real outcome — so this is
  a post-pilot activity, after a batch of shipments has run end to end.
- Best ground truth would be **carrier container-event data** (EDI/API milestones incl.
  explicit "rolled") — that measures rolls *directly* instead of by consequence. If we ever
  get it, it dominates this consequence-based method and also fixes the blind spot itself.
- Until then, forwarder reports + actual delivery dates are the practical ground truth.

See also: `scheduleTracker.md` §2b (connection-integrity model), §4 (limitations), `GUID.md`
§9 (limits).
