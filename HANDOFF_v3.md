# HANDOFF — v1 → v3 refactor

Continuity note for picking the work back up after a restart. Companion to
[synth.md](synth.md) (the detailed v3 method) and [CLAUDE.md](CLAUDE.md) (the
repo/migration conventions). This file is the **why** and the **status**.

---

## 1. The philosophy: why v3 exists

Every carrier scraper answers one question: *for a given origin, what schedules
does this carrier offer, and how long do they take?* Destinations come in two
flavors — **ports** (ocean discharge) and **inland yards** (reached by ocean +
rail). The refactor is entirely about how we get **inland** transit right without
over-generating invalid routes.

### v1 — direct, per-quote
Scrape exactly the (POL, destination) pairs in `quotes.csv`. A destination could
be a port or an inland yard; the carrier returned whatever it returned. Correct,
but only covers the handful of quotes we happened to have. No systematic coverage
of a carrier's network.

### v2 — synthesize inland from ports (abandoned)
Idea: scrape **ports** only, then *synthesize* inland schedules by adding an
estimated rail leg (discover a "discriminator" that predicts which port serves
which inland yard, add average rail time). Two fatal flaws surfaced in testing:

1. **Rail-time estimation error.** We were guessing transit; the carrier already
   knows it exactly.
2. **Origin-validity over-generation.** Which port serves a given inland yard is
   *origin-specific*. Synthesizing "port P → yard Y" for every origin invented
   routes that a given origin never actually sails. (Confirmed empirically on HMM.)

### v3 — scrape inland directly, derive ports by truncation (current)
Flip it around. **Scrape the inland yards directly**, origin by origin. The
carrier's own response already contains the ocean leg *and* the exact rail leg to
that yard. Then:

- **Port-to-port is derived, not scraped** — truncate an inland schedule at its
  discharge port (`eta = pod_eta`, drop the rail leg, dedup by ocean sailing). No
  estimation. The ocean leg is a fact already in the payload.
- **Origin-specificity is preserved for free** — we only ever record routes the
  carrier actually returned for that origin. No synthesis, no over-generation.
- **Pure-ocean ports** (served port-to-port but railing nothing inland, so they
  never appear as a discharge POD in the inland scrape) are caught by a
  **secondary scrape**: diff the carrier's port universe against the observed
  discharge PODs, and directly scrape the missing ports.

Net: exact transit times + exact origin validity + full network coverage, with
less machinery than v2.

---

## 2. The v3 pipeline (one run, one session, push last)

```
1. inland scrape    origins × inland yards          -> exact inland canonicals
2. derive ocean     truncate each at discharge port -> port-to-port canonicals
3. missing ports    port universe − observed PODs   -> per-origin pure-ocean gaps
4. secondary scrape origins × missing ports         -> the pure-ocean ports
5. push everything to Supabase — at the very end, on a clean finish only
```

Hard rules (apply to every carrier):

- **One session for the whole run.** Bootstrap once; re-bootstrap only on
  auth/bot failures (403/401), not per phase.
- **Push is deferred to the very end.** A crash saves partial progress and pushes
  **nothing** — pushes run only after all scraping succeeds. Each set (inland /
  ocean / secondary) gets its own ledger so re-runs are idempotent.
- **`coverage.json` is READ-ONLY.** It's the carrier's port universe (+ inland
  yards), discovered in a separate project. v3 only *reads* `type == "port"` /
  `type == "inland"`. Do not modify its stats.
- **Temp output is isolated** under `src/carriers/<code>/assets/temp_v3/`
  (`raw/`, `canonicals/`, `ocean/`, `raw_secondary/`, `secondary/`, ledgers,
  logs). v1's `src/data/<code>/…` is left completely alone, so v1 and v3 coexist.

### `coverage.json` shape
```json
{ "carrier": "HPL",
  "description": "...",
  "coverage": { "Chicago, IL": {"type": "inland"}, "Los Angeles, CA": {"type": "port"}, ... } }
```
Keys come from `<code>_yards.geojson` (`CityYard` property), classified
port (coastal) vs inland.

### The conflation guard (secondary pass only)
When we secondary-scrape a missing port X, some carriers resolve the query to a
*different* port Y's voyages. Guard: **drop a schedule if `eta == pod_eta` AND
`port_of_discharge != last_cy`** — i.e. a direct call discharging somewhere other
than what we asked for (already covered elsewhere). Keeps pure-ocean (discharge ==
queried) and rail-served (eta != pod_eta) schedules. Not applied to the inland
pass. MSC has no secondary pass, so no guard.

---

## 3. Per-carrier status

**Done (v3 built): COS, EMC, HMM, HPL, MSC, MSK, ONE, OOCL, ZIM.**
**Remaining: CMA, WHL, YML.**

Each done carrier has `mainv3.py` (+ `derive_ocean.py`, except MSC) and
`assets/coverage.json`. Files: `src/carriers/<code>/`.

| Carrier | Session / transport | v3 notes |
|---|---|---|
| **COS** | undetected_chromedriver — bootstraps cookies then **quits Chrome**, reuses creds, re-bootstraps on 403 | Reference v3 build. Full inland → derive → secondary. |
| **EMC** | pure `requests` (ShipmentLink HTML + BeautifulSoup) | Long Beach returns identical voyages to LA → aliased to `Los Angeles, CA` in `utils._PORT_LOOKUP` (removed from coverage). Seattle/Tacoma stay distinct. |
| **HMM** | patchright/Playwright, browser kept alive; 2-step JSON API (INIT→GrmNo→RESULT). **`patch` conda env.** | Seattle was a phantom (query Seattle → Tacoma voyages, 0 real Seattle PODs) → removed from coverage; Tacoma kept. LA/Long Beach conflation handled by the guard. |
| **HPL** | clean JSON **GET** API (`schedule.api.hlag.cloud/api/routes`, `x-token: public`) — no browser, no session | Simplest of the rail carriers. Uses existing `build_canonical_record` (no utilsv2). **Long Beach is derive-only**: not in coverage (never queried), kept distinct via `normalize_pod`. ⚠️ **Open item:** scraped data shows Long Beach rails **0** inland yards (all West-Coast inland goes via LA/Oakland/Tacoma), so as built Long Beach gets **zero** schedules. Fix pending: add `Long Beach, CA` as a `port` in coverage so the secondary pass scrapes it directly (guard then keeps only `port_of_discharge == Long Beach`). |
| **MSC** | pure `requests.Session` + cookie bootstrap (re-prime on 401/403) | **Special: port-to-port ONLY** — no inland, no derive, no secondary, no guard. v3 just loops origins × port coverage. LA/Long Beach & Miami/Port Everglades are separate PortIds/calls in MSC; v1's `msc_cities.json` nests each pair under one key, so v3 uses **`msc_citiesv3.json`** (those 2 keys exploded into 4 single-port keys) + an 18-port coverage. v1 untouched. |
| **MSK** | pure `requests` POST (`api.maersk.com/routing-unified`, GEO_ID payload, `consumer-key` header) — no browser/session bootstrap | Full inland → derive → secondary. Reuses v1 `build_canonical_record` (no utilsv2). v1's `msk_cities.json` nested the two Kansas Cities (KS + MO) under one list-valued `"Kansas City, KS"` key, so v3 uses **`msk_citiesv3.json`** (that key exploded into `Kansas City, KS` + `Kansas City, MO`) + a 39-key coverage (14 port / 25 inland from `msk_yards.geojson`). Nhava Sheva (origin) and Fort Worth (yard) have no Maersk code → auto-skipped. **Location enrichment (mirrors v1):** MSK returns some POD/TS ports as GEO_ID codes; `mainv3.enrich_location_codes` resolves each code → city name via `api.maersk.com/synergy/reference-data/geography/locations/{code}`, cached in `city_codes.json` (shared with v1), and rewrites the codes inside the raw responses **before** canonical building. Runs after both the inland and secondary scrapes. All coverage ports' bare city is in `utils.PORT_NAMES` so `normalize_pod` yields `City, ST` == coverage key (Newark → `Newark, NJ` and Wilmington → `Wilmington, NC` were added to `PORT_NAMES` for this; they also improve v1's own normalization). **⚠️ verify after first run:** yard set has only Los Angeles & Tacoma, so Long Beach/Seattle are derive-only distinct PODs (never queried), like HPL Long Beach. |
| **ONE** | pure `requests` GET (`ecomm.one-line.com/api/v1/schedule/point-to-point`, `porCode`/`delCode` params + `sessLocale`/`usrCntCd`/`AKA_A2` cookies) — no browser/session bootstrap (v1's unused `undetected_chromedriver` import dropped in v3) | Full inland → derive → secondary. Reuses v1 `build_canonical_record` (no utilsv2). `one_cities.json` is flat (`{name:{code}}`), so no cities-explode needed and **no location-code enrichment** — ONE returns port names (`podName`) directly. 36-key coverage (13 port / 23 inland from `one_yards.geojson`); all 7 origins + all 36 yards resolve. Yard set uses `New York, NY` (not Newark) and all 13 ports are already in `utils.PORT_NAMES`, so no normalizer additions. Yard set has only Los Angeles & Tacoma → Long Beach/Seattle are derive-only distinct PODs, like HPL Long Beach. |
| **OOCL** | patchright/Playwright browser kept alive across both passes (navigate sailing-schedules page, intercept `searchHubToHubRoute` POST). **`patch` conda env.** `headless=False`. | Full inland → derive → secondary. Reuses v1 `build_canonical_record` (no utilsv2). `mapping.json` is flat (`{name:[one locationid]}`) — **nothing to explode**, and **no location-code enrichment** (OOCL returns facility names). 36-key coverage (12 port / 24 inland from `oocl_yards.geojson`); all 7 origins + 36 yards resolve. Same **sweep-based transient retry as ONE** (timeout / navigation / 5xx / 429 → requeue with escalating cooldown, zero-progress abort) since browser navigation is flaky. Structural reference = HMM (browser session pattern). NOTES: (a) **Portland, OR classified inland**, not port — it's the one coastal-ish yard not in `utils.PORT_NAMES`, so as a port it would misalign the diff/guard (like MSK Newark); inland is safe (scraped directly, still derives its ocean leg). (b) yard set has Los Angeles (no Long Beach) & Seattle (no Tacoma) → Long Beach/Tacoma are derive-only distinct PODs, like HPL Long Beach. |
| **ZIM** | `requests` GET (`apigw.zim.com/digitalSchedules/PointToPoint/v2`, `subscription-key`) + **Akamai cookie bootstrap** (COS pattern): apigw.zim.com is fronted by Akamai Bot Manager which TLS-fingerprints clients — cookie-less `requests`/curl get 403 "Access Denied" (errors.edgesuite.net) no matter the headers. `get_new_session()` launches undetected_chromedriver (**visible window — headless is denied and harvests 0 cookies**), visits zim.com so Akamai issues `ak_bmsc`/`bm_sv`/`bm_sz`/`_abck`, grabs cookies + UA, quits Chrome; `requests` then passes. Re-bootstrap on 403/401, max once per sweep; a 403 that survives fresh creds goes back to pending. | Full inland → derive → secondary. **v2 migration (Jul 2026)**: v1 endpoint now 403s; v2 takes the same params + `CargoType=true`/`EmissionsType=true`, and the response **still contains the v1-shaped `routes`** (plus midPoints/emissions we ignore) — verified by probe + smoke test, so v1 `build_canonical_record` works unchanged. Port codes are sent with their **stored `;N` suffix** (`;10` marine / `;0` land, like the live UI; v1 forced `;10`). `docClosingDate` is null in v2 → `cutoff_date` None. `zim_cities.json` is flat (715 keys) — **nothing to explode**; **no location-code enrichment** (ports come back as names). Each entry's `shortPortName` is the effective port name (mirrors v1). 28-key coverage (11 port / 17 inland from `zim_yards.geojson`). Same **sweep-based transient retry as ONE**. NOTES: (a) **Newark, NJ classified inland**, not port — its `zim_cities` entry is actually the NY/NJ port `USNYC` / `shortPortName "New York, NY"` (≠ the yard key), so as a port it would misalign the diff/guard; inland it's scraped directly and its (direct) New York, NY ocean leg still derives. (b) all 11 port yards have `shortPortName == key` and are in `utils.PORT_NAMES`. (c) yard set has Los Angeles (no Long Beach) and no PNW port → Long Beach/Seattle/Tacoma are derive-only distinct PODs. (d) origins **Semarang and Karachi are not in `zim_cities`** (ZIM doesn't cover them) → auto-skipped, so ZIM effectively runs 5 origins. |

### Shared inputs
- `data/origins.csv` — column `port`, the 7 origins (Nhava Sheva, Semarang,
  Karachi, Qingdao, Manila, Ho Chi Minh, Shanghai). v3 loops these instead of
  reading `quotes.csv`.
- Supabase creds in `.env` (`SUPABASE_URL` / `SUPABASE_KEY`); push via
  `src/ingest/ingest.py::ingest_new_canonicals(code, canonical_dir, ledger_path)`
  — idempotent upsert on `schedule_hash`.
- conda envs: `patch` = HMM, OOCL; `schedules` = everything else.

---

## 4. Recipe for the remaining carriers (CMA, MSK, ONE, OOCL, WHL, YML, ZIM)

1. **Read the carrier's `main.py`** — determine session model (requests / uc /
   playwright), the API/HTML shape, and confirm `build_canonical_record` (or a
   `utilsv2` variant) already emits `port_of_discharge`, `etd`, `eta`, `pod_eta`,
   and the vessel fields the derive copies.
2. **Build `coverage.json`** from `<code>_yards.geojson` keys, classify port vs
   inland.
3. **Build `derive_ocean.py`** — copy an existing one (COS/HPL are clean
   references), swap the carrier code/name and the `derive_ocean_schedule` field
   list to match that carrier's canonical schedule shape.
4. **Build `mainv3.py`** — copy the closest session-model sibling
   (requests→EMC/HPL, uc→COS, playwright→HMM), wire the 5-phase orchestration.
   Reuse `scrape_matrix` / `build_canonicals` / `build_quotes` / `_drop_conflated`.
5. **Handle per-carrier LA/Long Beach + phantom ports** — check whether the
   carrier (a) returns identical voyages for co-located ports (alias, like EMC),
   (b) has a phantom that resolves elsewhere (remove, like HMM Seattle), (c) keeps
   them genuinely distinct (separate calls, like MSC), or (d) rails one but not the
   other (derive-only vs secondary, like HPL). Verify against scraped data, don't
   assume.
6. **Compile-check + confirm resolution** (origins + coverage resolve to codes),
   then hand to the user to run in the correct conda env.

---

## 5. Immediate next step

Two loose threads:

1. **HPL's Long Beach** (see the HPL row): decide whether to add `Long Beach, CA`
   as a `port` in HPL coverage so the secondary pass captures its direct sailings,
   then re-run HPL's secondary.
2. **MSK first run** — after running `mainv3.py` in the `schedules` env, check the
   log's derived `port_of_discharge` names against the two ⚠️ items in the MSK row
   (Long Beach/Seattle derive-only; Newark vs New York naming).

After that, proceed to the remaining carriers (CMA, WHL, YML)
using the recipe in §4. (CMA and WHL are HTML carriers.)
