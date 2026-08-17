from __future__ import annotations

from datetime import date
from typing import Optional, List, Dict, Any

import re
import requests

from event_harvester.models import Event
from event_harvester.utils.hashing import stable_event_id


# OpenFEMA API base (this is the API, not the FEMA web page)
API_BASE = "https://www.fema.gov/api/open/v2"
RESOURCE = "DisasterDeclarationsSummaries"
ENDPOINT = f"{API_BASE}/{RESOURCE}"


def _date_to_fema_iso(d: date) -> str:
    # FEMA filter strings expect UTC-like ISO; this works for filtering by date boundaries
    return f"{d.isoformat()}T00:00:00.000Z"


def normalize_incident_type(incident_type: Optional[str]) -> str:
    """Normalize FEMA incident types to a stable snake_case vocabulary."""
    if not incident_type:
        return "unknown"

    s = incident_type.strip().lower()

    # Basic cleanup: replace non-alphanumerics with underscore, collapse repeats
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")

    # Optional: map some known weird variants into canonical names
    mapping = {
        "severe_storm_s": "severe_storms",
        "severe_storm": "severe_storms",
        "coastal_storm": "coastal_storm",
        "snow": "winter_storm",
    }
    return mapping.get(s, s)


def fetch_raw_fema_data(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    top: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Fetch raw disaster declaration summaries from OpenFEMA with pagination.
    If start_date/end_date are provided, filter by declarationDate:
      start <= declarationDate < end  (end is exclusive)
    """
    params: Dict[str, Any] = {
        "$top": top,
        "$skip": 0,
    }

    # Build filter
    filters = []
    if start_date:
        filters.append(f"declarationDate ge '{_date_to_fema_iso(start_date)}'")
    if end_date:
        filters.append(f"declarationDate lt '{_date_to_fema_iso(end_date)}'")
    if filters:
        params["$filter"] = " and ".join(filters)

    all_records: List[Dict[str, Any]] = []

    while True:
        resp = requests.get(ENDPOINT, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

        # OpenFEMA v2 returns the list under the resource name:
        # {"DisasterDeclarationsSummaries": [...], "metadata": {...}}
        page = payload.get(RESOURCE, [])
        if not page:
            break

        all_records.extend(page)

        # Advance pagination
        params["$skip"] += top

    return all_records


def to_event(raw: Dict[str, Any]) -> Event:
    source = "FEMA"

    state = raw.get("state")  # e.g. "GA"
    disaster_number = raw.get("disasterNumber")

    # Stable source record id
    source_record_id = f"{disaster_number}-{state}" if state else str(disaster_number)

    event_id = stable_event_id(source, source_record_id)

    incident_type_raw = raw.get("incidentType")
    event_type = normalize_incident_type(incident_type_raw)

    title = f"{incident_type_raw or 'Unknown'} - {state or 'NA'} - Disaster {disaster_number}"

    # Prefer incidentBeginDate if available; else declarationDate
    event_start = raw.get("incidentBeginDate") or raw.get("declarationDate")
    event_end = raw.get("incidentEndDate")

    description = raw.get("declarationTitle") or None

    return Event(
        event_id=event_id,
        source=source,
        source_record_id=source_record_id,
        event_type=event_type,
        title=title,
        event_start=event_start,   # let Pydantic parse ISO strings
        event_end=event_end,
        state=state,
        description=description,
        url=None,
        raw=raw,
    )


def get_events(start_date: Optional[date] = None, end_date: Optional[date] = None) -> List[Event]:
    raw_data = fetch_raw_fema_data(start_date=start_date, end_date=end_date)
    return [to_event(r) for r in raw_data]


# if __name__ == "__main__":
#     # Quick sanity test: 1 month window
#     events = get_events(date(2020, 1, 1), date(2020, 2, 1))
#     print("Fetched events:", len(events))
#     for e in events[:3]:
#         print(e.model_dump())
