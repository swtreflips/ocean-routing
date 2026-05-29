from pathlib import Path
from datetime import datetime
import calendar
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import json
import hashlib
import re

# === PORT OF DISCHARGE NORMALIZATION (global, delimiter-agnostic — see NORM.md) ===
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

# Index by full 'city, st' AND bare 'city' (both lowercased) so a lookup hits
# whatever shape the carrier hands us.
_PORT_LOOKUP = {p.lower(): p for p in PORT_NAMES}
_PORT_LOOKUP.update({p.split(",", 1)[0].strip().lower(): p for p in PORT_NAMES})

# Trailing 2-letter region code in any delimiter shape:
#   'City, ST'  |  'City (ST)'  |  'City [ST]'
_POD_STATE_RE = re.compile(
    r"^\s*(?P<city>.+?)\s*[,(\[]\s*(?P<state>[A-Za-z]{2})\s*[)\]]?\s*$"
)


def normalize_pod(pod):
    """
    Normalize a port-of-discharge to 'City Title Case, ST' from a single rule
    set, regardless of the delimiter the carrier uses for the region code.

        'Charleston'        -> 'Charleston, SC'    (bare-city lookup)
        'CHARLESTON, SC'    -> 'Charleston, SC'    (comma)
        'Los Angeles (CA)'  -> 'Los Angeles, CA'   (parenthesis, ZIM)
        'Wilmington, NC'    -> 'Wilmington, NC'    (region present, not in list)
        'Bangkok'           -> 'Bangkok'           (foreign / no region code)

    Empty / None passes through unchanged.
    """
    if not pod:
        return pod
    raw = str(pod).strip()

    # 1) Exact lookup of the value as-is (bare 'city' or full 'city, st').
    hit = _PORT_LOOKUP.get(raw.lower())
    if hit:
        return hit

    # 2) Dynamically detect a trailing region code in any delimiter shape,
    #    then re-lookup the cleaned 'city, st' to lock the canonical spelling.
    m = _POD_STATE_RE.match(raw)
    if m:
        city = m.group("city").strip()
        state = m.group("state").upper()
        canonical = (_PORT_LOOKUP.get(f"{city.lower()}, {state.lower()}")
                     or _PORT_LOOKUP.get(city.lower()))
        if canonical:
            return canonical
        return f"{city.title()}, {state}"

    # 3) No region code: title-case (foreign ports, unknown bare cities).
    return raw.title()


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


# === CANONICAL TRANSFORMATION HELPERS ===

def _iso_date_or_none(value):
    """Accept None, ISO date, or ISO datetime ('YYYY-MM-DDTHH:MM:SS[+HH:MM]' or space-separated). Return ISO date or None."""
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


def _zim_is_ground(vessel_name):
    """ZIM marks ground/inland legs with vesselName='Land Trans'."""
    return (vessel_name or "").strip().lower() == "land trans"


def _zim_leg_label(vessel_name, voyage_no):
    """For ocean legs return 'Vessel/Voyage'; for ground legs return short 'TRUCK' label."""
    if _zim_is_ground(vessel_name):
        return "TRUCK"
    vn = (vessel_name or "").strip()
    voy = (voyage_no or "").strip()
    return f"{vn}/{voy}" if voy else vn


def _zim_legs(route):
    """
    Normalize a ZIM route's legs into:
      [{pol, pod, label, is_ocean, etd, eta, cutoff}, ...]
    """
    out = []
    for leg in route or []:
        vname = (leg.get("vesselName") or "").strip()
        out.append({
            "pol": leg.get("portDepartureName"),
            "pod": leg.get("portArrivalName"),
            "label": _zim_leg_label(vname, leg.get("voyageNumber")),
            "is_ocean": not _zim_is_ground(vname),
            "etd": leg.get("departureDate"),
            "eta": leg.get("arrivalDate"),
            "cutoff": leg.get("docClosingDate"),
            "voyage": (leg.get("voyageNumber") or "").strip() or None,
        })
    return out


def _zim_legs_summary(legs):
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


def _zim_first_ocean(legs):
    """First ocean leg of a normalized leg list (for mother metadata like cutoff)."""
    return next((lg for lg in legs if lg["is_ocean"]), None)


def build_schedule_rows(file_path):
    """
    Read a wrapped ZIM JSON and return flat CSV rows (one per route).
    Applies the ocean-only TS rule so 'Land Trans' inland feeders don't inflate TS counts.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    wrap = {
        "Carrier": "ZIM",
        "Port of Loading": data.get("PortOfLoading"),
        "Last CY": data.get("LastCY"),
        "Final Destination": data.get("FinalDestination"),
        "Query Date": data.get("query_date"),
        "Period": data.get("snapshot_date"),
    }

    rows = []
    routes = (data.get("schedules") or {}).get("routes") or []
    for route in routes:
        if not route:
            continue
        legs = _zim_legs(route)
        transport_type, mother_vessel, ts_ports, ts_vessels, _, _ = _zim_legs_summary(legs)

        first_ocean = _zim_first_ocean(legs)
        pod = legs[-1]["pod"]
        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        etd = _iso_date_or_none(legs[0]["etd"])
        eta = _iso_date_or_none(legs[-1]["eta"])
        cutoff = _iso_date_or_none(first_ocean["cutoff"] if first_ocean else None)

        # Final POD = last ocean leg's pod when available, else final leg's pod (ZIM
        # routes that end with a post-ocean Land Trans should still report the ocean POD)
        port_of_discharge = ocean[-1]["pod"] if ocean else pod

        rows.append({
            **wrap,
            "Port of Discharge": normalize_pod(port_of_discharge),
            "ETD": etd,
            "ETA": eta,
            "POD ETA": pod_eta,
            "Transit Time": _to_int_or_none(route[0].get("daysAtSea") if route else None),
            "Transport Type": transport_type,
            "TS Port(s)": " - ".join(p or "" for p in ts_ports),
            "Mother Vessel": mother_vessel or "",
            "TS Vessel(s)": " - ".join(v or "" for v in ts_vessels),
            "Cut-Off Date": cutoff or "",
        })
    return rows


def build_canonical_record(file_path):
    """
    Read a wrapped ZIM JSON and return ONE canonical record per query, with
    routes nested under `schedules`. Returns None if no usable routes.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    pol = data.get("PortOfLoading")
    last_cy = data.get("LastCY")
    final_destination = data.get("FinalDestination")
    query_date = data.get("query_date")
    snapshot_date = data.get("snapshot_date")

    schedules = []
    routes = (data.get("schedules") or {}).get("routes") or []
    for route in routes:
        if not route:
            continue
        legs = _zim_legs(route)
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = _zim_legs_summary(legs)

        first_ocean = _zim_first_ocean(legs)
        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = _iso_date_or_none(ocean[-1]["eta"] if ocean else None)
        etd_iso = _iso_date_or_none(legs[0]["etd"])
        eta_iso = _iso_date_or_none(legs[-1]["eta"])
        cutoff_iso = _iso_date_or_none(first_ocean["cutoff"] if first_ocean else None)
        raw_sched_id = first_ocean["voyage"] if first_ocean else None

        schedules.append({
            "id": _schedule_uuid("ZIM", pol, pod, etd_iso, mother_vessel, raw_sched_id),
            "port_of_discharge": pod,
            "cutoff_date": cutoff_iso,
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": _to_int_or_none(route[0].get("daysAtSea") if route else None),
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
        "id": _query_uuid("ZIM", pol, last_cy, query_date),
        "carrier": {"code": "ZIM", "name": "ZIM Integrated Shipping Services"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
