from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Any, Dict, Union

from pydantic import BaseModel, Field, field_validator, ConfigDict


DateLike = Union[datetime, date]


class Event(BaseModel):
    # (Recommended) allow you to keep raw payloads without worrying about extra keys elsewhere
    model_config = ConfigDict(extra="forbid")

    # Required fields
    event_id: str
    source: str
    source_record_id: str
    event_type: str
    title: str
    event_start: DateLike
    raw: Dict[str, Any]

    # Common but may be missing depending on source
    state: Optional[str] = None

    # Metadata
    retrieved_at: datetime = Field(default_factory=datetime.utcnow)

    # Optional fields
    event_end: Optional[DateLike] = None
    fatalities: Optional[int] = None
    injuries: Optional[int] = None
    property_damage_usd: Optional[float] = None
    description: Optional[str] = None
    url: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    county: Optional[str] = None

    @field_validator("state")
    @classmethod
    def normalize_state(cls, v: Optional[str]) -> Optional[str]:
        """Normalize state to 2-letter uppercase when possible."""
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        return v.upper()
