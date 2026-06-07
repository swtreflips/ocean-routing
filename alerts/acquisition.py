"""AIS position acquisition via a warm MarineTraffic browser session.

Isolated on purpose: this is the fragile, swappable layer. If you later buy a real
AIS API, replace `fetch_positions` and nothing else in the engine changes.

Usage:
    with PositionFetcher() as fetcher:
        obs = fetcher.fetch_observations([755947, 2949, ...])
    # obs: {vessel_id: {"lat","lon","speed","status","arrival_ts",...} or None}

`status` (navigationalStatus) and `arrival_ts` (voyage.arrivalTimestamp) drive the
adaptive cadence; lat/lon/speed drive the geofence engine.
"""

import time
import random

from patchright.sync_api import sync_playwright

from . import config


def _fetch_endpoint(page, ship_id, endpoint):
    return page.evaluate(
        """async ({ shipId, endpoint }) => {
            const url = `https://www.marinetraffic.com/en/vessels/${shipId}/${endpoint}`;
            const res = await fetch(url, {
                credentials: "include",
                headers: {
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest"
                }
            });
            if (!res.ok) return { error: true, status: res.status };
            return await res.json();
        }""",
        {"shipId": str(ship_id), "endpoint": endpoint},
    )


class PositionFetcher:
    """Opens one persistent Chrome context; reuse it to fetch many vessels per tick."""

    def __init__(self):
        self._pw = None
        self._ctx = None
        self._page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(config.MT_PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.new_page()
        self._page.goto(
            "https://www.marinetraffic.com/en/ais/home/centerx:-118.2/centery:33.7/zoom:11",
            wait_until="domcontentloaded",
        )
        self._ensure_session()
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _ensure_session(self):
        """Warm the session once if the existing cookies aren't valid."""
        test = _fetch_endpoint(self._page, config.SHIP_BOOTSTRAP_ID, "position")
        if isinstance(test, dict) and not test.get("error"):
            return
        print("[acquisition] Bootstrapping MarineTraffic session...")
        search = self._page.locator("input[placeholder='Search MarineTraffic']:visible")
        search.wait_for(state="visible", timeout=15000)
        search.click()
        search.fill(config.SHIP_BOOTSTRAP_ID)
        self._page.wait_for_selector("ul.MuiList-root li a", timeout=10000)
        self._page.locator("ul.MuiList-root li a").first.click()
        self._page.wait_for_selector("h1", timeout=15000)
        self._page.wait_for_timeout(3000)

    def fetch_observations(self, vessel_ids) -> dict:
        """{vessel_id: obs-dict | None}. Fetches position + voyage with a randomized
        delay before each vessel (anti-bot). None on error/missing position."""
        out = {}
        lo, hi = config.FETCH_JITTER_SEC
        for vid in {v for v in vessel_ids if v is not None}:
            try:
                wait = random.uniform(lo, hi)
                print(f"[acquisition] waiting {wait:.1f}s before fetching {vid}...")
                time.sleep(wait)

                pos = _fetch_endpoint(self._page, vid, "position")
                if not (isinstance(pos, dict) and not pos.get("error") and "lat" in pos):
                    out[vid] = None
                    continue

                voy = _fetch_endpoint(self._page, vid, "voyage")
                arrival_ts = voy.get("arrivalTimestamp") if isinstance(voy, dict) else None

                out[vid] = {
                    "lat": float(pos["lat"]),
                    "lon": float(pos["lon"]),
                    "speed": pos.get("speed"),
                    "course": pos.get("course"),
                    "timestamp": pos.get("timestamp"),
                    "area": pos.get("areaName"),
                    "status": pos.get("navigationalStatus"),
                    "arrival_ts": arrival_ts,
                }
            except Exception as e:  # noqa: BLE001 - one bad vessel must not kill the tick
                print(f"[acquisition] fetch failed for {vid}: {e}")
                out[vid] = None
        return out
