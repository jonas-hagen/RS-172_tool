"""Export downloaded logs to CSV or JSON."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .models import LogData, SeriesStats

CSV_COLUMNS = ["index", "time", "temperature_c", "temperature_f", "humidity_rh"]


def _write_text(text: str, path: str | Path | None) -> str:
    if path is not None:
        Path(path).write_text(text, encoding="utf-8", newline="")
    return text


def to_csv(log: LogData, path: str | Path | None = None) -> str:
    """CSV with one row per reading (ISO 8601 local timestamps). Returns the text, writes ``path`` if given."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in log.readings:
        w.writerow([r.index, r.time.isoformat(), f"{r.temperature_c:.1f}", f"{r.temperature_f:.2f}", f"{r.humidity:.1f}"])
    return _write_text(buf.getvalue(), path)


def _stats(s: SeriesStats | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "min": s.min.value,
        "min_time": s.min.time.isoformat(),
        "max": s.max.value,
        "max_time": s.max.time.isoformat(),
        "avg": s.avg,
    }


def to_dict(log: LogData) -> dict[str, Any]:
    info = log.info
    return {
        "info": {
            "start_time": info.start_time.isoformat(),
            "end_time": log.end.isoformat() if log.end else None,
            "interval_seconds": int(log.interval.total_seconds()),
            "sample_count": info.sample_count,
            "alarm": {
                "temp_unit": info.temp_unit.value,
                "temp_low": info.temp_alarm_low,
                "temp_high": info.temp_alarm_high,
                "rh_low": info.rh_alarm_low,
                "rh_high": info.rh_alarm_high,
            },
        },
        "stats": {"temperature_c": _stats(log.temperature_stats()), "humidity_rh": _stats(log.humidity_stats())},
        "readings": [
            {"index": r.index, "time": r.time.isoformat(), "temperature_c": r.temperature_c, "humidity_rh": r.humidity}
            for r in log.readings
        ],
    }


def to_json(log: LogData, path: str | Path | None = None, *, indent: int | None = 2) -> str:
    """JSON with header info, statistics and readings. Returns the text, writes ``path`` if given."""
    return _write_text(json.dumps(to_dict(log), indent=indent) + "\n", path)
