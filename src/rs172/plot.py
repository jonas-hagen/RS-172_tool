"""Plot logs saved with :func:`rs172.to_csv` / :func:`rs172.to_json`.

Needs the ``plot`` extra: ``pip install "rs172[plot]"``.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matplotlib.figure import Figure


@dataclass(frozen=True)
class Alarms:
    """Alarm thresholds; temperatures in °C."""

    temp_low: float
    temp_high: float
    rh_low: float
    rh_high: float


@dataclass(frozen=True)
class Series:
    """Readings loaded from an exported file."""

    times: list[datetime]
    temperature_c: list[float]
    humidity: list[float]
    alarms: Alarms | None = None  # only JSON files carry the alarm settings

    def __len__(self) -> int:
        return len(self.times)


#: Smallest y range shown, so sensor noise does not look like big swings.
MIN_TEMP_SPAN_C = 4.0
MIN_RH_SPAN = 5.0


def _ensure_span(ax, span: float, lower: float | None = None, upper: float | None = None) -> None:
    """Widen the y axis around its centre to at least ``span``, keeping it inside [lower, upper]."""
    lo, hi = ax.get_ylim()
    if hi - lo >= span:
        return
    mid = (lo + hi) / 2
    lo, hi = mid - span / 2, mid + span / 2
    if lower is not None and lo < lower:
        lo, hi = lower, lower + span
    if upper is not None and hi > upper:
        lo, hi = upper - span, upper
    ax.set_ylim(lo, hi)


def _f_to_c(v: float) -> float:
    return (v - 32) * 5 / 9


def load(path: str | Path) -> Series:
    """Load a CSV or JSON file written by this package (format chosen by the file extension)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        d = json.loads(path.read_text(encoding="utf-8"))
        rows = d["readings"]
        alarms = None
        if a := d.get("info", {}).get("alarm"):
            conv = _f_to_c if a["temp_unit"] == "F" else float
            alarms = Alarms(conv(a["temp_low"]), conv(a["temp_high"]), a["rh_low"], a["rh_high"])
        return Series(
            [datetime.fromisoformat(r["time"]) for r in rows],
            [float(r["temperature_c"]) for r in rows],
            [float(r["humidity_rh"]) for r in rows],
            alarms,
        )
    if suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return Series(
            [datetime.fromisoformat(r["time"]) for r in rows],
            [float(r["temperature_c"]) for r in rows],
            [float(r["humidity_rh"]) for r in rows],
        )
    raise ValueError(f"unsupported file type {suffix!r} (expected .csv or .json)")


def plot(series: Series, *, fahrenheit: bool = False, alarms: bool = False, title: str | None = None) -> Figure:
    """Draw temperature and humidity in two stacked panels sharing the time axis."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    conv = (lambda c: c * 9 / 5 + 32) if fahrenheit else (lambda c: c)
    unit = "°F" if fahrenheit else "°C"

    fig, (ax_t, ax_h) = plt.subplots(2, 1, sharex=True, figsize=(10, 6), layout="constrained")
    ax_t.plot(series.times, [conv(v) for v in series.temperature_c], color="tab:red", lw=1)
    ax_t.set_ylabel(f"Temperature ({unit})")
    ax_h.plot(series.times, series.humidity, color="tab:blue", lw=1)
    ax_h.set_ylabel("Humidity (%RH)")

    if alarms and series.alarms:
        a = series.alarms
        for ax, lo, hi in ((ax_t, conv(a.temp_low), conv(a.temp_high)), (ax_h, a.rh_low, a.rh_high)):
            for v, label in ((hi, "alarm high"), (lo, "alarm low")):
                ax.axhline(v, color="gray", ls="--", lw=0.8, label=label)
            ax.legend(loc="upper right", fontsize="small")

    _ensure_span(ax_t, MIN_TEMP_SPAN_C * (9 / 5 if fahrenheit else 1))
    _ensure_span(ax_h, MIN_RH_SPAN, 0, 100)
    for ax in (ax_t, ax_h):
        ax.grid(True, alpha=0.3)
    locator = mdates.AutoDateLocator()
    ax_h.xaxis.set_major_locator(locator)
    ax_h.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    if title:
        fig.suptitle(title)
    return fig
