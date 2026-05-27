# Ocean Routing

Ocean carrier schedule scrapers. One folder per carrier under `src/carriers/`. See [TREE.md](TREE.md) for the full layout and migration plan.

## Carriers

| Carrier | Folder | Conda env |
|---------|--------|-----------|
| CMA     | `src/carriers/cma/` | `schedules` |
| COSCO   | `src/carriers/cos/` | `schedules` |
| EMC     | `src/carriers/emc/` | `schedules` |
| HPL     | `src/carriers/hpl/` | `schedules` |
| HMM     | `src/carriers/hmm/` | `patch`     |
| MSC     | `src/carriers/msc/` | `schedules` |
| MSK     | `src/carriers/msk/` | `schedules` |
| ONE     | `src/carriers/one/` | `schedules` |
| OOCL    | `src/carriers/oocl/`| `patch`     |
| WHL     | `src/carriers/whl/` | `schedules` |
| YML     | `src/carriers/yml/` | `schedules` |
| ZIM     | `src/carriers/zim/` | `schedules` |

## Running a carrier

```bat
src\carriers\<carrier>\run.bat
```

Each `run.bat` activates the carrier's conda env, `cd`s into the carrier folder, and runs `python main.py`.

## Shared inputs

- `data/quotes.csv` — POL/POD pairs to query
- `data/locations.csv` — geocoded city cache

## Outputs

- `src/data/<carrier>/raw/` — raw API responses (one per query)
- `src/data/<carrier>/log/` — run logs + progress CSVs
- `src/data/<carrier>/csvs/` — per-run CSV exports
- `src/data/<carrier>/canonical/` — canonical JSON records
- `src/data/tables/` — aggregated tables across carriers
