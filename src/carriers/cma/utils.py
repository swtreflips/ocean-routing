from pathlib import Path
from datetime import datetime, timedelta
import calendar
import hashlib
import shutil
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import json
from bs4 import BeautifulSoup, Comment
import traceback

CARRIER_DIR = Path(__file__).resolve().parent
# === LOAD DATA ===
with open(CARRIER_DIR / "assets" / "cma_cities.json", "r", encoding="utf-8") as f:
    cma_cities = json.load(f)


def get_month_periods(year, month):
    """Return reference dates for a given month: start, mid, end."""
    start = datetime(year, month, 1)
    mid = datetime(year, month, 15)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day)
    return {'start': start, 'mid': mid, 'end': end}


def assign_snapshot(date_input):
    """
    Assign a snapshot period to a given date.
    - If within 5 days of the 1st → snap to that 1st.
    - If within 5 days of the 15th → snap to the 15th.
    - If day >= 28 → snap to the 1st of next month.
    - Otherwise → keep the original date.
    """
    date_input = datetime.strptime(date_input, '%Y-%m-%d')
    
    year, month = date_input.year, date_input.month
    periods = get_month_periods(year, month)

    # Snap near the 1st (only if in first 5 days of the month)
    if 1 <= date_input.day <= 5:
        return periods['start'].date()

    # Snap near the 15th
    if abs((date_input - periods['mid']).days) <= 5:
        return periods['mid'].date()

    # Snap to 1st of next month if late in month
    if date_input.day >= 28:
        if month == 12:
            return datetime(year + 1, 1, 1).date()
        else:
            return datetime(year, month + 1, 1).date()

    # Otherwise, keep original date
    return date_input.date()

def load_progress(quotes_file, progress_file):
    """
    Load existing progress file if it exists, otherwise 
    initialize from quotes.csv with the required columns.
    """
    if progress_file.exists():
        # Continue from saved progress
        quotes_progress = pd.read_csv(progress_file)
    else:
        # First run → start from quotes.csv
        quotes_progress = pd.read_csv(quotes_file).copy()

        if "LastCY" not in quotes_progress.columns:
            quotes_progress["LastCY"] = None

        if "status" not in quotes_progress.columns:
            quotes_progress["status"] = "pending"

        if "result_file" not in quotes_progress.columns:
            quotes_progress["result_file"] = None

    return quotes_progress


def get_unique_path(base_path: Path) -> Path:
    """
    Ensure the path is unique by appending (1), (2)... if needed.
    Example: file.json -> file(1).json -> file(2).json
    """
    if not base_path.exists():
        return base_path

    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent

    counter = 1
    while True:
        new_name = f"{stem}({counter}){suffix}"
        candidate = parent / new_name
        if not candidate.exists():
            return candidate
        counter += 1

def get_unique_filename(base_path: Path) -> Path:
    """
    Ensure unique filename by appending A, B, C... if file exists.
    Example: COSCO09.22.25.csv -> COSCO09.22.25B.csv -> COSCO09.22.25C.csv
    """
    if not base_path.exists():
        return base_path
    
    stem = base_path.stem  # e.g. "COSCO09.22.25"
    suffix = base_path.suffix  # ".csv"
    
    letter = ord("A")
    while True:
        new_file = base_path.with_name(f"{stem}{chr(letter)}{suffix}")
        if not new_file.exists():
            return new_file
        letter += 1



def geocode_city(city_name, geolocator):
    """Return lat/lon for a city name using Nominatim."""
    try:
        loc = geolocator.geocode(f"{city_name}, USA")
        if loc:
            return loc.latitude, loc.longitude
    except Exception as e:
        print(f"Geocoding failed for {city_name}: {e}")
    return None, None

def resolve_missing_locations(quotes_progress, locations, locations_file, geocode_fn, geolocator):
    """
    Ensure all destinations in quotes_progress exist in locations.
    If missing, attempt to geocode and update the locations file.
    
    Returns:
        pd.DataFrame: Updated locations dataframe
    """
    missing = set(quotes_progress["Final Destination"].unique()) - set(locations["Place of Discharge"].unique())

    for city in missing:
        lat, lon = geocode_fn(city, geolocator)
        if lat and lon:
            print(f"✅ Geocoded {city}: {lat}, {lon}")
            new_row = pd.DataFrame([{
                "Place of Discharge": city,
                "Latitude": lat,
                "Longitude": lon,
                "Type": "Geocoded"
            }])
            locations = pd.concat([locations, new_row], ignore_index=True)
        else:
            print(f"⚠️ Could not geocode {city}")

    # Save & reload for consistency
    locations.to_csv(locations_file, index=False)
    updated_locations = pd.read_csv(locations_file)
    return updated_locations


def build_voronoi_lookup(quotes_progress, locations, gdf_voronoi):
    """
    Build a lookup dict mapping Final Destination -> CityYard 
    by spatially joining destinations with Voronoi polygons.
    
    Args:
        quotes_progress (pd.DataFrame): Quotes progress dataframe.
        locations (pd.DataFrame): Locations with lat/lon.
        gdf_voronoi (gpd.GeoDataFrame): Voronoi polygons with 'CityYard'.
    
    Returns:
        dict: {Final Destination: CityYard}
    """
    # Step 1: Deduplicate destinations
    unique_dest = quotes_progress[["Final Destination"]].drop_duplicates()

    # Step 2: Attach coordinates from locations
    unique_dest = unique_dest.merge(
        locations,
        left_on="Final Destination",
        right_on="Place of Discharge",
        how="left"
    )

    # Step 3: Convert to GeoDataFrame and reproject to Voronoi CRS
    gdf_unique = gpd.GeoDataFrame(
        unique_dest,
        geometry=gpd.points_from_xy(unique_dest["Longitude"], unique_dest["Latitude"]),
        crs="EPSG:4326"
    )

    # Only reproject if different
    if gdf_voronoi.crs and gdf_voronoi.crs != gdf_unique.crs:
        gdf_unique = gdf_unique.to_crs(gdf_voronoi.crs)

    # Step 4: Spatial join with Voronoi polygons
    dest_with_yard = gpd.sjoin(
        gdf_unique, gdf_voronoi,
        how="left",
        predicate="within"
    )

    # Step 5: Build lookup dict
    lookup = dest_with_yard.set_index("Final Destination")["CityYard"].to_dict()

    return lookup


def get_locations(city_name):
    city_entry = cma_cities.get(city_name)
    if not city_entry:
        return []

    locations = []
    for loc in city_entry:
        name = loc.get("name")            # "MIAMI, FL ; US ; USMIA"
        capsule = loc.get("capsule")      # "Port"
        place_name = loc.get("placeName") # "miami, fl"
        place_code = loc.get("placeCode") # "USMIA"

        if name and capsule:
            locations.append({
                "description": name,      # used for CMA calls
                "type": capsule,
                "placeName": place_name,  # 👈 preserved
                "placeCode": place_code   # 👈 optional but very useful
            })

    return locations




def process_html_file(html_path: Path, html_archive_dir: Path = None):
    json_path = html_path.with_suffix(".json")

    try:
        # ---------- LOAD HTML ----------
        with html_path.open("r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "lxml")

        # ---------- EXTRACT METADATA ----------
        metadata = extract_request_metadata(soup)

        # ---------- PARSE SCHEDULES ----------
        schedule_cards = soup.select("article.card-route-horizontal")

        schedules = []
        for idx, article in enumerate(schedule_cards, start=1):
            schedules.append({
                "schedule_id": f"{idx:02d}",
                "summary": parse_summary_panel(article),
                "cutoffs": extract_cutoffs(article),
                "legs": build_legs(article)
            })

        result = {
            "request": metadata,
            "schedules": schedules
        }

        # ---------- WRITE JSON ----------
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        # ---------- ARCHIVE OR DELETE HTML ONLY AFTER SUCCESS ----------
        if html_archive_dir is not None:
            html_archive_dir.mkdir(parents=True, exist_ok=True)
            dst = get_unique_path(html_archive_dir / html_path.name)
            shutil.move(str(html_path), str(dst))
        else:
            html_path.unlink()

        print(f"✅ {html_path.name} → {json_path.name}")

    except Exception as e:
        print(f"❌ Failed: {html_path.name}")
        traceback.print_exc()



# ---------- HELPERS ----------

def is_location_li(li):
    classes = li.get("class", [])
    return (
        "ico-dot" in classes
        or "transhipment" in classes
    )


def extract_request_metadata(soup):
    metadata = {}

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        text = comment.strip()

        if "=" in text and "|" in text:
            parts = [p.strip() for p in text.split("|")]
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    metadata[k.strip()] = v.strip()
            break  # only one wrapper comment expected

    return metadata



def extract_cutoffs(article):
    cutoffs = {}

    # find the cutoff title span
    title_span = article.select_one("span.cut-off--title")
    if not title_span:
        return cutoffs

    # the next <ul> after this title is the cutoff list
    cutoff_ul = title_span.find_next_sibling("ul")
    if not cutoff_ul:
        return cutoffs

    for li in cutoff_ul.find_all("li", recursive=False):
        # label = text node before <span>
        label = li.contents[0].strip() if li.contents else None
        span = li.find("span")

        if label and span:
            cutoffs[label] = span.get_text(strip=True)

    return cutoffs

def parse_summary_panel(article):
    # ---------- ETD ----------
    etd_li = article.select_one("li.DepartureDatesCls")
    etd = etd_li.get("data-event-date") if etd_li else None

    # ---------- ETA ----------
    eta_li = article.select_one("li.ArrivalDatesCls")
    eta = eta_li.get("data-event-date") if eta_li else None

    # ---------- TRANSIT DAYS ----------
    transit_div = article.select_one("div.transit.remaining")
    transit_days = int(transit_div.get("data-value")) if transit_div else None

    return {
        "etd": etd,
        "eta": eta,
        "transit_days": transit_days
    }
def extract_location(li):
    classes = li.get("class", [])

    location = {
        "city": None,
        "terminal": None,
        "etd": None,
        "eta": None
    }

    # ---------- DATES ----------
    dates = [s.get_text(strip=True) for s in li.select("span.date")]

    if "transhipment" in classes:
        # CMA rule: first = ETD, second = ETA
        if len(dates) >= 1:
            location["etd"] = dates[0]
        if len(dates) >= 2:
            location["eta"] = dates[1]
    else:
        # ico-dot (POL / normal / POD)
        if len(dates) >= 1:
            location["etd"] = dates[0]

    # ---------- CITY ----------
    capsule = li.select_one("a.capsule-container")
    if capsule:
        text = capsule.get_text(" ", strip=True)
        location["city"] = (
            text.replace("POL", "")
                .replace("POD", "")
                .replace("Port", "")
                .strip()
        )

    # ---------- TERMINAL ----------
    terminal = li.select_one("a[href*='/terminal/detail']")
    if terminal:
        location["terminal"] = terminal.get_text(strip=True)

    return location

def extract_transport(li):
    classes = li.get("class", [])

    transport = {
        "mode": None,
        "vessel": None,
        "service": None,
        "voyage_ref": None,
        "co2": None
    }

    # ===============================
    # 🚆 HARD STOP: RAIL
    # ===============================
    if "rail" in classes:
        transport["mode"] = "Rail"
        return transport   # ← NOTHING else allowed to run

    # ===============================
    # ---------- CASE 1: INTERMODAL ----------
    if "intermodal" in classes:
        label = li.get_text(" ", strip=True).lower()

        # 🚆 Rail (text-based detection)
        if "rail" in label:
            transport["mode"] = "Rail"
            return transport

        # 🚢 Feeder / barge
        transport["mode"] = "Maritime"

        if li.contents:
            transport["vessel"] = li.contents[0].strip()

        return transport

    # ===============================
    # 🚢 STRUCTURED VESSEL
    # ===============================
    if li.find("dt", string=lambda s: s and "Vessel" in s):
        transport["mode"] = "Maritime"

        for dl in li.find_all("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                key = dt.get_text(strip=True)

                if key == "Vessel":
                    transport["vessel"] = dd.get_text(strip=True)

                elif key == "Service":
                    transport["service"] = dd.get_text(strip=True)

                elif "Voyage" in key:
                    a = dd.find("a")
                    transport["voyage_ref"] = (
                        a.get_text(strip=True) if a else dd.get_text(strip=True)
                    )

                if dd.has_attr("data-value"):
                    transport["co2"] = dd["data-value"]

        return transport

    return None
# ---------- MAIN PARSER ----------

def parse_transports(article):
    route_uls = article.select("ul.route")
    if len(route_uls) < 2:
        return []

    route_ul = route_uls[1]
    lis = route_ul.find_all("li", recursive=False)

    transports = []

    for li in lis:
        if is_location_li(li):
            continue

        transport = extract_transport(li)
        if transport:
            transports.append(transport)

    return transports


def build_legs(article):
    locations = parse_locations(article)
    transports = parse_transports(article)

    legs = []

    for idx, (loc, trans) in enumerate(zip(locations, transports), start=1):
        legs.append({
            "sequence": idx,
            "location": loc["location"],
            "transport": trans
        })

    return legs


def parse_locations(article):
    route_uls = article.select("ul.route")
    if len(route_uls) < 2:
        return []

    route_ul = route_uls[1]
    lis = route_ul.find_all("li", recursive=False)

    locations = []
    sequence = 1

    for li in lis:
        if is_location_li(li):
            locations.append({
                "sequence": sequence,
                "location": extract_location(li)
            })
            sequence += 1

    # ❌ drop final location (LastCY / POD)
    if locations:
        locations.pop()

    return locations

def transform_html_to_json(html_path: Path, html_archive_dir: Path = None):
    json_path = html_path.with_suffix(".json")

    try:
        with html_path.open("r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "lxml")

        metadata = extract_request_metadata(soup)
        schedule_cards = soup.select("article.card-route-horizontal")

        schedules = []
        for idx, article in enumerate(schedule_cards, start=1):
            schedules.append({
                "schedule_id": f"{idx:02d}",
                "summary": parse_summary_panel(article),
                "cutoffs": extract_cutoffs(article),
                "legs": build_legs(article)
            })

        result = {
            "request": metadata,
            "schedules": schedules
        }

        with json_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        # Archive HTML (preserved for re-parsing) or delete if no archive dir given
        if html_archive_dir is not None:
            html_archive_dir.mkdir(parents=True, exist_ok=True)
            dst = get_unique_path(html_archive_dir / html_path.name)
            shutil.move(str(html_path), str(dst))
        else:
            html_path.unlink()

        print(f"🧩 Parsed → {json_path.name}")

        return True

    except Exception:
        print(f"❌ Transform failed: {html_path.name}")
        traceback.print_exc()
        return False


def batch_transform_processing_dir(processing_dir: Path, html_archive_dir: Path = None):
    html_files = sorted(processing_dir.glob("*.html"))

    print(f"🔍 Found {len(html_files)} HTML files in {processing_dir}")

    for html_file in html_files:
        transform_html_to_json(html_file, html_archive_dir)

    print("🏁 CMA HTML → JSON batch complete")

# ==========================================================================
# Canonical-record builder (was utilscmacanonical.py)
# ==========================================================================


def normalize_city_state(value):
    """Normalize 'boston, ma' -> 'Boston, MA'.
    Drops a trailing 2-letter country code ('norfolk, va, US' -> 'Norfolk, VA').
    """
    if not isinstance(value, str) or "," not in value:
        return value
    parts = [p.strip() for p in value.split(",")]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-1].isalpha():
        parts = parts[:-1]
    if len(parts) < 2:
        return value
    city = ", ".join(parts[:-1]).title()
    state = parts[-1].upper()
    return f"{city}, {state}"


# === CANONICAL TRANSFORMATION HELPERS (generic — copy across carriers) ===

_MONTH_ABBR = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _iso_date_or_none(value):
    """
    Return 'YYYY-MM-DD' or None. Handles every CMA date string observed so far:
      - ISO 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM...'
      - 'MM/DD/YYYY'                       (summary.etd / summary.eta)
      - 'Tuesday, 17-FEB-2026'             (leg location.etd / leg location.eta)
      - '15-FEB-2026, 03:10 AM'            (cutoffs.Port)
      - '17-FEB-2026'                      (bare DD-MMM-YYYY)
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Strip leading weekday like "Tuesday, "
    if "," in s:
        head, _, tail = s.partition(",")
        if head.strip() in _WEEKDAYS:
            s = tail.strip()

    # Drop trailing time after comma ("15-FEB-2026, 03:10 AM")
    if "," in s:
        s = s.split(",", 1)[0].strip()

    # Drop time after T or whitespace
    s = s.split("T", 1)[0].split(" ", 1)[0]

    # ISO
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        pass

    # MM/DD/YYYY
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass

    # DD-MMM-YYYY (locale-independent via _MONTH_ABBR)
    parts = s.split("-")
    if len(parts) == 3:
        day, mon, year = parts
        mon_num = _MONTH_ABBR.get(mon.upper())
        if mon_num and day.isdigit() and year.isdigit() and len(year) == 4:
            return f"{year}-{mon_num}-{int(day):02d}"

    return None


def _to_int_or_none(value):
    """int(float(value)) on success, None on None / non-numeric."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _schedule_uuid(carrier, pol, pod, etd_iso, mother_vessel, raw_schedule_id):
    """Deterministic 16-char hex id for one schedule (Supabase upsert-safe)."""
    fields = [carrier, pol, pod, etd_iso, mother_vessel, raw_schedule_id]
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _query_uuid(carrier, pol, last_cy, query_date):
    """Deterministic 16-char hex id for a whole query (top-level canonical id)."""
    fields = [carrier, pol, last_cy, query_date]
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# === CMA-SPECIFIC LEG NORMALIZATION ===

# CMA uses titlecase mode names. Reserve Barge/Feeder/Water for future-proofing
# per structurePhilosophy.md (a feeder vessel still counts as an ocean leg).
_CMA_OCEAN_MODES = {"Maritime", "Barge", "Feeder", "Water"}
_CMA_GROUND_LABELS = {"Rail": "RAIL", "Truck": "TRUCK", "Road": "TRUCK"}


def _cma_leg_label(leg):
    """Vessel name for ocean legs; mode label (RAIL/TRUCK) for ground."""
    transport = leg.get("transport") or {}
    mode = transport.get("mode") or ""
    if mode in _CMA_OCEAN_MODES:
        vessel = (transport.get("vessel") or "").strip()
        return vessel or mode.upper()
    return _CMA_GROUND_LABELS.get(mode, mode.upper() if mode else "")


def _cma_legs(schedule, last_cy, summary_eta_raw):
    """
    Normalize a CMA schedule's legs into:
      [{pol, pod, label, is_ocean, etd, eta}, ...]

    CMA encodes one location per leg (the START point). The end of leg i is
    the start of leg i+1; the end of the last leg is the request's last_cy at
    summary.eta time.
    """
    raw_legs = schedule.get("legs") or []
    if not raw_legs:
        return []

    out = []
    for i, leg in enumerate(raw_legs):
        mode = (leg.get("transport") or {}).get("mode") or ""
        loc = leg.get("location") or {}
        if i + 1 < len(raw_legs):
            next_loc = raw_legs[i + 1].get("location") or {}
            pod = next_loc.get("city")
            # CMA stores only one date per location (its etd). For intermediate
            # legs there is no eta field, so the arrival at the next location is
            # taken from when the next leg departs it.
            eta = next_loc.get("eta") or next_loc.get("etd")
        else:
            pod = last_cy
            eta = summary_eta_raw
        out.append({
            "pol": loc.get("city"),
            "pod": pod,
            "label": _cma_leg_label(leg),
            "is_ocean": mode in _CMA_OCEAN_MODES,
            "etd": loc.get("etd"),
            "eta": eta,
        })
    return out


def _cma_legs_summary(legs):
    """
    Returns (transport_type, mother_vessel, ts_ports, ts_vessels,
             route_ports, vessel_sequence).

    Visible chain is ocean-only: every non-ocean leg (pre-ocean drayage,
    mid-route ground transfers, post-ocean inland delivery) is dropped from
    route_ports and vessel_sequence. Post-ocean inland is implicitly signaled
    downstream by `port_of_discharge != last_cy`.

    Invariants (when there is at least one ocean leg):
      len(vessel_sequence) == ocean_legs == transshipments + 1
      len(route_ports)     == ocean_legs + 1
      len(ts_ports)        == ocean_legs - 1 == transshipments
    """
    if not legs:
        return "Direct", None, [], [], [], []

    ocean = [lg for lg in legs if lg["is_ocean"]]
    n_ocean = len(ocean)

    if n_ocean == 0:
        return "Direct", None, [], [], [], []

    transport_type = "Direct" if n_ocean == 1 else f"{n_ocean - 1} TS"
    mother_vessel = ocean[0]["label"] or None
    ts_ports = [lg["pod"] for lg in ocean[:-1]]
    ts_vessels = [lg["label"] for lg in ocean[1:]]
    route_ports = [ocean[0]["pol"]] + [lg["pod"] for lg in ocean]
    vessel_sequence = [lg["label"] for lg in ocean]

    return transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence


def _cma_port_cutoff(schedule):
    """Extract cutoffs.Port and return its ISO date or None."""
    cutoffs = schedule.get("cutoffs") or {}
    return _iso_date_or_none(cutoffs.get("Port"))


# === CANONICAL RECORD BUILDER ===

def build_canonical_record(file_path):
    """
    Read a CMA post-HTML-transform JSON and return ONE canonical record per
    query with schedules nested. Returns None if no usable schedules.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]
    if not items:
        return None
    item = items[0]  # CMA wraps one query per file

    request = item.get("request") or {}
    pol = request.get("POL")
    last_cy = request.get("LastCY")
    final_destination = request.get("FinalDestination")
    query_date = request.get("query_date")
    snapshot_date = request.get("snapshot_date")

    schedules = []
    for schedule in item.get("schedules") or []:
        summary = schedule.get("summary") or {}
        legs = _cma_legs(schedule, last_cy, summary.get("eta"))
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = _cma_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = ocean[-1]["pod"] if ocean else legs[-1]["pod"]
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        # Inland destination: the last ocean leg is followed by a non-ocean
        # (rail/truck) leg. CMA stores the rail-ready date at the inland leg's
        # start; the actual port arrival is ~4 days earlier per CMA's own
        # port-only timeline.
        if pod_eta and ocean and ocean[-1] is not legs[-1]:
            pod_eta = (
                datetime.strptime(pod_eta, "%Y-%m-%d") - timedelta(days=4)
            ).strftime("%Y-%m-%d")
        etd_iso = _iso_date_or_none(legs[0]["etd"])
        eta_iso = _iso_date_or_none(legs[-1]["eta"])

        schedules.append({
            "id": _schedule_uuid("CMA", pol, pod, etd_iso, mother_vessel, schedule.get("schedule_id")),
            "port_of_discharge": normalize_city_state(pod),
            "cutoff_date": _cma_port_cutoff(schedule),
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": _to_int_or_none(summary.get("transit_days")),
            "transport_type": transport_type,
            "mother_vessel": mother_vessel,
            "ts_ports": ts_ports,
            "ts_vessels": ts_vessels,
            "route_ports": route_ports,
            "vessel_sequence": vessel_sequence,
        })

    if not schedules:
        return None

    return {
        "schema_version": 1,
        "id": _query_uuid("CMA", pol, last_cy, query_date),
        "carrier": {"code": "CMA", "name": "CMA CGM"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": normalize_city_state(last_cy),
        "final_destination": final_destination,
        "schedules": schedules,
    }
