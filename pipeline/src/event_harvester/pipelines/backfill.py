from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from event_harvester.models import Event

# Connectors
from event_harvester.connectors.fema import get_events as get_fema_events
from event_harvester.connectors.noaa import get_events as get_noaa_events
from event_harvester.connectors.nrc import bulk_events as get_nrc_bulk_events


# ---------- config ----------

@dataclass(frozen=True)
class BackfillConfig:
    out_dir: Path = Path("data/normalized")
    raw_dir: Path = Path("data/raw")

    # FEMA: date window (end is exclusive)
    fema_start: date = date(2020, 1, 1)
    fema_end: date = date(2020, 2, 1)

    # NOAA/NRC: list of years
    years: List[int] = None

    # NOAA state filter (optional). NOAA uses full state name like "GEORGIA"
    noaa_state_filter: Optional[str] = None

    def __post_init__(self):
        if self.years is None:
            object.__setattr__(self, "years", [2020])


# ---------- io helpers ----------

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def write_jsonl(events: Iterable[Event], out_path: Path) -> int:
    """
    Write events to JSONL. Returns number written.
    Uses default=str to serialize datetimes/dates.
    """
    _ensure_dir(out_path.parent)
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev.model_dump(), default=str) + "\n")
            n += 1
    return n


def read_jsonl(out_path: Path) -> Iterable[dict]:
    with open(out_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def merge_dedup_jsonl(input_files: List[Path], merged_out: Path) -> int:
    """
    Merge JSONL files, dedup by event_id, write merged JSONL.
    Returns number of unique events written.
    """
    _ensure_dir(merged_out.parent)
    seen = set()
    n = 0
    with open(merged_out, "w", encoding="utf-8") as out:
        for fp in input_files:
            for obj in read_jsonl(fp):
                eid = obj.get("event_id")
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                out.write(json.dumps(obj, default=str) + "\n")
                n += 1
    return n


# ---------- pipeline steps ----------

def run_fema(cfg: BackfillConfig) -> Path:
    out_path = cfg.out_dir / f"fema_{cfg.fema_start.isoformat()}_{cfg.fema_end.isoformat()}.jsonl"
    events = get_fema_events(cfg.fema_start, cfg.fema_end)
    n = write_jsonl(events, out_path)
    print(f"[FEMA] wrote {n} events -> {out_path}")
    return out_path


def run_noaa(cfg: BackfillConfig) -> List[Path]:
    out_files: List[Path] = []
    for y in cfg.years:
        out_path = cfg.out_dir / f"noaa_{y}.jsonl"
        raw_dir = cfg.raw_dir / "noaa"
        events = get_noaa_events(
            year=y,
            raw_dir=raw_dir,
            state_filter=cfg.noaa_state_filter,
        )
        n = write_jsonl(events, out_path)
        print(f"[NOAA] year={y} wrote {n} events -> {out_path}")
        out_files.append(out_path)
    return out_files


def run_nrc(cfg: BackfillConfig) -> List[Path]:
    out_files: List[Path] = []
    for y in cfg.years:
        out_path = cfg.out_dir / f"nrc_{y}.jsonl"
        raw_dir = cfg.raw_dir / "nrc" / "bulk" / str(y)
        events = list(get_nrc_bulk_events(y, raw_dir=raw_dir))
        n = write_jsonl(events, out_path)
        print(f"[NRC] year={y} wrote {n} events -> {out_path}")
        out_files.append(out_path)
    return out_files


def main() -> None:
    # Start small; expand once stable
    cfg = BackfillConfig(
        fema_start=date(2020, 1, 1),
        fema_end=date(2020, 2, 1),
        years=[2020],
        noaa_state_filter=None,  # e.g. "GEORGIA"
    )

    _ensure_dir(cfg.out_dir)

    fema_file = run_fema(cfg)
    noaa_files = run_noaa(cfg)
    nrc_files = run_nrc(cfg)

    merged_out = cfg.out_dir / "all_events_merged.jsonl"
    unique_n = merge_dedup_jsonl([fema_file] + noaa_files + nrc_files, merged_out)
    print(f"[MERGE] wrote {unique_n} unique events -> {merged_out}")


if __name__ == "__main__":
    main()
