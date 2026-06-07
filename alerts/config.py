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

# ---- routing ----
# ETA projection uses the realistic marine route distance (searoute) instead of a
# straight line. Falls back to great-circle automatically if searoute is missing or
# can't route a pair. Set False to force straight-line.
USE_SEAROUTE = True

# ---- adaptive cadence ----
# Each shipment carries its own next_check; the loop wakes every POLL_INTERVAL_MIN and
# only fetches shipments whose next_check has passed. Cadence is driven by the active
# vessel's navigational status + proximity to its destination — near port operations
# (moored / at anchor / approaching / arriving soon) is checked far more often than a
# vessel mid-ocean.
POLL_INTERVAL_MIN = 15          # how often --loop wakes to look for due shipments
MAX_RUNTIME_HOURS = 23.5        # exit ~30min before a daily relaunch so the mt_profile
                                # lock clears for a clean handoff; None = run forever
FETCH_JITTER_SEC = (5, 20)      # random delay before each vessel fetch (anti-bot)
ERROR_RETRY_MIN = 30            # recheck sooner after a fetch error

# Navigational statuses that mean "in port operations" -> highest frequency.
MOORED_STATUSES = ("Moored", "At Anchor")

# Cadence buckets (mirrors the proven AIS scraper). Minutes for moored, else hours.
CADENCE = {
    "moored_min": 45,       # Moored / At Anchor
    "near_or_today_h": 2,   # near destination, or ETA is today
    "lt4_h": 4,             # ETA < 4 days out
    "lt7_h": 8,             # ETA < 7 days out
    "gt10_h": 12,           # ETA > 10 days out
    "default_h": 8,         # 7-10 days out
    "no_eta_h": 12,         # no ETA known and not near port
}

# ---- acquisition ----
SHIP_BOOTSTRAP_ID = "9765586"   # IMO used to warm the MarineTraffic session
MT_PROFILE_DIR = PROJECT_ROOT / "mt_profile"
