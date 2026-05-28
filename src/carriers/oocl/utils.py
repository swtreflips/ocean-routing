from pathlib import Path
from datetime import datetime
import calendar
import os
import re
import pandas as pd
import geopandas as gpd
from geopy.geocoders import Nominatim
import json
import hashlib

# === LOAD DATA ===
CARRIER_DIR = Path(__file__).resolve().parent

# OOCL port-name -> locationid lookup (from mapping.json)
MAPPING_PATH = CARRIER_DIR / "assets" / "mapping.json"
_oocl_mapping = {}
_oocl_code_lookup = {}
try:
    with open(MAPPING_PATH, "r", encoding="utf-8") as _f:
        _oocl_mapping = json.load(_f)
    for _port_name, _entries in _oocl_mapping.items():
        if _entries and "locationid" in _entries[0]:
            _oocl_code_lookup[_port_name.strip().lower()] = _entries[0]["locationid"]
except FileNotFoundError:
    pass


def get_oocl_code(city_name):
    """Return OOCL locationid for a city name (case-insensitive), or None."""
    if not city_name:
        return None
    return _oocl_code_lookup.get(city_name.strip().lower())

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


# === Port-of-Discharge / Last CY normalization ===
# Carriers return POD in inconsistent shapes ("Charleston" / "CHARLESTON" /
# "Charleston, SC"); collapse to canonical "City, ST" so the
# port_of_discharge != last_cy inland-delivery heuristic stays stable.

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

_PORT_LOOKUP = {p.lower(): p for p in PORT_NAMES}
_PORT_LOOKUP.update({p.split(",", 1)[0].strip().lower(): p for p in PORT_NAMES})


def normalize_pod(pod):
    """
    Normalize a port-of-discharge to 'City Title Case, ST'.

    1. Lookup against PORT_NAMES (case-insensitive; accepts bare 'city'
       or full 'city, st').
    2. Fallback: if the value has a 2-letter state suffix, title-case
       the city and uppercase the state code (e.g. 'PORT EVERGLADES, FL'
       → 'Port Everglades, FL').
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


# === OOCL schedule parsing ===
# Schedule type is driven by count of `Voyage` legs; truck/rail short legs are ignored.

COLUMNS = [
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


def reformat_date(value):
    """Normalize 'YYYYMMDDHHMMSS.fff' or 'YYYY-MM-DD' to 'MM/DD/YYYY'."""
    if not value or not isinstance(value, str):
        return ""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
    if m:
        yyyy, mm, dd = m.groups()
        return f"{mm}/{dd}/{yyyy}"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return ""


def _date_str(date_obj):
    if isinstance(date_obj, dict):
        s = date_obj.get("dateStr")
        if isinstance(s, str):
            return s
    return ""


def _transit_days(minutes):
    try:
        return f"{float(minutes) / 1440:.1f}"
    except (TypeError, ValueError):
        return ""


def _transport_type(voyage_count):
    if voyage_count <= 1:
        return "Direct"
    return f"{voyage_count - 1} TS"


def parse_schedule(schedule):
    """Parse a single OOCL schedule entry. Dates returned raw; caller applies reformat_date."""
    voyages = [leg for leg in schedule.get("Legs", []) if leg.get("Type") == "Voyage"]
    if not voyages:
        return {}

    first, last = voyages[0], voyages[-1]

    if len(voyages) == 1:
        ts_ports = ""
        ts_vessels = ""
    else:
        ts_ports = " - ".join(v.get("DischargePort", {}).get("Name", "") for v in voyages[:-1])
        ts_vessels = " - ".join(v.get("VesselName", "") for v in voyages[1:])

    return {
        "Port of Discharge": last.get("DischargePort", {}).get("Name", ""),
        "ETD": _date_str(first.get("FromETDLocalDateTime")),
        "ETA": _date_str(schedule.get("CargoAvailabilityLocalDateTime")),
        "POD ETA": _date_str(last.get("ToETALocalDateTime")),
        "Transit Time": _transit_days(schedule.get("TransitTimeInMinute")),
        "Transport Type": _transport_type(len(voyages)),
        "TS Port(s)": ts_ports,
        "Mother Vessel": first.get("VesselName", ""),
        "TS Vessel(s)": ts_vessels,
        "Cut-Off Date": _date_str(schedule.get("CargoCutoffLocalDateTime")),
    }


def _load_json(path):
    # OOCL JSON contains bare NaN (e.g. "OFQ": NaN) which is invalid JSON — coerce to null.
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"\bNaN\b", "null", text)
    return json.loads(text)


def build_schedule_dataframe(processing_dir):
    """Reads all .json files from processing_dir, extracts schedule data using parse_schedule logic,
    and returns a combined DataFrame."""
    records = []

    for file in os.listdir(processing_dir):
        if not file.endswith(".json"):
            continue

        full_path = os.path.join(processing_dir, file)
        data = _load_json(full_path)

        carrier = "OOCL"
        port_of_loading = data.get("PortOfLoading")
        last_cy = data.get("LastCY")
        final_destination = data.get("FinalDestination")

        query_date = reformat_date(data.get("query_date"))
        snapshot_date_fmt = reformat_date(data.get("snapshot_date"))

        for schedule in data.get("schedules", []):
            parsed = parse_schedule(schedule)
            if not parsed:
                continue

            record = {
                "Carrier": carrier,
                "Port of Loading": port_of_loading,
                "Port of Discharge": parsed.get("Port of Discharge", ""),
                "Last CY": last_cy,
                "Final Destination": final_destination,
                "Query Date": query_date,
                "Period": snapshot_date_fmt,
                "ETD": reformat_date(parsed.get("ETD")),
                "ETA": reformat_date(parsed.get("ETA")),
                "POD ETA": reformat_date(parsed.get("POD ETA")),
                "Transit Time": parsed.get("Transit Time"),
                "Transport Type": parsed.get("Transport Type", ""),
                "TS Port(s)": parsed.get("TS Port(s)", ""),
                "Mother Vessel": parsed.get("Mother Vessel", ""),
                "TS Vessel(s)": parsed.get("TS Vessel(s)", ""),
                "Cut-Off Date": reformat_date(parsed.get("Cut-Off Date")),
            }
            records.append(record)

    return pd.DataFrame(records, columns=COLUMNS)


# === CANONICAL TRANSFORMATION HELPERS ===

def _oocl_date_iso(value):
    """OOCL dates can be ISO ('YYYY-MM-DD'), 'YYYYMMDDHHMMSS.fff', or wrapped {'dateStr': ...}.
    Return 'YYYY-MM-DD' or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("dateStr")
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
    if m:
        y, mo, d = m.groups()
        try:
            datetime(int(y), int(mo), int(d))
            return f"{y}-{mo}-{d}"
        except ValueError:
            return None
    date_part = value.split("T", 1)[0].split(" ", 1)[0]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except ValueError:
        return None


def _to_int_or_none(value):
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _schedule_uuid(carrier, pol, pod, etd_iso, mother_vessel, raw_schedule_id):
    fields = [carrier, pol, pod, etd_iso, mother_vessel, raw_schedule_id]
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _query_uuid(carrier, pol, last_cy, query_date):
    fields = [carrier, pol, last_cy, query_date]
    key = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# OOCL ocean leg detection. A leg is water-borne if:
#   - Type == "Voyage" (regular ocean carrier voyage), OR
#   - Type is a Door (Outbound/Inbound) AND TransportMode.Code == "BAR" (barge feeder)
def _oocl_is_ocean(leg):
    t = leg.get("Type")
    if t == "Voyage":
        return True
    if t in ("OutboundDoor", "InboundDoor"):
        tm = leg.get("TransportMode") or {}
        if isinstance(tm, dict) and (tm.get("Code") or "").upper() == "BAR":
            return True
    return False


def _oocl_leg_label(leg):
    """Vessel name for Voyage; mode label (BARGE/TRUCK/RAIL) for others."""
    t = leg.get("Type")
    if t == "Voyage":
        v = (leg.get("VesselName") or "").strip()
        # add voyage code if available
        svvd = (leg.get("LoadingSvvd") or "").strip()
        return f"{v}/{svvd}" if v and svvd else (v or "VESSEL")
    tm = leg.get("TransportMode") or {}
    if isinstance(tm, dict):
        code = (tm.get("Code") or "").upper()
        if code == "BAR":
            return "BARGE"
        if code == "TRU":
            return "TRUCK"
        if code in ("RAI", "RAL"):
            return "RAIL"
    # Intermodal / Door without TransportMode -> infer from Type
    if "Intermodal" in (t or ""):
        return "RAIL"
    if "Door" in (t or ""):
        return "TRUCK"
    return (t or "GROUND").upper()


def _oocl_facility_name(d):
    if isinstance(d, dict):
        return d.get("Name")
    return None


def _oocl_leg_pol(leg):
    if leg.get("Type") == "Voyage":
        return _oocl_facility_name(leg.get("LoadingPort")) or _oocl_facility_name(leg.get("OriginFacility"))
    return _oocl_facility_name(leg.get("OriginFacility"))


def _oocl_leg_pod(leg):
    if leg.get("Type") == "Voyage":
        return _oocl_facility_name(leg.get("DischargePort")) or _oocl_facility_name(leg.get("DestinationFacility"))
    return _oocl_facility_name(leg.get("DestinationFacility"))


def _oocl_normalize_legs(schedule):
    """Return [{pol, pod, label, is_ocean, etd, eta}, ...] for a schedule.

    Voyage legs carry FromETDLocalDateTime/ToETALocalDateTime; non-Voyage legs
    (Door, Intermodal — including BARGE OutboundDoor) don't, so fall back to
    CargoCutoffLocalDateTime / CargoAvailabilityLocalDateTime.
    """
    out = []
    for leg in schedule.get("Legs", []) or []:
        etd = (_oocl_date_iso(leg.get("FromETDLocalDateTime"))
               or _oocl_date_iso(leg.get("CargoCutoffLocalDateTime")))
        eta = (_oocl_date_iso(leg.get("ToETALocalDateTime"))
               or _oocl_date_iso(leg.get("CargoAvailabilityLocalDateTime")))
        out.append({
            "pol": _oocl_leg_pol(leg),
            "pod": _oocl_leg_pod(leg),
            "label": _oocl_leg_label(leg),
            "is_ocean": _oocl_is_ocean(leg),
            "etd": etd,
            "eta": eta,
        })
    return out


def _oocl_legs_summary(legs):
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
    """Read a wrapped OOCL JSON and return flat CSV rows (one per schedule)."""
    data = _load_json(file_path)

    wrap = {
        "Carrier": "OOCL",
        "Port of Loading": data.get("PortOfLoading"),
        "Last CY": data.get("LastCY"),
        "Final Destination": data.get("FinalDestination"),
        "Query Date": data.get("query_date"),
        "Period": data.get("snapshot_date"),
    }

    rows = []
    for sched in data.get("schedules", []) or []:
        legs = _oocl_normalize_legs(sched)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, _, _ = _oocl_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = ocean[-1]["eta"] if ocean else None
        # ETD = first VOYAGE leg's ETD (OOCL non-Voyage legs don't expose a clean ETD timestamp)
        first_voyage = next((lg for lg in legs if lg["is_ocean"]), None)
        etd = first_voyage["etd"] if first_voyage else None
        eta = _oocl_date_iso(sched.get("CargoAvailabilityLocalDateTime"))
        cutoff = _oocl_date_iso(sched.get("CargoCutoffLocalDateTime"))

        tmin = sched.get("TransitTimeInMinute")
        try:
            transit_days = int(round(float(tmin) / 1440)) if tmin not in (None, "") else None
        except (ValueError, TypeError):
            transit_days = None

        rows.append({
            **wrap,
            "Port of Discharge": pod or "",
            "ETD": etd or "",
            "ETA": eta or "",
            "POD ETA": pod_eta or "",
            "Transit Time": transit_days,
            "Transport Type": transport_type,
            "TS Port(s)": " - ".join(p or "" for p in ts_ports),
            "Mother Vessel": mother_vessel or "",
            "TS Vessel(s)": " - ".join(v or "" for v in ts_vessels),
            "Cut-Off Date": cutoff or "",
        })
    return rows


def build_canonical_record(file_path):
    """Read a wrapped OOCL JSON and return ONE canonical record per query."""
    data = _load_json(file_path)

    pol = data.get("PortOfLoading")
    last_cy = data.get("LastCY")
    final_destination = data.get("FinalDestination")
    query_date = data.get("query_date")
    snapshot_date = data.get("snapshot_date")

    schedules = []
    for sched in data.get("schedules", []) or []:
        legs = _oocl_normalize_legs(sched)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = _oocl_legs_summary(legs)

        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        pod_eta = ocean[-1]["eta"] if ocean else None
        first_voyage = next((lg for lg in legs if lg["is_ocean"]), None)
        etd_iso = first_voyage["etd"] if first_voyage else None
        eta_iso = _oocl_date_iso(sched.get("CargoAvailabilityLocalDateTime"))
        cutoff_iso = _oocl_date_iso(sched.get("CargoCutoffLocalDateTime"))

        tmin = sched.get("TransitTimeInMinute")
        try:
            transit_days = int(round(float(tmin) / 1440)) if tmin not in (None, "") else None
        except (ValueError, TypeError):
            transit_days = None

        schedules.append({
            "id": _schedule_uuid("OOCL", pol, pod, etd_iso, mother_vessel, sched.get("RouteId")),
            "port_of_discharge": pod,
            "cutoff_date": cutoff_iso,
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta,
            "transit_time_days": transit_days,
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
        "id": _query_uuid("OOCL", pol, last_cy, query_date),
        "carrier": {"code": "OOCL", "name": "Orient Overseas Container Line"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
