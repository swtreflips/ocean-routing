from pathlib import Path
from datetime import datetime
import calendar
import hashlib
import shutil
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import json
import traceback
import re
import os
from bs4 import BeautifulSoup, Comment

CARRIER_DIR = Path(__file__).resolve().parent

# === LOAD DATA ===
with open(CARRIER_DIR / "assets" / "emc_cities.json", "r", encoding="utf-8") as f:
    emc_cities = json.load(f)


# REGEX
# --------------------------------------------------
DATETIME_RE = re.compile(r"[A-Z]{3}-\d{2}-\d{4}\s+\d{2}:\d{2}")



# Functions
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
    # Step 1: Deduplicate destinations.
    # Drop rows where Final Destination is empty — those rows are using the
    # "LastCY filled directly" workflow and don't need a Voronoi lookup. If
    # nothing is left, return an empty mapping so main.py's apply() falls
    # through to the existing LastCY for every row.
    unique_dest = quotes_progress[["Final Destination"]].dropna().drop_duplicates()
    if unique_dest.empty:
        return {}

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



def clean(txt: str) -> str:
    return " ".join((txt or "").split())


def is_leg_row(tr) -> bool:
    tds = tr.find_all("td", recursive=False)
    if len(tds) != 8:
        return False
    return clean(tds[0].get_text()).isdigit()


# --------------------------------------------------
# REQUEST (META COMMENT) EXTRACTION
# --------------------------------------------------
def extract_request_meta(soup) -> dict:
    ALLOWED_KEYS = {
        "POL",
        "LastCY",
        "OFQ",
        "snapshot_date",
        "query_date"
    }

    meta = {}

    for comment in soup.find_all(string=lambda x: isinstance(x, Comment)):
        text = comment.strip()

        # Only consider comments that look like request metadata
        if not re.search(r"\bPOL\b|\bOFQ\b|\bLastCY\b", text, re.IGNORECASE):
            continue

        parts = re.split(r"[|\n]+", text)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            if "=" in part:
                key, value = part.split("=", 1)
            elif ":" in part:
                key, value = part.split(":", 1)
            else:
                continue

            key = key.strip()

            # 🔒 STRICT FILTER
            if key not in ALLOWED_KEYS:
                continue

            meta[key] = value.strip()

        break  # only one request block per file

    return meta

# --------------------------------------------------
# SCHEDULE-LEVEL EXTRACTION
# --------------------------------------------------
def extract_seq_from_table(table) -> str | None:
    table_id = table.get("id", "")
    if table_id.startswith("detailSeq"):
        return table_id.replace("detailSeq", "")
    return None


def extract_transit_days(soup, table) -> str | None:
    seq = extract_seq_from_table(table)
    if not seq:
        return None

    span = soup.select_one(
        f'span[ecType="modalbox"][params="seq={seq}"]'
    )
    if not span:
        return None

    details_td = span.find_parent("td")
    if not details_td:
        return None

    transit_td = details_td.find_previous_sibling("td")
    if not transit_td:
        return None

    value = clean(transit_td.get_text())
    return value if value.isdigit() else None


def extract_cutoff_date(soup, table) -> str | None:
    table_id = table.get("id", "")
    if not table_id.startswith("detailSeq"):
        return None

    seq = table_id.replace("detailSeq", "")

    span = soup.select_one(
        f'span[ecType="modalbox"][params="seq={seq}"]'
    )
    if not span:
        return None

    tr = span.find_parent("tr")
    if not tr:
        return None

    for td in tr.find_all("td"):
        text = clean(td.get_text())
        if DATETIME_RE.search(text):
            return text

    return None


def extract_schedules(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    schedules = []

    for table in soup.select('table[id^="detailSeq"]'):
        schedule_id = table.get("id")

        transit_days = extract_transit_days(soup, table)
        cutoff_date = extract_cutoff_date(soup, table)
        legs = []

        for tr in table.find_all("tr"):
            if not is_leg_row(tr):
                continue

            tds = tr.find_all("td", recursive=False)

            legs.append({
                "leg_no": clean(tds[0].get_text()),
                "from": clean(tds[1].get_text()),
                "to": clean(tds[2].get_text()),
                "departure_date": clean(tds[3].get_text()),
                "arrival_date": clean(tds[4].get_text()),
                "service": clean(tds[5].get_text()),
                "vessel_voyage": clean(tds[6].get_text()),
                "transit_time_days": clean(tds[7].get_text()),
            })

        if legs:
            schedules.append({
                "schedule_id": schedule_id,
                "transit_days": transit_days,
                "cutoff_date": cutoff_date,
                "legs": legs
            })

    return schedules


# --------------------------------------------------
# DOCUMENT WRAPPER (OPTION A)
# --------------------------------------------------
def extract_document(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    request = extract_request_meta(soup)
    schedules = extract_schedules(html)

    return {
        "request": request,
        "schedules": schedules
    }




def transform_html_to_json(html_path: Path, html_archive_dir: Path = None) -> bool:
    json_path = html_path.with_suffix(".json")

    try:
        with html_path.open("r", encoding="utf-8") as f:
            html = f.read()

        document = extract_document(html)

        with json_path.open("w", encoding="utf-8") as f:
            json.dump(document, f, indent=2)

        # Archive HTML (preserved for re-parsing) or delete if no archive dir given
        if html_archive_dir is not None:
            html_archive_dir.mkdir(parents=True, exist_ok=True)
            dst = get_unique_path(html_archive_dir / html_path.name)
            shutil.move(str(html_path), str(dst))
        else:
            html_path.unlink()

        print(f"🌲 Evergreen parsed → {json_path.name}")

        return True

    except Exception:
        print(f"❌ Evergreen transform failed: {html_path.name}")
        traceback.print_exc()
        return False


def batch_transform_processing_dir(processing_dir: Path, html_archive_dir: Path = None):
    html_files = sorted(processing_dir.glob("*.html"))

    print(f"🔍 Found {len(html_files)} Evergreen HTML files in {processing_dir}")

    for html_file in html_files:
        transform_html_to_json(html_file, html_archive_dir)

    print("🏁 Evergreen HTML → JSON batch complete")



# Dataframe structuring helpers and functions

def get_transport_type(legs: list[dict]) -> str:
    """
    Determine transport type based on number of vessel legs.
    """
    vessel_legs = [
        leg for leg in legs
        if leg.get("service") not in {"WAITING", "Intermodal"}
    ]

    vessel_count = len(vessel_legs)

    if vessel_count <= 1:
        return "Direct"

    return f"{vessel_count - 1} TS"

def get_pod_eta(legs: list[dict]) -> str:
    vessel_legs = [
        leg for leg in legs
        if leg["service"] not in ("WAITING", "Intermodal")
    ]

    if not vessel_legs:
        return ""

    return vessel_legs[-1]["arrival_date"]

def get_ts_ports(legs: list[dict]) -> str:
    vessel_legs = [
        leg for leg in legs
        if leg["service"] not in ("WAITING", "Intermodal")
    ]

    # Direct
    if len(vessel_legs) <= 1:
        return ""

    # TS ports = "to" of all vessel legs except the last one
    ts_ports = [leg["to"] for leg in vessel_legs[:-1]]

    return " - ".join(ts_ports)

def get_pod(legs: list[dict]) -> str:
    vessel_legs = [
        leg for leg in legs
        if leg["service"] not in ("WAITING", "Intermodal")
    ]

    if not vessel_legs:
        return ""

    return vessel_legs[-1]["to"]

def get_ts_vessels(legs: list[dict]) -> str:
    """
    Returns transshipment vessel(s):
    - "" if direct
    - vessel_voyage of remaining vessel legs, concatenated with " - "
    """
    vessel_legs = [
        leg for leg in legs
        if leg.get("service") not in {"WAITING", "Intermodal"}
    ]

    # Direct → no TS vessels
    if len(vessel_legs) <= 1:
        return ""

    # Exclude mother vessel
    ts_vessels = [
        leg.get("vessel_voyage") for leg in vessel_legs[1:]
        if leg.get("vessel_voyage")
    ]

    return " - ".join(ts_vessels)

def build_schedule_dataframe(processing_dir):
    """
    Reads all Evergreen JSON files from processing_dir,
    extracts schedule data, and returns a combined DataFrame.
    """
    records = []

    for file in os.listdir(processing_dir):
        if not file.endswith(".json"):
            continue

        full_path = os.path.join(processing_dir, file)
        with open(full_path, "r", encoding="utf-8") as infile:
            data = json.load(infile)

        carrier = "EMC"

        request = data.get("request", {})
        schedules = data.get("schedules", [])

        for sched in schedules:
            legs = sched.get("legs", [])
            if not legs:
                continue

            first_leg = legs[0]
            last_leg = legs[-1]

            record = {
                "Carrier": carrier,
                "Port of Loading": request.get("POL", ""),
                "Port of Discharge": normalize_pod(get_pod(legs)),
                "Last CY": request.get("LastCY", ""),
                "Final Destination": "",
                "Query Date": request.get("query_date", ""),
                "Period": request.get("snapshot_date", ""),
                "ETD": _iso_date_or_none(first_leg.get("departure_date")) or "",
                "ETA": _iso_date_or_none(last_leg.get("arrival_date")) or "",
                "POD ETA": _iso_date_or_none(get_pod_eta(legs)) or "",
                "Transit Time": sched.get("transit_days", ""),
                "Transport Type": get_transport_type(legs),
                "TS Port(s)": get_ts_ports(legs),
                "Mother Vessel": first_leg.get("vessel_voyage", ""),
                "TS Vessel(s)": get_ts_vessels(legs),
                "Cut-Off Date": sched.get("cutoff_date", ""),
            }

            records.append(record)

    cols = [
        "Carrier",
        "Port of Loading",
        "Port of Discharge",
        "Last CY",
        "Final Destination",
        "Query Date",
        "Period",
        "ETD",
        "ETA",
        "POD ETA",
        "Transit Time",
        "Transport Type",
        "TS Port(s)",
        "Mother Vessel",
        "TS Vessel(s)",
        "Cut-Off Date",
    ]

    return pd.DataFrame(records, columns=cols)

# ==========================================================================
# Canonical-record builder (was utilsemccanonical.py)
# ==========================================================================



# === PORT OF DISCHARGE NORMALIZATION ===

PORT_NAMES = [
    "Charleston, SC",
    "Houston, TX",
    "Los Angeles, CA",
    "Miami, FL",
    "New Orleans, LA",
    "New York, NY",
    "Norfolk, VA",
    "Oakland, CA",
    "Savannah, GA",
    "Tampa, FL",
    "Jacksonville, FL",
    "Mobile, AL",
    "Tacoma, WA",
    "Philadelphia, PA",
    "Boston, MA",
    "Long Beach, CA",
    "Seattle, WA",
    "Prince Rupert, BC",
    "Vancouver, BC",
]

_PORT_LOOKUP = {p.split(",", 1)[0].strip().lower(): p for p in PORT_NAMES}


def normalize_pod(pod):
    """
    Map a US port-of-discharge to its canonical 'City, ST' form
    (case-insensitive). Handles bare city ('Charleston') and 'City, ST'
    inputs in any case ('BOSTON, MA'). Returns the original value
    unchanged if no entry matches.
    """
    if not pod:
        return pod
    key = str(pod).split(",", 1)[0].strip().lower()
    return _PORT_LOOKUP.get(key, pod)


# === DATE PARSING ===

_MONTH_ABBR = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _iso_date_or_none(value):
    """
    Return 'YYYY-MM-DD' or None. Handles every EMC date string observed:
      - ISO 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM...'
      - 'FEB-10-2026'             (leg departure_date / arrival_date)
      - 'FEB-07-2026 12:00'       (schedule cutoff_date)
      - 'MM/DD/YYYY'              (defensive)
      - '17-FEB-2026'             (defensive — DD-MMM-YYYY)
      - 'Tuesday, 17-FEB-2026'    (defensive)
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

    # Drop trailing time after comma
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

    # Hyphenated 3-part date: try both DD-MMM-YYYY and MMM-DD-YYYY
    parts = s.split("-")
    if len(parts) == 3:
        a, b, c = parts
        # DD-MMM-YYYY (CMA style)
        if a.isdigit() and c.isdigit() and len(c) == 4:
            mon_num = _MONTH_ABBR.get(b.upper())
            if mon_num:
                return f"{c}-{mon_num}-{int(a):02d}"
        # MMM-DD-YYYY (EMC style)
        if b.isdigit() and c.isdigit() and len(c) == 4:
            mon_num = _MONTH_ABBR.get(a.upper())
            if mon_num:
                return f"{c}-{mon_num}-{int(b):02d}"

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


# === EMC-SPECIFIC LEG NORMALIZATION ===

# Service-code values that are NOT real ocean movements. WAITING rows are
# layovers (from==to, no vessel) and get dropped entirely; Intermodal rows
# are ground movements and are kept with is_ocean=False so the summary
# helper can hide leading pre-ocean ones and keep mid/post-ocean ones.
_EMC_SKIP_SERVICES = {"WAITING"}
_EMC_GROUND_SERVICES = {"Intermodal"}


def _emc_leg_label(leg):
    """Vessel/voyage for ocean legs; service name for ground legs."""
    service = (leg.get("service") or "").strip()
    if service in _EMC_GROUND_SERVICES:
        return service.upper()  # "INTERMODAL"
    vessel = (leg.get("vessel_voyage") or "").strip()
    # EMC fills missing vessels with "----"; treat that as empty.
    if vessel and vessel.replace("-", "") == "":
        vessel = ""
    return vessel or service.upper()


def _emc_legs(schedule):
    """
    Normalize an EMC schedule's legs into:
      [{pol, pod, label, is_ocean, etd, eta}, ...]

    WAITING rows are filtered out (layovers, not real movements). Intermodal
    rows are kept with is_ocean=False; everything else (named service codes)
    is treated as ocean.
    """
    out = []
    for leg in schedule.get("legs") or []:
        service = (leg.get("service") or "").strip()
        if service in _EMC_SKIP_SERVICES:
            continue
        is_ground = service in _EMC_GROUND_SERVICES
        out.append({
            "pol": leg.get("from"),
            "pod": leg.get("to"),
            "label": _emc_leg_label(leg),
            "is_ocean": not is_ground,
            "etd": leg.get("departure_date"),
            "eta": leg.get("arrival_date"),
        })
    return out


def _emc_legs_summary(legs):
    """
    Returns (transport_type, mother_vessel, ts_ports, ts_vessels,
             route_ports, vessel_sequence).

    Visible chain is ocean-only: every non-ocean leg (pre-ocean Intermodal,
    mid-route ground transfers like the Panama land-bridge, post-ocean
    Intermodal delivery) is dropped from route_ports and vessel_sequence.
    Post-ocean inland is implicitly signaled downstream by
    `port_of_discharge != last_cy`.

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


# === CANONICAL RECORD BUILDER ===

def build_canonical_record(file_path):
    """
    Read an EMC post-HTML-transform JSON and return ONE canonical record per
    query with schedules nested. Returns None if no usable schedules.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]
    if not items:
        return None
    item = items[0]  # EMC wraps one query per file

    request = item.get("request") or {}
    pol = request.get("POL")
    last_cy = request.get("LastCY")
    final_destination = request.get("FinalDestination")  # not currently set by EMC scraper
    query_date = request.get("query_date")
    snapshot_date = request.get("snapshot_date")

    schedules = []
    for schedule in item.get("schedules") or []:
        legs = _emc_legs(schedule)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = _emc_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        etd_iso = _iso_date_or_none(legs[0]["etd"])
        eta_iso = _iso_date_or_none(legs[-1]["eta"])

        schedules.append({
            "id": _schedule_uuid("EMC", pol, pod, etd_iso, mother_vessel, schedule.get("schedule_id")),
            "port_of_discharge": pod,
            "cutoff_date": _iso_date_or_none(schedule.get("cutoff_date")),
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": _to_int_or_none(schedule.get("transit_days")),
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
        "id": _query_uuid("EMC", pol, last_cy, query_date),
        "carrier": {"code": "EMC", "name": "Evergreen"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
