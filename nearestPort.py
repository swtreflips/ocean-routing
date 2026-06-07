"""Find the nearest seaport to a vessel's live MarineTraffic position, using PostGIS.

Fetches one vessel's live position from MarineTraffic (warm browser session, same as
positionVoyage.py), then asks Postgres/PostGIS for the closest port via the
`nearest_ports(...)` RPC. Nothing is written to disk.

Prereq: create the SQL function once in the Supabase SQL Editor:

    CREATE OR REPLACE FUNCTION nearest_ports(
        in_lat double precision, in_lon double precision,
        in_limit integer DEFAULT 1, in_types text[] DEFAULT NULL)
    RETURNS TABLE (canonical_name text, unlocode text, type text,
                   latitude double precision, longitude double precision,
                   distance_km double precision, distance_miles double precision)
    LANGUAGE sql STABLE AS $$
        SELECT p.canonical_name, p.unlocode, p.type, p.latitude, p.longitude,
            ST_Distance(p.geom::geography,
                ST_SetSRID(ST_MakePoint(in_lon, in_lat), 4326)::geography)/1000.0   AS distance_km,
            ST_Distance(p.geom::geography,
                ST_SetSRID(ST_MakePoint(in_lon, in_lat), 4326)::geography)/1609.344 AS distance_miles
        FROM ports p
        WHERE p.geom IS NOT NULL AND (in_types IS NULL OR p.type = ANY (in_types))
        ORDER BY distance_km LIMIT in_limit;
    $$;

Run with the schedulesenv venv + UTF-8:
    PYTHONUTF8=1 python nearestPort.py
"""

from patchright.sync_api import sync_playwright
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

# ----------------------------
# Config
# ----------------------------
SHIP_ID = "714453"
PORT_TYPES = ["P"]   # seaports only; set to None for all types
N = 1                # how many nearest ports to return

load_dotenv(Path(__file__).resolve().parent / ".env")


def fetch_endpoint(page, ship_id, endpoint):
    """Hit vessels/{ship_id}/{endpoint} from within the browser context."""
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
            if (!res.ok) {
                return { error: true, status: res.status, text: await res.text() };
            }
            return await res.json();
        }""",
        {"shipId": ship_id, "endpoint": endpoint}
    )


def check_session(page, test_id):
    """True if the existing session already returns valid data (no bootstrap needed)."""
    try:
        result = fetch_endpoint(page, test_id, "position")
        return isinstance(result, dict) and not result.get("error")
    except Exception as e:
        print("⚠ Session check failed:", e)
        return False


def extract_coords(position):
    """Pull (lat, lon) out of the MT position response, defensively."""
    print("RAW position:", json.dumps(position, ensure_ascii=False)[:500])
    lat_keys = ("lat", "latitude", "LAT", "Latitude")
    lon_keys = ("lon", "lng", "long", "longitude", "LON", "Longitude")
    lat = next((position[k] for k in lat_keys if k in position), None)
    lon = next((position[k] for k in lon_keys if k in position), None)
    if lat is None or lon is None:
        raise SystemExit(f"Could not find lat/lon in position keys: {list(position)}")
    return float(lat), float(lon)


def nearest_port(v_lat, v_lon):
    """Ask PostGIS for the closest port(s) via the nearest_ports RPC."""
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    res = client.rpc("nearest_ports", {
        "in_lat": v_lat,
        "in_lon": v_lon,
        "in_limit": N,
        "in_types": PORT_TYPES,
    }).execute()
    return res.data or []


with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        channel="chrome",
        user_data_dir="mt_profile",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )

    page = context.new_page()

    page.goto(
        "https://www.marinetraffic.com/en/ais/home/centerx:-118.2/centery:33.7/zoom:11",
        wait_until="domcontentloaded"
    )

    # ---------------- Bootstrap (only if the session isn't already warm) ----------------
    if check_session(page, SHIP_ID):
        print("✅ Existing session is valid, no bootstrap needed.")
    else:
        print("🚀 Bootstrapping session...")
        search = page.locator("input[placeholder='Search MarineTraffic']:visible")
        search.wait_for(state="visible", timeout=15000)
        search.click()
        search.fill("9765586")
        page.wait_for_selector("ul.MuiList-root li a", timeout=10000)
        page.locator("ul.MuiList-root li a").first.click()
        page.wait_for_selector("h1", timeout=15000)
        page.wait_for_timeout(3000)
        print("✅ Session bootstrapped.")

    # ---------------- Fetch live position ----------------
    print(f"📍 Fetching position for {SHIP_ID}")
    position = fetch_endpoint(page, SHIP_ID, "position")

    # ---------------- Nearest-port via PostGIS (no file written) ----------------
    if isinstance(position, dict) and not position.get("error"):
        v_lat, v_lon = extract_coords(position)
        ports = nearest_port(v_lat, v_lon)
        print(f"🚢 Vessel {SHIP_ID}: ({v_lat}, {v_lon})")
        if ports:
            for i, port in enumerate(ports, 1):
                tag = "⚓ Nearest seaport:" if N == 1 else f"  {i}."
                print(
                    f"{tag} {port['canonical_name']} "
                    f"({port.get('unlocode')}) — "
                    f"{port['distance_km']:.2f} km / {port['distance_miles']:.2f} miles"
                )
        else:
            print("❌ No port returned (is the nearest_ports function created?).")
    else:
        print("❌ Error fetching vessel position:")
        print("  position:", json.dumps(position, ensure_ascii=False)[:300])

    context.close()
