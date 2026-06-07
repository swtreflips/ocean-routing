"""Local-file persistence for the alert engine.

watchlist.json  — list of tracked shipments (the immutable plan snapshot).
state.json      — per-shipment mutable tracking state (cursor, edges, fired keys).
alerts.jsonl    — append-only log, one JSON alert per line.
debug/<ts>.log  — per-tick human-readable trace.

All state lives here so the worker is restartable: kill it, rerun, it resumes.
"""

import json
import datetime
from pathlib import Path

from . import config


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dirs() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data) -> None:
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)   # atomic on same filesystem


# ---- watchlist ----
def load_watchlist() -> list[dict]:
    return _load_json(config.WATCHLIST_PATH, [])


def save_watchlist(watchlist: list[dict]) -> None:
    _save_json(config.WATCHLIST_PATH, watchlist)


# ---- state (keyed by shipment_id) ----
def load_state() -> dict:
    return _load_json(config.STATE_PATH, {})


def save_state(state: dict) -> None:
    _save_json(config.STATE_PATH, state)


# ---- alerts (append-only) ----
def append_alerts(alerts: list[dict]) -> None:
    if not alerts:
        return
    ensure_dirs()
    with config.ALERTS_PATH.open("a", encoding="utf-8") as f:
        for a in alerts:
            a = {"logged_at": _utcnow(), **a}
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


# ---- per-tick debug trace ----
def write_debug(lines: list[str]) -> Path:
    ensure_dirs()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = config.DEBUG_DIR / f"{stamp}.log"
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path
