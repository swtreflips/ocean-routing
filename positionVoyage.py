from patchright.sync_api import sync_playwright
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client
from geopy.distance import geodesic

# ----------------------------
# Config
# ----------------------------
SHIP_ID = "714453"
TARGET_PORT = "Nhava Sheva, India"

load_dotenv(Path(__file__).resolve().parent / ".env")


def fetch_endpoint(page, ship_id, endpoint):
    """Hit vessels/{ship_id}/{endpoint} from within the browser context (same as example.py)."""
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


def get_port_coords(name):
    """Look up a port's latitude/longitude from the Supabase ports table."""
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    r = (
        client.table("ports")
        .select("canonical_name,latitude,longitude")
        .eq("canonical_name", name)
        .limit(1)
        .execute()
    )
    if not r.data:
        raise SystemExit(f"Port not found: {name}")
    row = r.data[0]
    return float(row["latitude"]), float(row["longitude"])


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

    # ---------------- Fetch position + voyage ----------------
    print(f"📍 Fetching position for {SHIP_ID}")
    position = fetch_endpoint(page, SHIP_ID, "position")

    print(f"🧭 Fetching voyage for {SHIP_ID}")
    voyage = fetch_endpoint(page, SHIP_ID, "voyage")

    # ---------------- In-memory distance check (no file written) ----------------
    if (
        isinstance(position, dict) and not position.get("error")
        and isinstance(voyage, dict) and not voyage.get("error")
    ):
        v_lat, v_lon = extract_coords(position)
        p_lat, p_lon = get_port_coords(TARGET_PORT)
        d = geodesic((v_lat, v_lon), (p_lat, p_lon))
        print(f"🚢 Vessel {SHIP_ID}: ({v_lat}, {v_lon})")
        print(f"⚓ {TARGET_PORT}: ({p_lat}, {p_lon})")
        print(f"📏 Distance: {d.km:.2f} km  /  {d.miles:.2f} miles")
    else:
        print("❌ Error fetching vessel data:")
        print("  position:", json.dumps(position, ensure_ascii=False)[:300])
        print("  voyage:  ", json.dumps(voyage, ensure_ascii=False)[:300])

    context.close()
