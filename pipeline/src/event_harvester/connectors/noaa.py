from __future__ import annotations

import csv
import gzip
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from event_harvester.models import Event
from event_harvester.utils.hashing import stable_event_id


NOAA_DIR_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
# We will auto-select latest matching:
# StormEvents_details-ftp_v1.0_dYYYY_cYYYYMMDD.csv.gz


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _snake(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


_NOAA_DT_FMT = "%d-%b-%y %H:%M:%S"

def parse_noaa_datetime(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        # NOAA uses month abbreviations like JUN, JAN, etc.
        return datetime.strptime(s, _NOAA_DT_FMT)
    except Exception:
        return None


def parse_damage(value: Optional[str]) -> Optional[float]:
    """
    Parse NOAA damage strings like '10K', '2.5M', '1B', '0', '', None into USD float.
    Returns None if missing/blank. Returns 0.0 for '0'/'0.00K' etc.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None

    # Some files use "0.00K" etc.
    if s in {"0", "0.0", "0.00"}:
        return 0.0

    m = re.fullmatch(r"(?i)\s*([0-9]*\.?[0-9]+)\s*([KMB])?\s*", s)
    if not m:
        # If weird format, try to coerce numeric
        try:
            return float(s)
        except Exception:
            return None

    num = float(m.group(1))
    suf = (m.group(2) or "").upper()
    mult = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}.get(suf, 1.0)
    return num * mult


def _safe_int(x: Any) -> Optional[int]:
    try:
        if x is None or str(x).strip() == "":
            return None
        return int(float(x))
    except Exception:
        return None


def _pick_latest_details_filename(index_html: str, year: int) -> str:
    """
    From NOAA directory listing HTML, pick the latest details file for a given year
    based on the largest cYYYYMMDD suffix.
    """
    # Example: StormEvents_details-ftp_v1.0_d2020_c20260116.csv.gz
    pat = re.compile(rf"StormEvents_details-ftp_v1\.0_d{year}_c(\d{{8}})\.csv\.gz")
    matches = pat.findall(index_html)
    if not matches:
        raise FileNotFoundError(f"No StormEvents details file found for year {year} at {NOAA_DIR_URL}")

    latest_c = max(matches)
    return f"StormEvents_details-ftp_v1.0_d{year}_c{latest_c}.csv.gz"


def get_latest_details_url(year: int) -> str:
    """
    Discover the latest NOAA Storm Events details file URL for the given year.
    """
    resp = requests.get(NOAA_DIR_URL, timeout=60)
    resp.raise_for_status()
    fname = _pick_latest_details_filename(resp.text, year)
    return NOAA_DIR_URL + fname


def download_noaa_year(year: int, raw_dir: Path) -> Tuple[Path, Path]:
    """
    Download and decompress NOAA Storm Events details for a given year.
    Returns: (gz_path, csv_path)
    """
    _ensure_dir(raw_dir)

    url = get_latest_details_url(year)
    gz_path = raw_dir / Path(url).name
    csv_path = raw_dir / gz_path.name.replace(".csv.gz", ".csv")

    # Download if not already present
    if not gz_path.exists():
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(gz_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    # Decompress if not already present
    if not csv_path.exists():
        with gzip.open(gz_path, "rb") as fin, open(csv_path, "wb") as fout:
            fout.write(fin.read())

    return gz_path, csv_path


def row_to_event(row: Dict[str, Any]) -> Event:
    """
    Convert one NOAA StormEvents details row to Event.
    NOAA headers are uppercase with underscores (commonly).
    """
    source = "NOAA_STORM"

    episode_id = row.get("EPISODE_ID") or row.get("episode_id")
    event_id_raw = row.get("EVENT_ID") or row.get("event_id")
    source_record_id = f"{episode_id}-{event_id_raw}"

    event_id = stable_event_id(source, source_record_id)

    event_type = _snake(row.get("EVENT_TYPE") or row.get("event_type"))

    state = row.get("STATE") or row.get("state")  # Often full name (e.g., "GEORGIA")
    county = row.get("CZ_NAME") or row.get("cz_name")  # County/Zone name

    # Dates: let Pydantic parse these strings. They often look like "01-JAN-20 00:00:00"
    # Pydantic *may* not parse NOAA's custom format automatically. If it fails, we’ll add a parser.
    event_start = parse_noaa_datetime(row.get("BEGIN_DATE_TIME") or row.get("begin_date_time"))
    event_end = parse_noaa_datetime(row.get("END_DATE_TIME") or row.get("end_date_time"))


    title = f"{row.get('EVENT_TYPE','Unknown')} - {state or 'NA'} - {event_start or ''}".strip()

    deaths_direct = _safe_int(row.get("DEATHS_DIRECT") or row.get("deaths_direct")) or 0
    injuries_direct = _safe_int(row.get("INJURIES_DIRECT") or row.get("injuries_direct")) or 0

    dmg_property = parse_damage(row.get("DAMAGE_PROPERTY") or row.get("damage_property"))

    narrative = row.get("EVENT_NARRATIVE") or row.get("event_narrative") or None

    # Lat/lon fields exist in details for some years: BEGIN_LAT, BEGIN_LON, END_LAT, END_LON
    lat = row.get("BEGIN_LAT") or row.get("begin_lat")
    lon = row.get("BEGIN_LON") or row.get("begin_lon")
    try:
        lat_f = float(lat) if lat not in (None, "") else None
    except Exception:
        lat_f = None
    try:
        lon_f = float(lon) if lon not in (None, "") else None
    except Exception:
        lon_f = None

    return Event(
        event_id=event_id,
        source=source,
        source_record_id=source_record_id,
        event_type=event_type,
        title=title,
        event_start=event_start,  # may need custom parser later
        event_end=event_end,
        state=state,
        county=county,
        lat=lat_f,
        lon=lon_f,
        fatalities=deaths_direct,
        injuries=injuries_direct,
        property_damage_usd=dmg_property,
        description=narrative,
        url=None,
        raw=row,
    )


def parse_noaa_csv(csv_path: Path) -> Iterable[Event]:
    """
    Stream-parse the NOAA details CSV and yield Events.
    """
    with open(csv_path, "r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                yield row_to_event(row)
            except Exception:
                # optionally log row identifiers here
                continue



def get_events(
    year: int,
    raw_dir: Path,
    state_filter: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Event]:
    """
    Download + parse NOAA Storm Events details for a year.
    Optional:
      - state_filter: match STATE (case-insensitive)
      - start_date/end_date: filter by event_start date (best-effort; may be refined later)
    """
    _, csv_path = download_noaa_year(year, raw_dir)

    events: List[Event] = []
    sf = state_filter.strip().lower() if state_filter else None

    for ev in parse_noaa_csv(csv_path):
        # State filter (NOAA often uses full state name)
        if sf and (ev.state or "").strip().lower() != sf:
            continue

        # Date filtering (best-effort; if event_start is datetime/date already, great)
        if start_date or end_date:
            try:
                # Pydantic may have parsed; if not, this might still be a string.
                # We'll refine this once you run and see what type it is.
                ev_date = ev.event_start.date() if hasattr(ev.event_start, "date") else None
            except Exception:
                ev_date = None

            if start_date and ev_date and ev_date < start_date:
                continue
            if end_date and ev_date and ev_date >= end_date:
                continue

        events.append(ev)

    return events


if __name__ == "__main__":
    # Example test
    raw_dir = Path("data/raw/noaa")
    events = get_events(2020, raw_dir=raw_dir, state_filter="GEORGIA")
    print("Events:", len(events))
    for e in events[:3]:
        print(e.model_dump())
