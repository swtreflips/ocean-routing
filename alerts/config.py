"""Phase 1 alert-engine configuration.

Buffers are deliberately generous to start — tighten once calibrated against a few
manual ground-truth checks. All durations in days unless noted.
"""

from pathlib import Path

# ---- paths ----
ALERTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ALERTS_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"

DATA_DIR = ALERTS_DIR / "data"
DEBUG_DIR = DATA_DIR / "debug"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
STATE_PATH = DATA_DIR / "state.json"
ALERTS_PATH = DATA_DIR / "alerts.jsonl"

# ---- buffer times / thresholds ----
ON_TIME_TOLERANCE_DAYS = 1.0   # +/- window for on-time vs late classification
MCT_DAYS = 2.0                 # minimum connection time at a transshipment port
SPEED_FLOOR_KN = 1.0          # below this SOG, skip ETA projection (stopped/anchored)
ARRIVE_RADIUS_MI = 50.0       # geofence radius that counts as "at" a port
APPROACH_RADIUS_MI = 150.0    # wider ring meaning "approaching" a port

# ---- run cadence (informational; used by run.py loop mode) ----
CADENCE_HOURS = 6

# ---- acquisition ----
SHIP_BOOTSTRAP_ID = "9765586"   # IMO used to warm the MarineTraffic session
MT_PROFILE_DIR = PROJECT_ROOT / "mt_profile"
