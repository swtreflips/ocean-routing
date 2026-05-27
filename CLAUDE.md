# CLAUDE.md

Conventions established when migrating from the legacy Schedules folder (OneDrive) to this repo. Read this before migrating any of the remaining carriers (COS, EMC, HPL, HMM, MSC, MSK, ONE, OOCL, WHL, YML, ZIM).

The plan lives in [TREE.md](TREE.md). CMA is the only carrier migrated so far and is the reference implementation.

## Carrier-code naming

Always use the carrier code (matches output table prefixes and vessel CSVs):

| Code | Source name (legacy) |
|------|----------------------|
| cma  | cma   |
| cos  | cosco |
| emc  | emc   |
| hpl  | hapag |
| hmm  | hmm   |
| msc  | msc   |
| msk  | maersk (in `src/data/`) / msk (in `src/carriers/`) |
| one  | one   |
| oocl | oocl  |
| whl  | whl (wanhai) |
| yml  | yang  |
| zim  | zim   |

Apply consistently to: carrier folder under `src/carriers/`, output folder under `src/data/`, asset filenames (`{code}_yards.geojson`, `{code}_cities.json`, `{code}_wait_phases.json`).

A folder named `yml/` does not conflict with `.yml` YAML file extensions — Python doesn't care about folder names unless there's an `__init__.py`.

## Per-carrier folder shape

```
src/carriers/<code>/
├── main.py           # the canonical script (renamed from whatever the .bat ran)
├── utils.py          # all carrier-specific helpers, merged into one file
├── run.bat           # renamed from <code>.bat / <code>.bat.bat
└── assets/
    ├── <code>_cities.json         (or carrier equivalent)
    ├── <code>_wait_phases.json    (if the carrier has one)
    └── <code>_yards.geojson       (renamed from *_yards_clipped.geojson)
```

Output directories sit under `src/data/<code>/`:
- `raw/` — archived API responses (JSON)
- `log/` — run logs + progress CSV
- `csvs/` — per-run CSV exports
- `canonical/` — canonical JSON records
- `html/` — **CMA only**, see below
- `tables/` (at `src/data/tables/`) — aggregated cross-carrier table

All `src/data/*/{raw,log,csvs,canonical,html}/` are gitignored. Assets and code are committed.

## Per-carrier migration recipe

1. **Identify canonical script.** Open the carrier's `.bat` in the legacy folder — the `python <script>.py` line names it. Renaming targets:
   - `cma`: was `main2.py`
   - `cos`: was `modular4.py`
   - `whl`: was `main2.py`
   - `zim`: was `main4.py`
   - others: already `main.py`

2. **Identify utility files.** Look at the `from utils<x> import ...` lines at the top of the canonical script. Some carriers split into two files (e.g., CMA had `utilscma.py` + `utilscmacanonical.py`). Merge them into one `utils.py`.

3. **Identify assets.** Anything the script loads from `CARRIER_DIR / "*.json"` or `CARRIER_DIR / "*.geojson"`. Watch for **module-level file opens inside the utility file** (e.g., `with open(CARRIER_DIR / "cities.json") at the top — runs at import).

4. **Copy + transform.** Standard patches to main.py:
   - `from utils<x> import (...)` → `from utils import (...)`
   - If a second util file existed (e.g., `from utilscmacanonical import build_canonical_record`), add its symbols to the same `from utils import (...)` block and delete the second import line.
   - `CARRIER_DIR / "<asset>.json"` → `CARRIER_DIR / "assets" / "<asset>.json"`
   - `CARRIER_DIR / "<carrier>_yards_clipped.geojson"` → `CARRIER_DIR / "assets" / "<code>_yards.geojson"`
   - Module-level opens in utils.py get the same `assets/` patch.

5. **Build utils.py.** Concatenate the carrier's utility files. Strip duplicate top-level imports if you want, but it's safe to leave them. **Do not drop "unused" functions** — see [#unused-functions](#gotcha-unused-functions).

6. **Create run.bat.** Copy the legacy `.bat`, change `python <oldscript>.py` to `python main.py`. Conda env stays the same:
   - `schedules` env: cma, cos, emc, hpl, msc, msk, one, whl, yml, zim
   - `patch` env: hmm, oocl

7. **Rename yards.** `*_yards_clipped.geojson` → `<code>_yards.geojson`.

8. **Test.** Run `src\carriers\<code>\run.bat`. Logs land in `src\data\<code>\log\`. Errors mention the script name + line — easy to trace.

## Standard path conventions in main.py

All canonical scripts compute paths the same way — preserve this when migrating:

```python
PROJECT_ROOT = Path(__file__).resolve().parents[3]    # ocean-routing/
DATA_DIR     = PROJECT_ROOT / "data"                  # shared inputs
LOG_DIR      = PROJECT_ROOT / "src" / "data" / "<code>" / "log"
RAW_DIR      = PROJECT_ROOT / "src" / "data" / "<code>" / "raw"
CSV_DIR      = PROJECT_ROOT / "src" / "data" / "<code>" / "csvs"
CANONICAL_DIR= PROJECT_ROOT / "src" / "data" / "<code>" / "canonical"
TABLES_DIR   = PROJECT_ROOT / "src" / "data" / "tables"
CARRIER_DIR  = Path(__file__).resolve().parent
```

`parents[3]` works because `main.py` is at `<root>/src/carriers/<code>/main.py` — same depth as before.

## Gotchas

### Unused-function trap

When deciding what to drop from the merged `utils.py`, check **internal callers**, not just `main.py`'s import list. Example: `get_month_periods` was dropped because `main.py` didn't import it — but `assign_snapshot` (which IS imported) calls it internally. `NameError` at runtime.

Recipe: before deleting a function, grep the entire utility file for its name. If anything other than its `def` line matches, keep it.

### GDAL_DATA prelude

`utils.py` imports `geopandas`. On Anaconda Windows installs, geopandas/GDAL fires `Warning 3: Cannot find header.dxf (GDAL_DATA is not defined)` unless `GDAL_DATA` is set before geopandas is loaded. Every carrier's `main.py` must open with this 3-line prelude **before** the `from utils import …` line:

```python
import os
import sys
os.environ['GDAL_DATA'] = os.path.join(f'{os.sep}'.join(sys.executable.split(os.sep)[:-1]), 'Library', 'share', 'gdal')
```

If the legacy carrier script already has it (CMA does), copy it through. If it doesn't (COSCO didn't), add it during the migration.

### Module-level side effects in utils

CMA's old `utilscma.py` had `with open(CARRIER_DIR / "cma_cities.json") ...` at module top level. That runs at `import` time. If you move the JSON to `assets/`, you must patch the open inside utils.py too — not just the explicit references in main.py.

### Two conda envs

HMM and OOCL run in the `patch` conda env. Everything else runs in `schedules`. Keep `run.bat` consistent with the legacy `.bat`.

### `OneDrive` source as backup

The legacy folder at `C:\Users\LuisMiguelHernandezT\OneDrive - Prime Time Packaging\Schedules\` is the migration source. We **copy**, not move — the OneDrive copy stays intact during migration so a partial/broken state in this repo is recoverable. Delete from OneDrive only after each carrier has been verified end-to-end in the new repo.

## CMA-specific deviations

CMA is the only carrier where the API returns HTML (everyone else returns JSON). Three CMA-only behaviors:

1. **HTML archive.** Raw HTMLs are moved to `src/data/cma/html/` instead of deleted, so a parser bug can be debugged by re-parsing the saved HTML without re-hitting CMA's bot-detected endpoint. Implemented via an optional `html_archive_dir` parameter on `transform_html_to_json` / `batch_transform_processing_dir`. **Do not replicate this for non-HTML carriers** — they have nothing to archive.

2. **pod_eta -4 days for inland destinations.** CMA's HTML stores only one date per location node (the rail-ready date when there's an inland leg). For inland routes we subtract 4 days from the ocean leg's eta to approximate the actual port arrival, matching CMA's own port-only timeline. Logic at [utils.py:812-822](src/carriers/cma/utils.py#L812-L822). Trigger: `ocean[-1] is not legs[-1]`. **Don't port this to other carriers** — they have explicit eta fields.

3. **Dynamic SearchDate.** The CMA payload's `SearchDate` field is set from `fromDate = today.strftime("%d-%b-%Y")` ([main.py:220-221](src/carriers/cma/main.py#L220-L221)). `%b` is locale-dependent — fine on English Windows, would break on a non-English machine.

## Conda envs (reference)

- `schedules`: 10 of 12 carriers
- `patch`: HMM, OOCL

Both are anaconda3 envs under `C:\Users\LuisMiguelHernandezT\anaconda3\envs\`. Worth folding into a single env eventually; not urgent.
