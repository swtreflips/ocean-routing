"""Helpers for the WHL (Wan Hai Lines) pipeline.

Mirrors the structure of utilscma.py / utilsemc.py:
  - Common helpers (progress, snapshot, geocoding, Voronoi) ported verbatim from utilscma.
  - WHL-specific Selenium scraping logic (origin/POD dropdown handling, pick_pod
    fallback via connections.json) ported from flow3.py.
  - HTML -> JSON parser ported from wanhai_html_to_json.py.
  - JSON -> DataFrame builder ported from wanhaiDataStructuring.py.
"""

import calendar
import hashlib
import json
import os
import re
import shutil
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim

import undetected_chromedriver as uc
uc.Chrome.__del__ = lambda self: None

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait


CARRIER = "Wan Hai"
WANHAI_URL = "https://www.wanhai.com/views/skd/SkdByPort.xhtml"
DETAIL_TABLE_CSS = "table[id^='DATA_TABLE']"


# ---------------------------------------------------------------------------
# Snapshot / date helpers (verbatim from utilscma.py)
# ---------------------------------------------------------------------------

def get_month_periods(year, month):
    """Return reference dates for a given month: start, mid, end."""
    start = datetime(year, month, 1)
    mid = datetime(year, month, 15)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day)
    return {"start": start, "mid": mid, "end": end}


def assign_snapshot(date_input):
    """Snap a date to the nearest reporting period (1st, 15th, or next month's 1st)."""
    date_input = datetime.strptime(date_input, "%Y-%m-%d")
    year, month = date_input.year, date_input.month
    periods = get_month_periods(year, month)

    if 1 <= date_input.day <= 5:
        return periods["start"].date()
    if abs((date_input - periods["mid"]).days) <= 5:
        return periods["mid"].date()
    if date_input.day >= 28:
        if month == 12:
            return datetime(year + 1, 1, 1).date()
        return datetime(year, month + 1, 1).date()
    return date_input.date()


# ---------------------------------------------------------------------------
# Progress / filename helpers (verbatim from utilscma.py)
# ---------------------------------------------------------------------------

def load_progress(quotes_file, progress_file):
    """Load existing progress CSV, or initialize a fresh one from quotes.csv."""
    if progress_file.exists():
        quotes_progress = pd.read_csv(progress_file)
    else:
        quotes_progress = pd.read_csv(quotes_file).copy()
        if "LastCY" not in quotes_progress.columns:
            quotes_progress["LastCY"] = None
        if "status" not in quotes_progress.columns:
            quotes_progress["status"] = "pending"
        if "result_file" not in quotes_progress.columns:
            quotes_progress["result_file"] = None
    return quotes_progress


def get_unique_path(base_path: Path) -> Path:
    """Avoid filename collisions by appending (1), (2)..."""
    if not base_path.exists():
        return base_path
    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def get_unique_filename(base_path: Path) -> Path:
    """Avoid filename collisions by appending A, B, C..."""
    if not base_path.exists():
        return base_path
    stem = base_path.stem
    suffix = base_path.suffix
    letter = ord("A")
    while True:
        new_file = base_path.with_name(f"{stem}{chr(letter)}{suffix}")
        if not new_file.exists():
            return new_file
        letter += 1


# ---------------------------------------------------------------------------
# Geocoding + Voronoi (verbatim from utilscma.py)
# ---------------------------------------------------------------------------

def geocode_city(city_name, geolocator):
    """Return lat/lon for a city name using Nominatim (USA scope)."""
    try:
        loc = geolocator.geocode(f"{city_name}, USA")
        if loc:
            return loc.latitude, loc.longitude
    except Exception as e:
        print(f"Geocoding failed for {city_name}: {e}")
    return None, None


def resolve_missing_locations(quotes_progress, locations, locations_file, geocode_fn, geolocator):
    """Ensure every Final Destination in quotes_progress has a row in locations; geocode the rest."""
    missing = set(quotes_progress["Final Destination"].unique()) - set(locations["Place of Discharge"].unique())

    for city in missing:
        lat, lon = geocode_fn(city, geolocator)
        if lat and lon:
            print(f"✅ Geocoded {city}: {lat}, {lon}")
            new_row = pd.DataFrame([{
                "Place of Discharge": city,
                "Latitude": lat,
                "Longitude": lon,
                "Type": "Geocoded",
            }])
            locations = pd.concat([locations, new_row], ignore_index=True)
        else:
            print(f"⚠️ Could not geocode {city}")

    locations.to_csv(locations_file, index=False)
    return pd.read_csv(locations_file)


def build_voronoi_lookup(quotes_progress, locations, gdf_voronoi):
    """Spatial-join Final Destinations against Voronoi polygons → {dest: CityYard}."""
    # Drop rows where Final Destination is empty — those rows are using the
    # "LastCY filled directly" workflow and don't need a Voronoi lookup. If
    # nothing is left, return an empty mapping so main.py's apply() falls
    # through to the existing LastCY for every row.
    unique_dest = quotes_progress[["Final Destination"]].dropna().drop_duplicates()
    if unique_dest.empty:
        return {}
    unique_dest = unique_dest.merge(
        locations,
        left_on="Final Destination",
        right_on="Place of Discharge",
        how="left",
    )

    gdf_unique = gpd.GeoDataFrame(
        unique_dest,
        geometry=gpd.points_from_xy(unique_dest["Longitude"], unique_dest["Latitude"]),
        crs="EPSG:4326",
    )

    if gdf_voronoi.crs and gdf_voronoi.crs != gdf_unique.crs:
        gdf_unique = gdf_unique.to_crs(gdf_voronoi.crs)

    dest_with_yard = gpd.sjoin(gdf_unique, gdf_voronoi, how="left", predicate="within")
    return dest_with_yard.set_index("Final Destination")["CityYard"].to_dict()


# ---------------------------------------------------------------------------
# WHL data file loaders
# ---------------------------------------------------------------------------

def load_wanhai_locations(path):
    """Load wanhai_locations.json: { city → [ {country, port, portcode, countrycode}, ... ] }."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_connections(path):
    """Load connections.json: { 'inlands': {...}, 'ports': {...} }."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Skip-silently signals — let main.py mark a row without producing a traceback
# ---------------------------------------------------------------------------

class WHLDestinationSkipped(Exception):
    """Destination resolved + dropdown loaded, but the LastCY (and any allowed
    fallbacks) isn't among the available options. Caller should set status
    'skipped_not_found' and move on quietly."""


class WHLDestinationUnmapped(Exception):
    """Destination can't be resolved (not in wanhai_locations) AND isn't in
    connections['ports'] for a country-derivation fallback. Caller should set
    status 'skipped_unmapped' and move on quietly."""


def _is_fallback_eligible_port(name, connections):
    """A LastCY is fallback-eligible only if it's listed in connections['ports'].
    Inland cities (connections['inlands']) and anything else are NOT eligible —
    they get tried direct-only and skip silently on miss."""
    return name in (connections.get("ports") or {})


def get_locations(city_name, wanhai_locations):
    """Compatibility wrapper: city → list of WHL location entries (or empty)."""
    return wanhai_locations.get(city_name, []) or []


# ---------------------------------------------------------------------------
# WHL name → (countrycode, portcode|None, port_name) resolution
# ---------------------------------------------------------------------------

def resolve(name, locations):
    """Resolve a place name to (countrycode, portcode|None, port_name).

    Returns: (resolved_tuple, None) on success, (None, reason) on failure.
    """
    entries = locations.get(name)
    if not entries:
        return None, "name not in wanhai_locations.json"
    for e in entries:
        if "portcode" in e:
            return (e["countrycode"], e["portcode"], e["port"]), None
    for e in entries:
        if "countrycode" in e and "port" in e:
            return (e["countrycode"], None, e["port"]), None
    return None, "no usable port/country info"


# ---------------------------------------------------------------------------
# Selenium dropdown helpers (from flow3.py)
# ---------------------------------------------------------------------------

def wait_for_option(driver, name, value, timeout=15):
    """Wait until <select name=...> has an <option value=value>."""
    WebDriverWait(driver, timeout, ignored_exceptions=[StaleElementReferenceException]).until(
        lambda d: any(
            o.get_attribute("value") == value
            for o in d.find_element(By.NAME, name).find_elements(By.TAG_NAME, "option")
        )
    )


def safe_select(driver, name, value, retries=8):
    """Select.by_value with stale-element retries."""
    for attempt in range(retries):
        try:
            Select(driver.find_element(By.NAME, name)).select_by_value(value)
            return
        except StaleElementReferenceException:
            if attempt == retries - 1:
                raise
            time.sleep(0.2)


def get_options(driver, name):
    """Snapshot <option> entries (skip placeholder). Returns [{'code', 'name'}, ...]."""
    for _ in range(8):
        try:
            el = driver.find_element(By.NAME, name)
            out = []
            for o in el.find_elements(By.TAG_NAME, "option"):
                val = (o.get_attribute("value") or "").strip()
                txt = (o.text or "").strip()
                if not val:
                    continue
                out.append({"code": val, "name": txt})
            return out
        except StaleElementReferenceException:
            time.sleep(0.2)
    raise StaleElementReferenceException("get_options never settled")


def wait_for_pod_populated(driver, timeout=15):
    """After to_nation is selected, pod is re-rendered. Wait until real options appear."""
    WebDriverWait(driver, timeout, ignored_exceptions=[StaleElementReferenceException]).until(
        lambda d: any(
            (o.get_attribute("value") or "").strip()
            for o in d.find_element(By.NAME, "pod").find_elements(By.TAG_NAME, "option")
        )
    )


# ---------------------------------------------------------------------------
# pick_pod — WHL particularity: connections.json fallback
# ---------------------------------------------------------------------------

def pick_pod(target_name, target_resolved, pod_options, connections):
    """Decide which POD option to select.

    Returns ('direct'|'fallback', used_name, code) or None if nothing matches.
    """
    target_upper = target_name.strip().upper()

    if target_resolved is not None:
        _, target_code, target_port = target_resolved
        if target_code:
            for o in pod_options:
                if o["code"] == target_code:
                    return ("direct", target_name, o["code"])
        for o in pod_options:
            if o["name"].upper() == (target_port or "").upper():
                return ("direct", target_name, o["code"])

    for o in pod_options:
        if o["name"].upper() == target_upper:
            return ("direct", target_name, o["code"])

    # Port → inland fallback. ONLY for LastCYs explicitly listed in
    # connections['ports'] (the 8 curated US ports). Inlands and anything else
    # are direct-only — never fall back to a port.
    if _is_fallback_eligible_port(target_name, connections):
        for inland in connections.get("ports", {}).get(target_name, []):
            inland_upper = inland.strip().upper()
            for o in pod_options:
                if o["name"].upper() == inland_upper:
                    return ("fallback", inland, o["code"])

    return None


# ---------------------------------------------------------------------------
# Single-row scrape orchestration
# ---------------------------------------------------------------------------

def open_search_and_set_origin(driver, wait, origin):
    """Load search page, select from_nation and pol. Returns the actual pol code."""
    from_nation, pol_code, pol_name = origin
    driver.get(WANHAI_URL)
    wait.until(EC.presence_of_element_located((By.NAME, "from_nation")))
    safe_select(driver, "from_nation", from_nation)
    if pol_code is not None:
        wait_for_option(driver, "pol", pol_code)
        safe_select(driver, "pol", pol_code)
        actual_pol = pol_code
    else:
        target = (pol_name or "").upper()
        WebDriverWait(driver, 15, ignored_exceptions=[StaleElementReferenceException]).until(
            lambda d: any(
                (o.text or "").strip().upper() == target
                for o in d.find_element(By.NAME, "pol").find_elements(By.TAG_NAME, "option")
            )
        )
        opts = get_options(driver, "pol")
        match = next((o for o in opts if o["name"].upper() == target), None)
        if not match:
            raise RuntimeError(f"pol option {pol_name!r} not found")
        safe_select(driver, "pol", match["code"])
        actual_pol = match["code"]
    time.sleep(0.5)
    return actual_pol


def finish_search(driver, wait, pod_code):
    """Submit form, trigger 'All Services', parse the DATA_TABLE.

    Returns (html_table_str, n_rows, actual_pod).
    """
    actual_pod = pod_code

    driver.find_element(By.NAME, "subtn").click()
    wait.until(EC.url_contains("SkdByPortMain"))
    wait.until(EC.presence_of_element_located((By.ID, "skdByPortBean")))

    driver.execute_script("""
        mojarra.jsfcljs(
            document.getElementById('skdByPortBean'),
            {'skd_p2p_detail':'skd_p2p_detail','srv_code':'ALL'},
            ''
        );
    """)

    wait.until(EC.url_contains("SkdByPortDetail"))
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DETAIL_TABLE_CSS)))
    WebDriverWait(driver, 15).until(
        lambda d: len(d.find_element(By.CSS_SELECTOR, DETAIL_TABLE_CSS)
                       .find_elements(By.CSS_SELECTOR, "tbody tr")) >= 1
        and d.find_element(By.CSS_SELECTOR, DETAIL_TABLE_CSS)
             .find_element(By.CSS_SELECTOR, "tbody tr").text.strip() != ""
    )

    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.find("table", id=lambda x: x and x.startswith("DATA_TABLE"))
    if table is None:
        raise RuntimeError("DATA_TABLE_* not found on detail page")
    return str(table), len(table.find("tbody").find_all("tr")), actual_pod


def scrape_with_decision(driver, wait, origin, dest_name, wanhai_locations, connections):
    """End-to-end one row: origin → to_nation → snapshot pod options → decide → submit.

    Returns (html, n_rows, actual_pol, actual_pod, kind, used_dest).
    Raises:
      WHLDestinationUnmapped  — dest not in wanhai_locations AND not a fallback-eligible port
      WHLDestinationSkipped   — dest didn't match the dropdown and isn't fallback-eligible
    """
    dest_resolved, dest_reason = resolve(dest_name, wanhai_locations)
    if dest_resolved is None:
        # Country-derivation fallback: ONLY for fallback-eligible ports.
        # Inlands / foreign ports / anything else: skip silently as unmapped.
        if _is_fallback_eligible_port(dest_name, connections):
            for inland in connections.get("ports", {}).get(dest_name, []):
                r, _ = resolve(inland, wanhai_locations)
                if r is not None:
                    dest_resolved = r
                    print(f"      Last CY={dest_name!r} not in wanhai_locations.json; "
                          f"using country from fallback {inland!r}")
                    break
        if dest_resolved is None:
            raise WHLDestinationUnmapped(
                f"{dest_name!r}: {dest_reason} (and no fallback-eligible port)"
            )

    to_nation = dest_resolved[0]
    actual_pol = open_search_and_set_origin(driver, wait, origin)

    safe_select(driver, "to_nation", to_nation)
    wait_for_pod_populated(driver)
    time.sleep(0.3)
    pod_options = get_options(driver, "pod")

    decision = pick_pod(dest_name, dest_resolved, pod_options, connections)
    if decision is None:
        raise WHLDestinationSkipped(
            f"{dest_name!r} not in pod dropdown for this origin "
            f"(eligible for fallback: {_is_fallback_eligible_port(dest_name, connections)})"
        )

    kind, used_dest, pod_code = decision
    safe_select(driver, "pod", pod_code)
    time.sleep(0.3)

    html, n_rows, actual_pod = finish_search(driver, wait, pod_code)
    return html, n_rows, actual_pol, actual_pod, kind, used_dest


# ---------------------------------------------------------------------------
# Driver factory (Cloudflare warmup)
# ---------------------------------------------------------------------------

def create_wanhai_session(headless=False, chrome_version=148, warmup_timeout=90):
    """Create an undetected_chromedriver session + WebDriverWait, warming Cloudflare.

    Returns (driver, wait).
    """
    options = uc.ChromeOptions()
    if headless:
        options.headless = True
        options.add_argument("--window-size=1920,1080")

    driver = uc.Chrome(version_main=chrome_version, options=options, headless=headless)
    wait = WebDriverWait(driver, 30)

    print("warming up (clearing Cloudflare)...")
    driver.get(WANHAI_URL)
    WebDriverWait(driver, warmup_timeout).until(
        EC.presence_of_element_located((By.NAME, "from_nation"))
    )
    print("ready.")
    return driver, wait


# ---------------------------------------------------------------------------
# HTML → JSON (ported from wanhai_html_to_json.py)
# ---------------------------------------------------------------------------

def _parse_metadata_comment(html_text):
    """Parse the leading '<!-- key=value | ... -->' comment into a dict."""
    m = re.search(r"<!--\s*(.*?)\s*-->", html_text)
    meta = {}
    if not m:
        return meta
    for part in m.group(1).split("|"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        meta[k] = None if v == "" or v.lower() == "nan" else v
    return meta


def _cell_lines(cell):
    """Return non-empty text lines from a cell, splitting on <br/>."""
    for s in cell.find_all("script"):
        s.decompose()
    text = cell.get_text(separator="\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _parse_vessel_block(cell):
    """Parse a Departure/Arrival cell: 4 lines → date / vessel / voyage / service."""
    lines = _cell_lines(cell)
    out = {"Date": "", "Vessel": "", "Voyage": "", "Service": ""}
    if len(lines) > 0:
        out["Date"] = lines[0]
    if len(lines) > 1:
        out["Vessel"] = lines[1]
    if len(lines) > 2:
        out["Voyage"] = lines[2]
    if len(lines) > 3:
        service = lines[3]
        if service.startswith("(") and service.endswith(")"):
            service = service[1:-1]
        out["Service"] = service
    return out


def _parse_ts_ports(cell):
    """Split T/S Port cell on <hr> tags; each fragment yields {Port, Date}."""
    html = cell.decode_contents()
    fragments = re.split(r"<hr\s*/?>", html, flags=re.IGNORECASE)
    ports = []
    for frag in fragments:
        if not frag.strip():
            continue
        sub = BeautifulSoup(frag, "html.parser")
        lines = [line.strip() for line in sub.get_text(separator="\n").split("\n") if line.strip()]
        if not lines:
            continue
        ports.append({"Port": lines[0], "Date": lines[1] if len(lines) > 1 else ""})
    return ports


def _parse_cut_off(cell):
    text = cell.get_text(strip=True)
    return "" if text in ("", "-") else text


def _parse_transfer_flag(cell):
    div = cell.find("div")
    return (div.get_text(strip=True) if div else cell.get_text(strip=True))


def _synthesize_legs(pol, departure, ts_ports, pod, arrival):
    """
    WHL HTML only names two vessels per schedule (Departure = mother covering
    POL -> TSPorts[0], Arrival = final covering TSPorts[-1] -> POD). Any extra
    in-between hop (TSPorts[i] -> TSPorts[i+1]) is a feeder that WHL leaves
    unnamed in the table. Synthesize a per-leg list that makes those feeder
    hops explicit:

      0 TS ports  -> 1 leg (POL -> POD, mother vessel)
      1 TS port   -> 2 legs (mother, arrival; no feeder)
      N TS ports  -> N+1 legs (mother, N-1 FEEDER legs, arrival)
    """
    departure = departure or {}
    arrival = arrival or {}
    ts_ports = ts_ports or []

    def _mk(p, q, dep_date, arr_date, vessel_block, is_feeder):
        if is_feeder:
            return {
                "Pol": p, "Pod": q,
                "Departure": dep_date or "", "Arrival": arr_date or "",
                "Vessel": None, "Voyage": None, "Service": None,
                "IsFeeder": True,
            }
        return {
            "Pol": p, "Pod": q,
            "Departure": dep_date or "", "Arrival": arr_date or "",
            "Vessel": vessel_block.get("Vessel") or None,
            "Voyage": vessel_block.get("Voyage") or None,
            "Service": vessel_block.get("Service") or None,
            "IsFeeder": False,
        }

    if not ts_ports:
        # Direct: one ocean hop, mother vessel covers POL -> POD.
        return [_mk(pol, pod,
                    departure.get("Date"), arrival.get("Date"),
                    departure, is_feeder=False)]

    legs = []
    # Mother leg: POL -> first TS port
    legs.append(_mk(pol, ts_ports[0].get("Port"),
                    departure.get("Date"), ts_ports[0].get("Date"),
                    departure, is_feeder=False))
    # Feeder legs (one per gap between consecutive TS ports)
    for i in range(len(ts_ports) - 1):
        legs.append(_mk(ts_ports[i].get("Port"), ts_ports[i + 1].get("Port"),
                        ts_ports[i].get("Date"), ts_ports[i + 1].get("Date"),
                        {}, is_feeder=True))
    # Arrival leg: last TS port -> POD
    legs.append(_mk(ts_ports[-1].get("Port"), pod,
                    ts_ports[-1].get("Date"), arrival.get("Date"),
                    arrival, is_feeder=False))
    return legs


def parse_html(html_text):
    """Parse a single Wan Hai HTML page → dict mirroring the CMA wrapper shape."""
    meta = _parse_metadata_comment(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", id="DATA_TABLE_US")

    schedules = []
    if table is not None:
        tbody = table.find("tbody")
        rows = tbody.find_all("tr", recursive=False) if tbody else []
        for tr in rows:
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 8:
                continue
            pol = cells[0].get_text(strip=True)
            departure = _parse_vessel_block(cells[2])
            ts_ports = _parse_ts_ports(cells[3])
            pod = cells[4].get_text(strip=True)
            arrival = _parse_vessel_block(cells[5])
            schedules.append({
                "POL": pol,
                "CutOff": _parse_cut_off(cells[1]),
                "Departure": departure,
                "TSPorts": ts_ports,
                "POD": pod,
                "Arrival": arrival,
                "TransitTime": cells[6].get_text(strip=True),
                "TransferFlag": _parse_transfer_flag(cells[7]),
                # Synthesized per-leg view (makes feeder hops explicit when
                # there are >=2 TS ports — WHL only names mother + arrival).
                "Legs": _synthesize_legs(pol, departure, ts_ports, pod, arrival),
            })

    # CMA-compatible wrapper: a `request` dict so build_schedule_dataframe can
    # read POL / LastCY / FinalDestination / query_date / snapshot_date / OFQ
    # by the same keys CMA uses.
    request = {
        "POL": meta.get("POL"),
        "LastCY": meta.get("LastCY"),
        "FinalDestination": meta.get("FinalDestination") or meta.get("LastCY"),
        "OFQ": meta.get("OFQ"),
        "query_date": meta.get("query_date"),
        "snapshot_date": meta.get("snapshot_date"),
    }

    return {
        "carrier": CARRIER,
        "request": request,
        "schedules": schedules,
    }


def transform_html_to_json(html_path: Path, html_archive_dir: Path = None):
    """Convert one .html to a sibling .json; archive (or delete) the HTML on success."""
    json_path = html_path.with_suffix(".json")
    try:
        html_text = html_path.read_text(encoding="utf-8")
        data = parse_html(html_text)
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
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
    """Process every wanhai_*.html in processing_dir into a sibling .json."""
    processing_dir = Path(processing_dir)
    html_files = sorted(processing_dir.glob("wanhai_*.html"))
    print(f"🔍 Found {len(html_files)} WHL HTML files in {processing_dir}")
    for html_file in html_files:
        transform_html_to_json(html_file, html_archive_dir)
    print("🏁 WHL HTML → JSON batch complete")


# ---------------------------------------------------------------------------
# JSON → DataFrame (ported from wanhaiDataStructuring.py)
# ---------------------------------------------------------------------------

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
    """'YYYY/MM/DD[ HH:MM:SS]' or 'YYYY-MM-DD' → 'MM/DD/YYYY'."""
    if not value or not isinstance(value, str):
        return ""
    s = value.strip()
    if s in ("", "-"):
        return ""
    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})", s)
    if m:
        yyyy, mm, dd = m.groups()
        return f"{mm}/{dd}/{yyyy}"
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return ""


def _parse_transit_days(value):
    """'56 days' → '56.0'. Empty → ''."""
    if not value:
        return ""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", str(value))
    return f"{float(m.group(1)):.1f}" if m else ""


def _transport_type(n):
    return "Direct" if n <= 0 else f"{n} TS"


def parse_schedule(schedule):
    """One JSON schedule → one parsed dict. TS Vessel(s) is the single Arrival.Vessel."""
    ts_ports = schedule.get("TSPorts") or []
    departure = schedule.get("Departure") or {}
    arrival = schedule.get("Arrival") or {}

    if ts_ports:
        ts_port_str = " - ".join(p.get("Port", "") for p in ts_ports)
        ts_vessel_str = arrival.get("Vessel", "")
        transport = _transport_type(len(ts_ports))
    else:
        ts_port_str = ""
        ts_vessel_str = ""
        transport = "Direct"

    return {
        "Port of Discharge": schedule.get("POD", ""),
        "ETD": departure.get("Date", ""),
        "ETA": arrival.get("Date", ""),
        "POD ETA": arrival.get("Date", ""),
        "Transit Time": _parse_transit_days(schedule.get("TransitTime")),
        "Transport Type": transport,
        "TS Port(s)": ts_port_str,
        "Mother Vessel": departure.get("Vessel", ""),
        "TS Vessel(s)": ts_vessel_str,
        "Cut-Off Date": schedule.get("CutOff", ""),
    }


def build_schedule_dataframe(processing_dir):
    """Read every *.json in processing_dir → combined 16-column DataFrame."""
    records = []
    processing_dir = Path(processing_dir)

    for file in os.listdir(processing_dir):
        if not file.endswith(".json"):
            continue
        full_path = processing_dir / file
        with open(full_path, "r", encoding="utf-8") as infile:
            data = json.load(infile)

        carrier = data.get("carrier") or CARRIER
        request = data.get("request") or {}
        port_of_loading = request.get("POL")
        last_cy = request.get("LastCY")
        final_destination = request.get("FinalDestination") or last_cy

        query_date = reformat_date(request.get("query_date"))
        period = reformat_date(request.get("snapshot_date"))

        for schedule in data.get("schedules", []):
            parsed = parse_schedule(schedule)
            records.append({
                "Carrier": carrier,
                "Port of Loading": port_of_loading,
                "Port of Discharge": parsed.get("Port of Discharge", ""),
                "Last CY": last_cy,
                "Final Destination": final_destination,
                "Query Date": query_date,
                "Period": period,
                "ETD": reformat_date(parsed.get("ETD")),
                "ETA": reformat_date(parsed.get("ETA")),
                "POD ETA": reformat_date(parsed.get("POD ETA")),
                "Transit Time": parsed.get("Transit Time"),
                "Transport Type": parsed.get("Transport Type", ""),
                "TS Port(s)": parsed.get("TS Port(s)", ""),
                "Mother Vessel": parsed.get("Mother Vessel", ""),
                "TS Vessel(s)": parsed.get("TS Vessel(s)", ""),
                "Cut-Off Date": reformat_date(parsed.get("Cut-Off Date")),
            })

    return pd.DataFrame(records, columns=COLUMNS)


# === CANONICAL TRANSFORMATION HELPERS ===

def _iso_date_or_none(value):
    """Accept None, 'YYYY/MM/DD', 'YYYY-MM-DD', or 'YYYY-MM-DD HH:MM[:SS]'. Return 'YYYY-MM-DD' or None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("/", "-")
    date_part = s.split("T", 1)[0].split(" ", 1)[0]
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


def _whl_transit_days(value):
    """Parse '61 days' -> 61. Returns None if unparseable."""
    if not value:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def _whl_add_days(iso_date, days):
    """Add `days` to an ISO date string. Returns ISO date or None on failure."""
    if not iso_date or days is None:
        return None
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d") + timedelta(days=int(days))
        return d.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _is_lastcy_a_port(last_cy, connections):
    """LastCY is a port iff it appears as a key in connections['ports']."""
    if not last_cy or not connections:
        return False
    return last_cy in (connections.get("ports") or {})


# All WHL legs are water-borne (mother voyage, feeder hop, or arrival voyage).
# WHL doesn't expose ground transport in its schedule table — any port-to-inland
# move is implicit in the LastCY assignment (handled at the date-mapping layer).
def _whl_leg_label(leg):
    """For a synthesized WHL leg: 'Vessel/Voyage' for named legs, 'FEEDER' for unnamed."""
    if leg.get("IsFeeder"):
        return "FEEDER"
    v = (leg.get("Vessel") or "").strip()
    voy = (leg.get("Voyage") or "").strip()
    if v and voy:
        return f"{v}/{voy}"
    return v or "VESSEL"


def _whl_legs(schedule):
    """Normalize WHL synthesized Legs into [{pol, pod, label, is_ocean, etd, eta}, ...]"""
    out = []
    for leg in schedule.get("Legs", []) or []:
        out.append({
            "pol": leg.get("Pol"),
            "pod": leg.get("Pod"),
            "label": _whl_leg_label(leg),
            "is_ocean": True,  # every WHL leg is water-borne (mother / feeder / arrival)
            "etd": leg.get("Departure"),
            "eta": leg.get("Arrival"),
        })
    return out


def _whl_legs_summary(legs):
    """Same contract as ONE/MSK/MSC/ZIM/HMM/OOCL/YML helpers (per structurePhilosophy)."""
    if not legs:
        return "Direct", None, [], [], [], []

    ocean = [lg for lg in legs if lg["is_ocean"]]
    n_ocean = len(ocean)

    if n_ocean == 0:
        transport_type = "Direct"
        mother_vessel = None
        ts_ports = []
        ts_vessels = []
    else:
        transport_type = "Direct" if n_ocean == 1 else f"{n_ocean - 1} TS"
        mother_vessel = ocean[0]["label"] or None
        ts_ports = [lg["pod"] for lg in ocean[:-1]]
        ts_vessels = [lg["label"] for lg in ocean[1:]]

    # Per structurePhilosophy: visible chain is ocean-only.
    route_ports = ([ocean[0]["pol"]] + [lg["pod"] for lg in ocean]) if ocean else []
    vessel_sequence = [lg["label"] for lg in ocean]
    return transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence


def _whl_dates(schedule, last_cy, connections):
    """
    Compute (etd, eta, pod_eta, cutoff) per WHL date semantics.

    Rule from user:
      - LastCY is a port:    ETA == POD ETA == Arrival.Date
      - LastCY is inland:    ETA = Departure.Date + transit_days, POD ETA = Arrival.Date
      - Unknown LastCY:      treat as port (safer default; foreign ports fall here)
    """
    departure = schedule.get("Departure") or {}
    arrival = schedule.get("Arrival") or {}
    etd = _iso_date_or_none(departure.get("Date"))
    pod_eta = _iso_date_or_none(arrival.get("Date"))
    cutoff = _iso_date_or_none(schedule.get("CutOff"))
    transit_days = _whl_transit_days(schedule.get("TransitTime"))

    if _is_lastcy_a_port(last_cy, connections):
        eta = pod_eta
    else:
        # Inland (or non-port) LastCY: derive ETA from ETD + transit days
        eta = _whl_add_days(etd, transit_days) if (etd and transit_days is not None) else pod_eta
    return etd, eta, pod_eta, cutoff


# ---------------------------------------------------------------------------
# Port of Discharge normalization
# ---------------------------------------------------------------------------

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
       the city and uppercase the state code.
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


def build_schedule_rows(file_path, connections=None):
    """
    Read a parsed WHL JSON and return flat CSV rows (one per schedule).
    Requires `connections` (from load_connections) to apply the port/inland
    ETA rule.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    request = data.get("request") or {}
    pol = request.get("POL")
    last_cy = request.get("LastCY")
    final_destination = request.get("FinalDestination") or last_cy
    query_date = request.get("query_date")
    snapshot_date = request.get("snapshot_date")

    wrap = {
        "Carrier": "WHL",
        "Port of Loading": pol,
        "Last CY": last_cy,
        "Final Destination": final_destination,
        "Query Date": query_date,
        "Period": snapshot_date,
    }

    rows = []
    for sched in data.get("schedules", []) or []:
        legs = _whl_legs(sched)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, _, _ = _whl_legs_summary(legs)

        etd, eta, pod_eta, cutoff = _whl_dates(sched, last_cy, connections)
        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        transit_days = _whl_transit_days(sched.get("TransitTime"))

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


def build_canonical_record(file_path, connections=None):
    """
    Read a parsed WHL JSON and return ONE canonical record per query.
    Requires `connections` for port/inland ETA semantics.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    request = data.get("request") or {}
    pol = request.get("POL")
    last_cy = request.get("LastCY")
    final_destination = request.get("FinalDestination") or last_cy
    query_date = request.get("query_date")
    snapshot_date = request.get("snapshot_date")

    schedules = []
    for sched in data.get("schedules", []) or []:
        legs = _whl_legs(sched)
        if not legs:
            continue
        transport_type, mother_vessel, ts_ports, ts_vessels, route_ports, vessel_sequence = _whl_legs_summary(legs)

        etd_iso, eta_iso, pod_eta_iso, cutoff_iso = _whl_dates(sched, last_cy, connections)
        ocean = [lg for lg in legs if lg["is_ocean"]]
        pod = normalize_pod(ocean[-1]["pod"] if ocean else legs[-1]["pod"])
        transit_days = _whl_transit_days(sched.get("TransitTime"))

        # Schedule id seed: combine mother voyage + first leg's departure date.
        # No carrier-provided unique id; this is unique within a query.
        mother_voyage = ((sched.get("Departure") or {}).get("Voyage") or "").strip() or None

        schedules.append({
            "id": _schedule_uuid("WHL", pol, pod, etd_iso, mother_vessel, mother_voyage),
            "port_of_discharge": pod,
            "cutoff_date": cutoff_iso,
            "etd": etd_iso,
            "eta": eta_iso,
            "pod_eta": pod_eta_iso,
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
        "id": _query_uuid("WHL", pol, last_cy, query_date),
        "carrier": {"code": "WHL", "name": "Wan Hai Lines"},
        "query_date": query_date,
        "snapshot_date": snapshot_date,
        "port_of_loading": pol,
        "last_cy": last_cy,
        "final_destination": final_destination,
        "schedules": schedules,
    }
