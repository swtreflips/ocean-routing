from pathlib import Path
from datetime import datetime
import calendar
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import json
import hashlib

CARRIER_DIR = Path(__file__).resolve().parent

# === LOAD DATA ===
with open(CARRIER_DIR / "assets" / "msk_cities.json", "r", encoding="utf-8") as f:
    msk_cities = json.load(f)

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


# === HELPERS ===
def get_locations(name):
    """
    Return a list of dicts like:
    [{"name": "MIAMI, FL", "code": "USMIA"}, {"name": "FORT LAUDERDALE, FL", "code": "USFLL"}]
    Handles single or multiple entries.
    """
    entry = msk_cities.get(name)
    if isinstance(entry, list):
        return [
            {"name": e.get("businessLocationName"), "code": e.get("businessLocode")}
            for e in entry if e.get("businessLocode")
        ]
    elif isinstance(entry, dict):
        return [{"name": entry.get("businessLocationName"), "code": entry.get("businessLocode")}]
    return []


# === CANONICAL TRANSFORMATION HELPERS ===

def _iso_date_or_none(value):
    """Accept None, ISO date, or ISO datetime ('YYYY-MM-DDTHH:MM:SS' or with space). Return ISO date or None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    date_part = s.split("T", 1)[0].split(" ", 1)[0]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except ValueError:
        return None


def _to_int_or_none(value):
    """int(float(value)) on success, None on None / non-numeric."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


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
    "Kansas City, KS",
    "Kansas City, MO"
]

_PORT_LOOKUP = {p.lower(): p for p in PORT_NAMES}
_PORT_LOOKUP.update({p.split(",", 1)[0].strip().lower(): p for p in PORT_NAMES})


def normalize_pod(pod):
    """
    Normalize a port name to 'City Title Case, ST'.

    1. Lookup against PORT_NAMES (case-insensitive; accepts bare 'city' or
       full 'city, st').
    2. Fallback: if the value has a 2-letter state suffix, title-case the
       city and uppercase the state code.
    3. Otherwise title-case the whole value.

    Empty / None passes through unchanged.
    """
    if not pod:
        return pod
    raw = str(pod).strip()
    hit = _PORT_LOOKUP.get(raw.lower())
    if hit:
        return hit
    if "," in raw:
        city, _, state = raw.rpartition(",")
        state = state.strip().upper()
        if len(state) == 2 and state.isalpha():
            return f"{city.strip().title()}, {state}"
    return raw.title()


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


def _msk_iso_duration_to_days(iso_str):
    """Parse 'P60DT14H' -> 60. Matches existing behavior (drops sub-day fraction)."""
    if not iso_str or not isinstance(iso_str, str):
        return None
    if not iso_str.startswith("P") or "D" not in iso_str:
        return None
    try:
        return int(iso_str.split("D", 1)[0].replace("P", ""))
    except ValueError:
        return None


# Map Maersk carriageType to short label for non-ocean legs
_MSK_GROUND_LABELS = {"ROAD": "TRUCK", "RAIL": "RAIL"}
# Modes treated as ocean (water-borne). MSK uses bare "OCEAN"; reserved for future
# values like "BARGE" or "FEEDER" that should also count as ocean.
_MSK_OCEAN_TYPES = {"OCEAN", "BARGE", "FEEDER", "WATER"}


def _msk_facility_name(loc_block):
    """Pull a clean place name (e.g. 'Bangkok') from a vesselPortCall/location/railSchedule block."""
    if not loc_block:
        return None
    facility = (loc_block.get("location") or {}).get("facility") or loc_block.get("facility") or {}
    alts = facility.get("alternativeCodes") or []
    if alts:
        code = alts[0].get("alternativeCode")
        if code:
            return code
    # fallbacks
    return facility.get("name") or facility.get("facilityCode")


def _msk_leg_endpoints(carriage):
    """Return ((pol_block, pod_block), (etd_field, eta_field)) by carriageType."""
    ct = (carriage.get("carriageType") or "").upper()
    if ct in _MSK_OCEAN_TYPES:
        return carriage.get("vesselPortCallStart"), carriage.get("vesselPortCallEnd")
    if ct == "RAIL":
        return carriage.get("railScheduleLocationStart"), carriage.get("railScheduleLocationEnd")
    # ROAD and any other ground mode
    return carriage.get("locationStart"), carriage.get("locationEnd")


def _msk_leg_label(carriage):
    """Vessel/voyage for OCEAN-like legs; mode label (TRUCK/RAIL) for ground."""
    ct = (carriage.get("carriageType") or "").upper()
    if ct in _MSK_OCEAN_TYPES:
        vessel = (carriage.get("vessel") or {}).get("vesselName") or ""
        # voyage number: prefer departure (start), fall back to arrival (end)
        start = carriage.get("vesselPortCallStart") or {}
        end   = carriage.get("vesselPortCallEnd") or {}
        voyage = start.get("departureVoyageNumber") or end.get("arrivalVoyageNumber") or ""
        label = f"{vessel}/{voyage}".strip(" /")
        return label or ct
    return _MSK_GROUND_LABELS.get(ct, ct or "")


def _msk_legs(routing):
    """
    Normalize an MSK routing's legs into:
      [{pol, pod, label, is_ocean, etd, eta}, ...]
    """
    out = []
    for leg in routing.get("routingLegs") or []:
        carriage = leg.get("carriage") or {}
        ct = (carriage.get("carriageType") or "").upper()
        start, end = _msk_leg_endpoints(carriage)
        out.append({
            "pol": _msk_facility_name(start),
            "pod": _msk_facility_name(end),
            "label": _msk_leg_label(carriage),
            "is_ocean": ct in _MSK_OCEAN_TYPES,
            "etd": (start or {}).get("estimatedTimeOfDeparture"),
            "eta": (end or {}).get("estimatedTimeOfArrival"),
        })
    return out


def _msk_legs_summary(legs):
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


def build_schedule_rows(file_path):
    """
    Read a wrapped MSK JSON and return flat CSV rows (one per routing).
    ETD = first leg's start time (matches the user's 'when carrier first received it' rule).
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]
    rows = []
    for item in items:
        wrap = {
            "Carrier": "MSK",
            "Port of Loading": item.get("PortOfLoading", ""),
            "Last CY": item.get("LastCY", ""),
            "Final Destination": item.get("FinalDestination", ""),
            "Query Date": item.get("query_date", ""),
            "Period": item.get("snapshot_date", ""),
        }

        for routing in item.get("routings", []) or []:
            legs = _msk_legs(routing)
            if not legs:
                continue
            transport_type, mother_vessel, ts_ports, ts_vessels, _, _ = _msk_legs_summary(legs)

            # POD = last OCEAN leg's pod (ocean discharge port)
            ocean = [lg for lg in legs if lg["is_ocean"]]
            pod = ocean[-1]["pod"] if ocean else legs[-1]["pod"]
            pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)

            # ETD = first LEG's etd (NOT first OCEAN's etd) per user rule
            etd = _iso_date_or_none(legs[0]["etd"])
            # ETA = last leg's eta (final delivery arrival)
            eta = _iso_date_or_none(legs[-1]["eta"])

            rows.append({
                **wrap,
                "Last CY": normalize_pod(wrap["Last CY"]),
                "Port of Discharge": normalize_pod(pod) if pod else "",
                "ETD": etd or "",
                "ETA": eta or "",
                "POD ETA": pod_eta or "",
                "Transit Time": _msk_iso_duration_to_days(routing.get("estimatedTransitTime", "")),
                "Transport Type": transport_type,
                "TS Port(s)": "-".join(p or "" for p in ts_ports),
                "Mother Vessel": mother_vessel or "",
                "TS Vessel(s)": "-".join(v or "" for v in ts_vessels),
                "Cut-Off Date": "",
            })
    return rows


def build_canonical_record(file_path):
    """
    Read a wrapped MSK JSON and return ONE canonical record per query with
    routings (schedules) nested. Returns None if no usable routings.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]
    if not items:
        return None
    item = items[0]  # MSK wraps one query per file

    pol = item.get("PortOfLoading")
    last_cy = item.get("LastCY")
    final_destination = item.get("FinalDestination")
    query_date = item.get("query_date")
    snapshot_date = item.get("snapshot_date")

    schedules = []
    for routing in item.get("routings", []) or []:
        legs = _msk_legs(routing)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = _msk_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        etd_iso = _iso_date_or_none(legs[0]["etd"])
        eta_iso = _iso_date_or_none(legs[-1]["eta"])

        schedules.append({
            "id": _schedule_uuid("MSK", pol, pod, etd_iso, mother_vessel, routing.get("routingIdentifier")),
            "port_of_discharge": pod,
            "cutoff_date": None,
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": _msk_iso_duration_to_days(routing.get("estimatedTransitTime")),
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
        "id": _query_uuid("MSK", pol, last_cy, query_date),
        "carrier": {"code": "MSK", "name": "Maersk"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": normalize_pod(last_cy),
        "final_destination": final_destination,
        "schedules": schedules,
    }
