from pathlib import Path
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import json
import hashlib
from collections import defaultdict
from datetime import datetime

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


def _iso_date_or_none(value):
    """Accept None, 'YYYY-MM-DD', or 'YYYY-MM-DD HH:MM[:SS]'. Return ISO date string or None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    date_part = s.split(" ", 1)[0]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except ValueError:
        return None


def _to_int_or_none(value):
    """int(float(value)) on success, None on None / non-numeric. Truncates fractions."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _schedule_uuid(carrier, pol, pod, etd_iso, mother_vessel, raw_schedule_id):
    """Deterministic 16-char hex id. Stable across reruns → Supabase upsert-safe."""
    fields = [carrier, pol, pod, etd_iso, mother_vessel, raw_schedule_id]
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _query_uuid(carrier, pol, last_cy, query_date):
    """Deterministic 16-char hex id for a whole query (top-level canonical id)."""
    fields = [carrier, pol, last_cy, query_date]
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# Canonical "City, ST" forms for the US ports COSCO discharges at. The carrier
# returns bare city names (e.g. "Charleston") — we normalize Port of Discharge
# only (other fields are left as the carrier reports them).
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
    "Vancouver, BC"
]

# bare-city (lowercased) -> "City, ST"
_PORT_LOOKUP = {p.split(",", 1)[0].strip().lower(): p for p in PORT_NAMES}


def normalize_pod(pod):
    """
    Map a bare US port-of-discharge name to its 'City, ST' form (case-insensitive).
    Returns the original value unchanged if no entry matches. Applied only to
    `Port of Discharge` / `port_of_discharge` — TS ports and route_ports keep the
    raw carrier values per the agreed scope.
    """
    if not pod:
        return pod
    key = str(pod).strip().lower()
    return _PORT_LOOKUP.get(key, pod)


def assign_ids_inplace(file_path):
    """
    Opens a JSON file, assigns IDs forward across legs,
    and saves back to the same file (in place).
    """
    with open(file_path, "r") as f:
        data = json.load(f)

    current_id = None
    for leg in data["schedules"]:
        if leg["id"] is not None:
            current_id = leg["id"]
        else:
            leg["id"] = current_id

    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"✅ IDs assigned in {file_path}")


def _group_schedules(data):
    """Return list of (schedule_id, legs-sorted-by-legSequence). Skips None-id orphans."""
    grouped = defaultdict(list)
    for leg in data["schedules"]:
        grouped[leg["id"]].append(leg)

    out = []
    for schedule_id, legs in grouped.items():
        if schedule_id is None:
            print(f"⚠️ Skipping {len(legs)} orphan leg(s) with id=None")
            continue
        out.append((schedule_id, sorted(legs, key=lambda x: x["legSequence"])))
    return out


def build_schedule_rows(file_path):
    """
    Reads a JSON file (with IDs already assigned) and
    returns a list of rows (dicts) for the flat CSV.
    """
    with open(file_path, "r") as f:
        data = json.load(f)

    rows = []
    for schedule_id, legs in _group_schedules(data):
        max_leg = legs[-1]
        first_leg = legs[0]
        max_seq = max_leg["legSequence"]

        transport_type = "Direct" if max_seq == 1 else f"{max_seq-1} TS"
        ts_ports = " - ".join([leg["pod"] for leg in legs[:-1]]) if max_seq > 1 else ""
        ts_vessels = " - ".join([leg["vessel"] for leg in legs[1:]]) if max_seq > 1 else ""

        rows.append({
            "Carrier": "COS",
            "Port of Loading": data["PortofLoading"],
            "Port of Discharge": normalize_pod(max_leg["pod"]),
            "Last CY": data["LastCY"],
            "Final Destination": data["FinalDestination"],
            "Query Date": data["query_date"],
            "Period": data["snapshot_date"],
            "ETD": first_leg["etd"],
            "ETA": max_leg["available"],
            "POD ETA": max_leg["eta"],
            "Transit Time": first_leg["transitTime"],
            "Transport Type": transport_type,
            "TS Port(s)": ts_ports,
            "Mother Vessel": first_leg["vessel"],
            "TS Vessel": ts_vessels,
            "Cut-Off Date": first_leg["cutOff"]
        })
    return rows


def build_canonical_record(file_path):
    """
    Reads a JSON file (with IDs already assigned) and returns ONE canonical
    record per raw query, with schedules nested under `schedules`. Returns
    None if the query has no usable schedules.
    """
    with open(file_path, "r") as f:
        data = json.load(f)

    pol = data.get("PortofLoading")
    last_cy = data.get("LastCY")
    final_destination = data.get("FinalDestination")
    query_date = data.get("query_date")
    snapshot_date = data.get("snapshot_date")

    schedules = []
    for schedule_id, legs in _group_schedules(data):
        first_leg = legs[0]
        max_leg = legs[-1]
        max_seq = max_leg["legSequence"]

        etd_iso = _iso_date_or_none(first_leg.get("etd"))
        mother_vessel = first_leg.get("vessel")
        pod = normalize_pod(max_leg.get("pod"))

        schedules.append({
            "id": _schedule_uuid("COS", pol, pod, etd_iso, mother_vessel, schedule_id),
            "port_of_discharge": pod,
            "cutoff_date": _iso_date_or_none(first_leg.get("cutOff")),
            "etd": etd_iso,
            "eta": _iso_date_or_none(max_leg.get("available")),
            "pod_eta": _iso_date_or_none(max_leg.get("eta")),
            "transit_time_days": _to_int_or_none(first_leg.get("transitTime")),
            "transport_type": "Direct" if max_seq == 1 else f"{max_seq-1} TS",
            "mother_vessel": mother_vessel,
            "ts_ports": [leg.get("pod") for leg in legs[:-1]],
            "ts_vessels": [leg.get("vessel") for leg in legs[1:]],
            "route_ports": [legs[0].get("pol")] + [leg.get("pod") for leg in legs],
            "vessel_sequence": [leg.get("vessel") for leg in legs],
        })

    if not schedules:
        return None

    return {
        "schema_version": 1,
        "id": _query_uuid("COS", pol, last_cy, query_date),
        "carrier": {"code": "COS", "name": "COSCO Shipping Lines"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
