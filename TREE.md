# Ocean Routing — Proposed Project Layout

This document captures (1) what the current Schedules folder actually contains, (2) what each carrier's canonical entry point is, and (3) a recommended target tree for the new repo at `ocean-routing/`. Phase 2 will migrate one carrier at a time using this map.

Source location being migrated from:
`C:\Users\LuisMiguelHernandezT\OneDrive - Prime Time Packaging\Schedules`

---

## 1. Canonical script per carrier (from each `.bat`)

| Carrier | Folder    | Canonical script | Utility modules imported               | Conda env   |
|---------|-----------|------------------|----------------------------------------|-------------|
| CMA     | `cma/`    | `main2.py`       | `utilscma.py`, `utilscmacanonical.py`  | `schedules` |
| COSCO   | `cos/`    | `modular4.py`    | `utils.py`                             | `schedules` |
| EMC     | `emc/`    | `main.py`        | `utilsemc.py`, `utilsemccanonical.py`  | `schedules` |
| HPL     | `hpl/`    | `main.py`        | `utilshapag.py`, `utilshpl.py`         | `schedules` |
| HMM     | `hmm/`    | `main.py`        | `utilshmm.py`                          | `patch`     |
| MSC     | `msc/`    | `main.py`        | `utilsmsc.py`                          | `schedules` |
| MSK     | `msk/`    | `main.py`        | `utilsmsk.py`                          | `schedules` |
| ONE     | `one/`    | `main.py`        | `utilsone.py`                          | `schedules` |
| OOCL    | `oocl/`   | `main.py`        | `utilsoocl.py`                         | `patch`     |
| WHL     | `whl/`    | `main2.py`       | `utilswhl.py`                          | `schedules` |
| YML     | `yml/`    | `main.py`        | `utilsym.py`                           | `schedules` |
| ZIM     | `zim/`    | `main4.py`       | `utilszim.py`                          | `schedules` |

Every canonical script computes `PROJECT_ROOT = Path(__file__).resolve().parents[3]` and resolves I/O from there. So as long as each carrier's `main.py` sits at `<root>/src/carriers/<carrier>/main.py`, the runtime paths keep working.

Shared inputs every canonical script reads:
- `<root>/data/quotes.csv`
- `<root>/data/locations.csv`

Shared output:
- `<root>/src/data/tables/<CARRIER>_<DATE>.csv`

Per-carrier I/O:
- `<root>/src/data/<carrier>/{raw,log,csvs,canonical}/`
- `<root>/src/carriers/<carrier>/assets/<carrier>_cities.json`, `<carrier>_yards.geojson`, `<carrier>_wait_phases.json` (carrier-specific lookup assets)

---

## 2. Recommended target tree

```
ocean-routing/
├── README.md
├── requirements.txt
├── .gitignore
├── config.py                      # PROJECT_ROOT, RAW_DIR, progress_filename
│
├── data/                          # SHARED inputs (read by every carrier)
│   ├── quotes.csv
│   ├── locations.csv
│   └── combinations.csv           # if still used
│
├── src/
│   ├── carriers/                  # 1 folder per carrier, self-contained
│   │   ├── cma/
│   │   │   ├── main.py            # renamed from main2.py
│   │   │   ├── utils.py           # merged utilscma.py + utilscmacanonical.py
│   │   │   ├── run.bat            # renamed from cma.bat.bat
│   │   │   └── assets/
│   │   │       ├── cma_cities.json
│   │   │       ├── cma_cities_filtered.json
│   │   │       └── cma_yards.geojson
│   │   │
│   │   ├── cos/
│   │   │   ├── main.py            # renamed from modular4.py
│   │   │   ├── utils.py           # was utils.py (no rename)
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── allcoscocities.json    # only if modular4.py opens it
│   │   │       ├── coscotsports.json
│   │   │       └── cos_yards.geojson
│   │   │
│   │   ├── emc/
│   │   │   ├── main.py
│   │   │   ├── utils.py           # merged utilsemc.py + utilsemccanonical.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── emc_cities.json
│   │   │       └── emc_yards.geojson
│   │   │
│   │   ├── hpl/
│   │   │   ├── main.py
│   │   │   ├── utils.py           # merged utilshapag.py + utilshpl.py
│   │   │   ├── run.bat            # renamed from hpl.bat.bat
│   │   │   └── assets/
│   │   │       ├── hpl_cities.json
│   │   │       ├── hpl_wait_phases.json
│   │   │       └── hpl_yards.geojson
│   │   │
│   │   ├── hmm/
│   │   │   ├── main.py
│   │   │   ├── utils.py           # was utilshmm.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── hmm_cities.json
│   │   │       ├── hmm_wait_phases.json
│   │   │       └── hmm_yards.geojson
│   │   │
│   │   ├── msc/
│   │   │   ├── main.py
│   │   │   ├── utils.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── msc_cities.json
│   │   │       ├── msc_wait_phases.json
│   │   │       └── msc_yards.geojson
│   │   │
│   │   ├── msk/
│   │   │   ├── main.py
│   │   │   ├── utils.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── city_codes.json
│   │   │       ├── msk_cities.json
│   │   │       ├── msk_wait_phases.json
│   │   │       └── msk_yards.geojson
│   │   │
│   │   ├── one/
│   │   │   ├── main.py
│   │   │   ├── utils.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── onecities.json
│   │   │       ├── one_wait_phases.json
│   │   │       └── one_yards.geojson
│   │   │
│   │   ├── oocl/
│   │   │   ├── main.py
│   │   │   ├── utils.py           # was utilsoocl.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── mapping.json
│   │   │       └── oocl_yards.geojson
│   │   │
│   │   ├── whl/
│   │   │   ├── main.py            # renamed from main2.py
│   │   │   ├── utils.py
│   │   │   ├── run.bat
│   │   │   └── assets/
│   │   │       ├── connections.json
│   │   │       ├── wanhai_locations.json
│   │   │       └── whl_yards.geojson
│   │   │
│   │   ├── yml/
│   │   │   ├── main.py
│   │   │   ├── utils.py           # was utilsym.py
│   │   │   ├── run.bat            # renamed from yml.bat
│   │   │   └── assets/
│   │   │       ├── ym_cities.json         # keep the active variant
│   │   │       └── yml_yards.geojson
│   │   │
│   │   └── zim/
│   │       ├── main.py            # renamed from main4.py
│   │       ├── utils.py
│   │       ├── run.bat
│   │       └── assets/
│   │           ├── zim_locs.json          # keep the active variant
│   │           ├── zim_wait_phases.json
│   │           └── zim_yards.geojson
│   │
│   └── data/                      # carrier OUTPUTS (one subfolder per carrier)
│       ├── tables/                # final shared output that everyone writes to
│       ├── cma/{raw,log,csvs,canonical}/
│       ├── cos/{raw,log,csvs,canonical}/
│       ├── emc/{raw,log,csvs,canonical}/
│       ├── hpl/{raw,log,csvs,canonical}/
│       ├── hmm/{raw,log,csvs,canonical}/
│       ├── msc/{raw,log,csvs,canonical}/
│       ├── msk/{raw,log,csvs,canonical}/
│       ├── one/{raw,log,csvs,canonical}/
│       ├── oocl/{raw,log,csvs,canonical}/
│       ├── whl/{raw,log,csvs,canonical}/
│       ├── yml/{raw,log,csvs,canonical}/
│       └── zim/{raw,log,csvs,canonical}/
│
├── database/
│   └── db.xlsx                    # master DB
│
└── icons/                         # carrier logos (kept as-is)
```

`PROJECT_ROOT = parents[3]` still resolves correctly because the depth of `src/carriers/<carrier>/main.py` is unchanged.

---

## 3. What got dropped (ancillary / dev leftovers)

The following are removed in the new layout. Nothing in any canonical `.bat` references them.

### Non-canonical Python scripts (older iterations)
- `cma/main.py`, `cma/main2 - Copy.py`, `cma/notjson.py`, `cma/test.py`
- `cosco/main.py`, `cosco/modular.py`, `cosco/modular2.py`, `cosco/modular3.py`, `cosco/modular5.py`, `cosco/prototype.py`
- `hmm/coverage_runner.py`, `hmm/hmm_datastructure.py`, `hmm/hp.py`, `hmm/hp_datastructure.py`, `hmm/mainhmm.py`, `hmm/parse_hmm.py`
- `oocl/ooclLoopflow.py`, `oocl/ooclfunction.py`
- `whl/main.py`, `whl/flow3.py`, `whl/wanhaiDataStructuring.py`, `whl/wanhai_html_to_json.py`
- `zim/main.py`, `zim/main2.py`, `zim/main3.py`, `zim/structure.py`, `zim/test.py`

### Notebooks (development scratchpads)
- `cma/parser.ipynb`, `cma/test.ipynb`
- `cosco/cosco_scraper.ipynb`
- `emc/test.ipynb`
- `one/mainone.ipynb`
- `yang/formatjson.ipynb`
- `zim/mod.ipynb`
- `data/cma/batcher.ipynb`, `data/cma/semarang/main.ipynb`
- `data/df.ipynb`, `Vessels/one/vessels.ipynb`

### Notes / philosophy markdown files
- Every carrier's `DATE.md`, `NORM.md`
- `cma/canonical.md` *(but check first — sometimes contains the column spec)*
- `cosco/structurePhilosophy.md`, `hapag/savingphilosophy.md`, `hapag/waittime.md`, `msk/lastcyPhilosophy.md`
- `hmm/CLAUDE.md`, `hmm/HMM_script_notes.txt`
- `one/endpoint.txt`, `yang/read.me.txt`

> If any of these contain decisions worth keeping, fold them into one `docs/decisions.md` at the root instead of scattering per-carrier.

### Caches / scratch data
- All `__pycache__/` folders
- `src/carriers/cma/.venv/` — accidentally committed virtualenv, do NOT migrate
- `schedulesenv/` — top-level virtualenv, do NOT migrate
- `cma/cma_cities.json` vs `cma_cities_filtered.json` — keep whichever `main2.py` actually opens
- `zim/zim_locs.json` vs `zim_locs_cleaned.json` — same
- `yang/ym_cities.json` vs `ym_cities_fixed.json` — same
- `data/quotes.csv`, `quotes2.csv`, `quotes3.csv` — keep only the one referenced by scripts
- `data/combinations.xlsx` (keep `.csv` if it's the active one)
- `data/cma/semarang/` — looks like one-off Semarang debugging
- `src/data/tables/processed/`, `src/data/tables/stats/`, `src/data/tables/stats/db/` — historical output, archive separately if you want, don't keep in repo

### Stale/unused folders
- `msk/processing/` (empty)
- `src/utils/geocode_utils.py`, `src/utils/spatial_utils.py` — only referenced by `cosco/main.py`, which is NOT the canonical cosco script. Confirm and delete.

---

## 4. On the utilities question: merge or keep separate?

**Short answer: keep one `utils.py` per carrier (don't merge into one global file), but later extract the truly shared helpers into `src/common/`.**

### What I saw

Every carrier's utility file (220–1100 lines each — 14 files, ~7,100 lines total) is structured the same way:

1. **Top section (~lines 14–220) is near-identical across carriers** — `get_month_periods`, `assign_snapshot`, `load_progress`, `get_unique_path`, `get_unique_filename`, `geocode_city`, `resolve_missing_locations`, `build_voronoi_lookup`, `get_locations`. These are project-wide helpers, not carrier-specific.

2. **Bottom section is carrier-specific parsing** — leg parsing, vessel-name handling, canonical record assembly. Heavily tied to each API's response shape (MSC vs ZIM vs OOCL vary a lot).

### Why not one giant `utils.py`

A merged file would be ~7,000 lines, with most functions namespaced by prefix (`_msc_legs`, `_zim_legs`, `_oocl_legs`, …). That's a monolith — slow to navigate, painful to diff, and a single syntax error breaks every carrier at once. It also makes the "migrate carrier by carrier" plan harder because nothing is truly isolated.

### Why not 12 unrelated files (status quo)

That same shared-helpers block has been copy-pasted 12 times. Fixing a bug in `assign_snapshot` means editing 12 files. Already, a couple of carriers (CMA, EMC, HPL) split their utilities into two files because the single file was getting unwieldy.

### Recommended middle path

```
src/
├── common/                        # one source of truth for shared logic
│   ├── __init__.py
│   ├── io.py                      # load_progress, get_unique_filename,
│   │                              #   get_unique_path, safe_to_csv
│   ├── geo.py                     # geocode_city, resolve_missing_locations,
│   │                              #   build_voronoi_lookup, get_locations
│   └── snapshot.py                # assign_snapshot, get_month_periods,
│                                  #   run timestamp helpers
│
└── carriers/<carrier>/utils.py    # ONLY the carrier-specific parsing,
                                   # imports shared bits from src.common
```

Each carrier's `utils.py` shrinks from 400–1100 lines down to roughly the carrier-specific portion (~200–700 lines). Each `main.py` changes from `from utilshapag import …` to two imports: one from `common`, one from the local `utils`.

### Suggested phasing

- **Phase 2 (now)**: just consolidate per-carrier into a single `utils.py` (merging the carriers that have two utility files today: CMA, EMC, HPL). Don't extract to `common/` yet. This keeps the move mechanical and lets you verify each carrier runs end-to-end before any refactor.
- **Phase 3 (later, optional)**: extract the shared block into `src/common/`. Do it once, all carriers at once, after Phase 2 has been validated.

That ordering also matches your stated plan of moving carrier-by-carrier — each carrier in Phase 2 is a self-contained, low-risk move.

---

## 5. Other things worth flagging while you're cleaning up

- `requirements.txt.txt` and `README.md.txt` have double extensions — rename to `requirements.txt` and `README.md`. Both look empty currently; populate `requirements.txt` from the active conda envs (`schedules` and `patch`) so the project is reproducible.
- Two different conda envs are in use (`schedules` for most, `patch` for HMM + OOCL). Worth documenting in README which carrier needs which env — or unifying.
- `database/db.xlsx` lives outside any clear ownership — clarify whether it's input (downstream consumer reads it) or output (a carrier writes to it).
- After migration, this project should be turned into a real `git init` repo with a `.gitignore` that excludes `__pycache__/`, `*.venv/`, `schedulesenv/`, `src/data/<carrier>/{raw,log}/`, and probably `src/data/tables/` (generated data shouldn't live in version control).
