"""Data classes and enums describing logger configuration and recorded data."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import fmean

#: Maximum number of samples the logger can store.
CAPACITY = 16350


class IntervalUnit(enum.Enum):
    """Unit of the sampling interval."""

    SECONDS = "s"
    MINUTES = "min"
    HOURS = "h"

    @property
    def seconds(self) -> int:
        return {"s": 1, "min": 60, "h": 3600}[self.value]

    @property
    def max_value(self) -> int:
        return 24 if self is IntervalUnit.HOURS else 59


class LedCycle(enum.Enum):
    """Flash cycle of the REC LED. ``OFF`` gives the longest battery life."""

    S10 = 10
    S20 = 20
    S30 = 30
    OFF = 0


class StartMode(enum.Enum):
    """``MANUAL``: logging starts on button press; ``AUTOMATIC``: immediately after setup."""

    MANUAL = "manual"
    AUTOMATIC = "automatic"


class TempUnit(enum.Enum):
    """Unit of the temperature alarm thresholds (data is always stored in °C)."""

    CELSIUS = "C"
    FAHRENHEIT = "F"


@dataclass(frozen=True)
class BatteryStatus:
    """Battery status as reported by the logger."""

    raw: int

    @property
    def description(self) -> str:
        return {4: "70%-100%"}.get(self.raw, f"unknown (code 0x{self.raw:02x})")

    def __str__(self) -> str:
        return self.description


@dataclass(frozen=True)
class Interval:
    """Sampling interval as the logger stores it: a value plus a unit."""

    value: int
    unit: IntervalUnit

    def __post_init__(self) -> None:
        if not 1 <= self.value <= self.unit.max_value:
            raise ValueError(
                f"interval value for unit {self.unit.value!r} must be in 1..{self.unit.max_value}, got {self.value}"
            )

    @classmethod
    def from_timedelta(cls, td: timedelta) -> Interval:
        """Pick the representation the device supports (e.g. 90 s -> error, 120 s -> 2 min)."""
        secs = td.total_seconds()
        if secs != int(secs):
            raise ValueError(f"interval must be a whole number of seconds: {td}")
        secs = int(secs)
        for unit in (IntervalUnit.SECONDS, IntervalUnit.MINUTES, IntervalUnit.HOURS):
            if secs % unit.seconds == 0 and 1 <= secs // unit.seconds <= unit.max_value:
                return cls(secs // unit.seconds, unit)
        raise ValueError(f"interval {td} cannot be represented (1-59 s, 1-59 min or 1-24 h)")

    @property
    def timedelta(self) -> timedelta:
        return timedelta(seconds=self.value * self.unit.seconds)

    def __str__(self) -> str:
        return f"{self.value} {self.unit.value}"


def _temp_limits(unit: TempUnit) -> tuple[int, int]:
    return (-40, 70) if unit is TempUnit.CELSIUS else (-40, 158)


@dataclass(frozen=True)
class LoggerConfig:
    """Complete logger setup, mirroring the vendor software's setup dialog.

    Alarm temperatures are given in ``temp_unit``; humidity in %RH.
    """

    interval: Interval = field(default_factory=lambda: Interval(10, IntervalUnit.SECONDS))
    led_cycle: LedCycle = LedCycle.S10
    start_mode: StartMode = StartMode.MANUAL
    circular: bool = False
    temp_unit: TempUnit = TempUnit.CELSIUS
    temp_alarm_low: int = 0
    temp_alarm_high: int = 40
    rh_alarm_low: int = 30
    rh_alarm_high: int = 90
    alarm_led: bool = True

    def __post_init__(self) -> None:
        lo, hi = _temp_limits(self.temp_unit)
        for name in ("temp_alarm_low", "temp_alarm_high"):
            v = getattr(self, name)
            if not lo <= v <= hi:
                raise ValueError(f"{name} must be in {lo}..{hi} °{self.temp_unit.value}, got {v}")
        for name in ("rh_alarm_low", "rh_alarm_high"):
            v = getattr(self, name)
            if not 0 <= v <= 100:
                raise ValueError(f"{name} must be in 0..100 %RH, got {v}")
        if self.temp_alarm_low > self.temp_alarm_high:
            raise ValueError("temp_alarm_low must not exceed temp_alarm_high")
        if self.rh_alarm_low > self.rh_alarm_high:
            raise ValueError("rh_alarm_low must not exceed rh_alarm_high")

    @classmethod
    def default(cls) -> LoggerConfig:
        """Factory defaults, identical to the vendor software's *Default* button."""
        return cls()

    @property
    def record_duration(self) -> timedelta:
        """Time until the memory is full at this interval (the vendor's "Rec Time")."""
        return CAPACITY * self.interval.timedelta

    def memory_full_time(self, start: datetime) -> datetime:
        """Time of the last sample that fits into memory for a log started at ``start``."""
        return start + (CAPACITY - 1) * self.interval.timedelta


@dataclass(frozen=True)
class LoggerInfo:
    """Contents of the log header (what the logger itself reports)."""

    sample_count: int
    interval: Interval
    start_time: datetime
    temp_unit: TempUnit
    temp_alarm_low: int
    temp_alarm_high: int
    rh_alarm_low: int
    rh_alarm_high: int
    overrun_marker: int  # header bytes 3-4, believed to relate to circular overruns

    @property
    def end_time(self) -> datetime | None:
        if self.sample_count == 0:
            return None
        return self.start_time + (self.sample_count - 1) * self.interval.timedelta


@dataclass(frozen=True)
class Reading:
    """A single sample."""

    index: int
    time: datetime
    temperature_c: float
    humidity: float

    @property
    def temperature_f(self) -> float:
        return round(self.temperature_c * 9 / 5 + 32, 2)


@dataclass(frozen=True)
class Extreme:
    value: float
    time: datetime


@dataclass(frozen=True)
class SeriesStats:
    min: Extreme
    max: Extreme
    avg: float


@dataclass(frozen=True)
class LogData:
    """A downloaded log: header info plus all readings."""

    info: LoggerInfo
    readings: list[Reading]

    @property
    def start(self) -> datetime:
        return self.info.start_time

    @property
    def end(self) -> datetime | None:
        return self.readings[-1].time if self.readings else None

    @property
    def interval(self) -> timedelta:
        return self.info.interval.timedelta

    def __len__(self) -> int:
        return len(self.readings)

    def temperature_stats(self) -> SeriesStats | None:
        return self._stats("temperature_c")

    def humidity_stats(self) -> SeriesStats | None:
        return self._stats("humidity")

    def _stats(self, attr: str) -> SeriesStats | None:
        if not self.readings:
            return None
        # first occurrence wins, like the vendor software
        lo = min(self.readings, key=lambda r: getattr(r, attr))
        hi = max(self.readings, key=lambda r: getattr(r, attr))
        avg = round(fmean(getattr(r, attr) for r in self.readings), 2)
        return SeriesStats(Extreme(getattr(lo, attr), lo.time), Extreme(getattr(hi, attr), hi.time), avg)
