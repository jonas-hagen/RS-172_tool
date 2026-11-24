"""Pure encode/decode functions for the RS-172 serial protocol (no I/O).

See ``docs/protocol.md`` for the full description.
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    BatteryStatus,
    Interval,
    IntervalUnit,
    LedCycle,
    LoggerConfig,
    LoggerInfo,
    Reading,
    StartMode,
    TempUnit,
)

BAUDRATE = 9600
USB_VID = 0x10C4
USB_PID = 0xEA60

BYTES_PER_SAMPLE = 4
CHUNK_SIZE = 654
CHUNK_COUNT = 100
MEMORY_SIZE = CHUNK_SIZE * CHUNK_COUNT  # 65400 bytes = 16350 samples

CMD_BATTERY = bytes([0x79])
CMD_HEADER = bytes([0x68] * 7)
HEADER_SIZE = 21

ACK_SET_TIME = 0xEE
ACK_SETUP = 0x1E


class ProtocolError(Exception):
    """The logger sent an unexpected reply."""


# --- helpers ---------------------------------------------------------------

def to_bcd(value: int) -> int:
    if not 0 <= value <= 99:
        raise ValueError(f"BCD value out of range: {value}")
    return (value // 10) << 4 | (value % 10)


def from_bcd(byte: int) -> int:
    hi, lo = byte >> 4, byte & 0x0F
    if hi > 9 or lo > 9:
        raise ProtocolError(f"invalid BCD byte 0x{byte:02x}")
    return hi * 10 + lo


# --- battery ---------------------------------------------------------------

def parse_battery(reply: bytes) -> BatteryStatus:
    if len(reply) != 1:
        raise ProtocolError(f"battery: expected 1 byte, got {reply.hex(' ')!r}")
    return BatteryStatus(reply[0])


# --- set time --------------------------------------------------------------

def build_set_time(dt: datetime) -> bytes:
    if not 2000 <= dt.year <= 2099:
        raise ValueError("year must be in 2000..2099")
    return bytes(
        [0x58] + [to_bcd(v) for v in (dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute, dt.second)]
    )


# --- setup -----------------------------------------------------------------

_LED_NIBBLE = {LedCycle.S10: 1, LedCycle.S20: 2, LedCycle.S30: 3, LedCycle.OFF: 4}
_UNIT_BITS = {IntervalUnit.SECONDS: 1, IntervalUnit.MINUTES: 2, IntervalUnit.HOURS: 3}

FLAG_MANUAL_START = 0x80
FLAG_NO_CIRCULAR = 0x40
FLAG_NO_ALARM_LED = 0x20
FLAG_FAHRENHEIT = 0x10
FLAG_TMAX_NEGATIVE = 0x08
FLAG_TMIN_NEGATIVE = 0x04


def build_setup(cfg: LoggerConfig) -> bytes:
    """Encode a :class:`LoggerConfig` into the 7-byte setup command."""
    flags = _UNIT_BITS[cfg.interval.unit]
    if cfg.start_mode is StartMode.MANUAL:
        flags |= FLAG_MANUAL_START
    if not cfg.circular:
        flags |= FLAG_NO_CIRCULAR
    if not cfg.alarm_led:
        flags |= FLAG_NO_ALARM_LED
    if cfg.temp_unit is TempUnit.FAHRENHEIT:
        flags |= FLAG_FAHRENHEIT
    if cfg.temp_alarm_high < 0:
        flags |= FLAG_TMAX_NEGATIVE
    if cfg.temp_alarm_low < 0:
        flags |= FLAG_TMIN_NEGATIVE
    return bytes(
        [
            _LED_NIBBLE[cfg.led_cycle] << 4 | 0x08,
            flags,
            cfg.interval.value,
            abs(cfg.temp_alarm_high),
            abs(cfg.temp_alarm_low),
            cfg.rh_alarm_high,
            cfg.rh_alarm_low,
        ]
    )


# --- header ----------------------------------------------------------------

_HEADER_UNITS = {0x60: IntervalUnit.SECONDS, 0x30: IntervalUnit.MINUTES, 0x20: IntervalUnit.HOURS}
_SIGNS = {0x78: (1, 1), 0x76: (1, -1), 0x77: (-1, -1)}  # (sign of Tmax, sign of Tmin)
_TEMP_UNITS = {0x23: TempUnit.CELSIUS, 0x24: TempUnit.FAHRENHEIT}


def parse_header(reply: bytes) -> LoggerInfo:
    if len(reply) != HEADER_SIZE:
        raise ProtocolError(f"header: expected {HEADER_SIZE} bytes, got {len(reply)}")
    if reply[0] != 0xDD or reply[7] != 0xCE or reply[20] != 0xAA:
        raise ProtocolError(f"header: bad markers in {reply.hex(' ')}")
    size = int.from_bytes(reply[1:3], "big")
    if size % BYTES_PER_SAMPLE or size > MEMORY_SIZE:
        raise ProtocolError(f"header: implausible log size {size}")
    try:
        unit = _HEADER_UNITS[reply[5]]
    except KeyError:
        raise ProtocolError(f"header: unknown interval unit 0x{reply[5]:02x}") from None
    y, mo, d, h, mi, s = (from_bcd(b) for b in reply[8:14])
    try:
        start = datetime(2000 + y, mo, d, h, mi, s)
    except ValueError as e:
        raise ProtocolError(f"header: invalid start time {reply[8:14].hex(' ')}") from e
    sign_hi, sign_lo = _SIGNS.get(reply[19], (1, 1))
    temp_unit = _TEMP_UNITS.get(reply[18])
    if temp_unit is None:
        raise ProtocolError(f"header: unknown temperature unit 0x{reply[18]:02x}")
    return LoggerInfo(
        sample_count=size // BYTES_PER_SAMPLE,
        interval=Interval(reply[6], unit),
        start_time=start,
        temp_unit=temp_unit,
        temp_alarm_high=sign_hi * reply[14],
        temp_alarm_low=sign_lo * reply[15],
        rh_alarm_high=reply[16],
        rh_alarm_low=reply[17],
        overrun_marker=int.from_bytes(reply[3:5], "big"),
    )


# --- data ------------------------------------------------------------------

def build_read_chunk(index: int) -> bytes:
    if not 0 <= index < CHUNK_COUNT:
        raise ValueError(f"chunk index must be in 0..{CHUNK_COUNT - 1}")
    return bytes([0x68] * 6 + [index])


def chunks_needed(sample_count: int) -> int:
    nbytes = sample_count * BYTES_PER_SAMPLE
    return -(-nbytes // CHUNK_SIZE)


def parse_records(data: bytes, info: LoggerInfo) -> list[Reading]:
    """Decode concatenated chunk data into readings (only the first ``info.sample_count``)."""
    needed = info.sample_count * BYTES_PER_SAMPLE
    if len(data) < needed:
        raise ProtocolError(f"data: need {needed} bytes, got {len(data)}")
    step = info.interval.timedelta
    readings = []
    for i in range(info.sample_count):
        off = i * BYTES_PER_SAMPLE
        t = int.from_bytes(data[off : off + 2], "big", signed=True) / 10
        h = int.from_bytes(data[off + 2 : off + 4], "big", signed=True) / 10
        readings.append(Reading(i, info.start_time + i * step, t, h))
    return readings
