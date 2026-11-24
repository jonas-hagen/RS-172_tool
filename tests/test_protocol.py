from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rs172 import protocol as p
from rs172.models import (
    CAPACITY,
    Interval,
    IntervalUnit,
    LedCycle,
    LoggerConfig,
    LogData,
    StartMode,
    TempUnit,
)

DATA = Path(__file__).parent / "data"


def test_bcd_roundtrip():
    for v in range(100):
        assert p.from_bcd(p.to_bcd(v)) == v
    with pytest.raises(p.ProtocolError):
        p.from_bcd(0x1A)


def test_set_time():
    # frame observed on the device
    assert p.build_set_time(datetime(2026, 11, 24, 9, 47, 40)) == bytes.fromhex("58261124094740")


# Setup frames that were sent to the device and acknowledged; the last two were
# cross-checked against the vendor software.
@pytest.mark.parametrize(
    "cfg, frame",
    [
        (
            LoggerConfig(Interval(2, IntervalUnit.SECONDS), LedCycle.S10, StartMode.AUTOMATIC, False,
                         TempUnit.CELSIUS, 0, 40, 30, 90, True),
            "18410228005a1e",
        ),
        (
            LoggerConfig(Interval(2, IntervalUnit.SECONDS), LedCycle.S10, StartMode.MANUAL, False,
                         TempUnit.CELSIUS, 0, 40, 30, 90, True),
            "18c10228005a1e",
        ),
        (
            LoggerConfig(Interval(2, IntervalUnit.SECONDS), LedCycle.S10, StartMode.AUTOMATIC, False,
                         TempUnit.CELSIUS, -10, 40, 30, 90, True),
            "184502280a5a1e",
        ),
        (
            LoggerConfig(Interval(2, IntervalUnit.SECONDS), LedCycle.OFF, StartMode.AUTOMATIC, False,
                         TempUnit.FAHRENHEIT, -20, -5, 25, 70, True),
            "485d0205144619",
        ),
    ],
)
def test_build_setup(cfg, frame):
    assert p.build_setup(cfg).hex() == frame


def test_setup_flags_misc():
    cfg = LoggerConfig(Interval(3, IntervalUnit.HOURS), LedCycle.S30, StartMode.AUTOMATIC, True,
                       alarm_led=False)
    frame = p.build_setup(cfg)
    assert frame[0] == 0x38
    assert frame[1] == p.FLAG_NO_ALARM_LED | 0x03
    assert frame[2] == 3


def test_default_config():
    cfg = LoggerConfig.default()
    assert p.build_setup(cfg).hex() == "18c10a28005a1e"
    assert cfg.record_duration == timedelta(seconds=CAPACITY * 10)


def test_config_validation():
    with pytest.raises(ValueError):
        LoggerConfig(temp_alarm_low=50, temp_alarm_high=40)
    with pytest.raises(ValueError):
        LoggerConfig(temp_alarm_high=100)  # > 70 °C
    LoggerConfig(temp_unit=TempUnit.FAHRENHEIT, temp_alarm_high=150)
    with pytest.raises(ValueError):
        LoggerConfig(rh_alarm_high=101)
    with pytest.raises(ValueError):
        Interval(60, IntervalUnit.SECONDS)
    with pytest.raises(ValueError):
        Interval(0, IntervalUnit.MINUTES)


def test_interval_from_timedelta():
    assert Interval.from_timedelta(timedelta(seconds=30)) == Interval(30, IntervalUnit.SECONDS)
    assert Interval.from_timedelta(timedelta(minutes=2)) == Interval(2, IntervalUnit.MINUTES)
    assert Interval.from_timedelta(timedelta(hours=5)) == Interval(5, IntervalUnit.HOURS)
    with pytest.raises(ValueError):
        Interval.from_timedelta(timedelta(seconds=90))


@pytest.mark.parametrize(
    "frame, expect",
    [
        ("dd00440000300 1ce261124092031 28005a1e2378aa",
         dict(sample_count=17, interval=Interval(1, IntervalUnit.MINUTES), start=datetime(2026, 11, 24, 9, 20, 31),
              unit=TempUnit.CELSIUS, t=(0, 40), rh=(30, 90))),
        ("dd001c0000600 3ce261124095019 230a50142376aa",
         dict(sample_count=7, interval=Interval(3, IntervalUnit.SECONDS), start=datetime(2026, 11, 24, 9, 50, 19),
              unit=TempUnit.CELSIUS, t=(-10, 35), rh=(20, 80))),
        ("dd00100000600 2ce261124095052 05144619 2477aa",
         dict(sample_count=4, interval=Interval(2, IntervalUnit.SECONDS), start=datetime(2026, 11, 24, 9, 50, 52),
              unit=TempUnit.FAHRENHEIT, t=(-20, -5), rh=(25, 70))),
    ],
)
def test_parse_header(frame, expect):
    info = p.parse_header(bytes.fromhex(frame.replace(" ", "")))
    assert info.sample_count == expect["sample_count"]
    assert info.interval == expect["interval"]
    assert info.start_time == expect["start"]
    assert info.temp_unit is expect["unit"]
    assert (info.temp_alarm_low, info.temp_alarm_high) == expect["t"]
    assert (info.rh_alarm_low, info.rh_alarm_high) == expect["rh"]


def test_parse_header_rejects_garbage():
    with pytest.raises(p.ProtocolError):
        p.parse_header(bytes(21))
    with pytest.raises(p.ProtocolError):
        p.parse_header(b"\xdd")


def test_chunks():
    assert p.build_read_chunk(0).hex() == "68686868686800"
    assert p.chunks_needed(0) == 0
    assert p.chunks_needed(17) == 1
    assert p.chunks_needed(164) == 2  # 656 bytes
    assert p.chunks_needed(CAPACITY) == 100


def test_parse_real_download_matches_vendor_software():
    """Data captured from the device; expected values read from the vendor app's summary."""
    info = p.parse_header((DATA / "header_17samples.bin").read_bytes())
    readings = p.parse_records((DATA / "chunk00_17samples.bin").read_bytes(), info)
    log = LogData(info, readings)
    assert len(log) == 17
    assert log.end == datetime(2026, 11, 24, 9, 36, 31)
    t, h = log.temperature_stats(), log.humidity_stats()
    assert (t.max.value, t.max.time) == (24.0, datetime(2026, 11, 24, 9, 22, 31))
    assert (t.min.value, t.min.time) == (22.7, datetime(2026, 11, 24, 9, 36, 31))
    assert t.avg == 23.36
    assert (h.max.value, h.max.time) == (42.9, datetime(2026, 11, 24, 9, 20, 31))
    assert (h.min.value, h.min.time) == (31.6, datetime(2026, 11, 24, 9, 23, 31))
    assert h.avg == 33.19


def test_negative_temperatures():
    info = p.parse_header(bytes.fromhex("dd00080000600 1ce261124095019 230a50142376aa".replace(" ", "")))
    r = p.parse_records(bytes.fromhex("ff9c03e8" "fe700001"), info)
    assert (r[0].temperature_c, r[0].humidity) == (-10.0, 100.0)
    assert (r[1].temperature_c, r[1].humidity) == (-40.0, 0.1)
    assert r[1].temperature_f == -40.0
