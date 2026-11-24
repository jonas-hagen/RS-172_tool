"""Serial communication with the RS-172 logger."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import serial
from serial.tools import list_ports

from . import protocol as p
from .models import BatteryStatus, LoggerConfig, LoggerInfo, LogData

log = logging.getLogger(__name__)


class RS172Error(Exception):
    """Base class for logger communication errors."""


class RS172TimeoutError(RS172Error):
    """The logger did not answer (in time)."""


class DeviceNotFoundError(RS172Error):
    """No logger could be found."""


class SerialLike(Protocol):
    timeout: float | None

    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


class AmbiguousPortError(RS172Error):
    """Autodetection found more than one candidate port."""


#: Reply timeout when probing a candidate port during autodetection.
PROBE_TIMEOUT = 1.0


def _open_serial(port: str, timeout: float) -> serial.Serial:
    try:
        return serial.Serial(port, p.BAUDRATE, bytesize=8, parity="N", stopbits=1, timeout=timeout)
    except serial.SerialException as e:
        raise RS172Error(f"cannot open {port}: {e}") from e


def find_ports() -> list[str]:
    """Return serial ports with the RS-172's USB bridge (CP210x, 10C4:EA60).

    Other devices use the same chip, so a port listed here is only a candidate;
    :func:`probe` can check whether a logger answers on it.
    """
    return [
        info.device
        for info in sorted(list_ports.comports(), key=lambda i: i.device)
        if info.vid == p.USB_VID and info.pid == p.USB_PID
    ]


def probe(port: str, timeout: float = PROBE_TIMEOUT) -> bool | None:
    """Check whether an RS-172 answers on ``port`` by sending the battery query.

    The battery query does not interrupt a running log. Returns True if the logger
    answered, False if nothing (valid) came back, None if the port cannot be opened
    (for example because the vendor software holds it).
    """
    try:
        ser = _open_serial(port, timeout)
    except RS172Error:
        return None
    dev = RS172(ser, retries=1)
    try:
        dev.battery()
        time.sleep(dev._gap)  # the next connection's first command must not follow the reply too quickly
        return True
    except RS172Error:
        return False
    finally:
        dev.close()


def detect_port() -> str:
    """Find the logger's port from the USB ID alone; nothing is sent to any port.

    Raises :class:`DeviceNotFoundError` if no CP210x port exists and
    :class:`AmbiguousPortError` if there are several (other devices use the same chip).
    """
    candidates = find_ports()
    if not candidates:
        raise DeviceNotFoundError("no CP210x USB-UART (10C4:EA60) found; is the logger plugged in?")
    if len(candidates) > 1:
        raise AmbiguousPortError(f"several CP210x ports found ({', '.join(candidates)}); choose the logger's port explicitly")
    return candidates[0]


class RS172:
    """Connection to an RS-172 temperature/humidity logger.

    Use as a context manager::

        with RS172.open() as dev:      # autodetect, or RS172.open("COM3") / "/dev/ttyUSB0"
            print(dev.battery())
            data = dev.download()

    Note the side effects of the device (see ``docs/protocol.md``):

    * :meth:`read_info` and :meth:`download` stop a running log.
    * :meth:`set_time` and :meth:`setup` erase the stored log and start a new one.
    """

    #: The device ignores a command that follows its previous reply too quickly.
    COMMAND_GAP = 0.2

    def __init__(self, port: SerialLike, *, retries: int = 2, command_gap: float | None = None):
        self._port = port
        self._retries = retries
        self._gap = self.COMMAND_GAP if command_gap is None else command_gap
        self._last_io = 0.0

    # --- connection --------------------------------------------------------

    @classmethod
    def open(cls, port: str | None = None, *, timeout: float = 3.0, **kwargs) -> RS172:
        """Open the logger on ``port``; autodetect with :func:`detect_port` if ``port`` is None."""
        return cls(_open_serial(port or detect_port(), timeout), **kwargs)

    def close(self) -> None:
        self._port.close()

    def __enter__(self) -> RS172:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- low level ---------------------------------------------------------

    def _transact(self, request: bytes, reply_len: int) -> bytes:
        """Send ``request`` and read exactly ``reply_len`` bytes, retrying on silence."""
        attempts = 1 + self._retries
        for attempt in range(1, attempts + 1):
            wait = self._last_io + self._gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._port.reset_input_buffer()
            log.debug("TX %s", request.hex(" "))
            self._port.write(request)
            reply = self._port.read(reply_len)
            self._last_io = time.monotonic()
            log.debug("RX %s", reply.hex(" ") if len(reply) <= 32 else f"{len(reply)} bytes")
            if len(reply) == reply_len:
                return reply
            if reply:  # partial reply: give the device time to finish, then discard
                time.sleep(0.5)
            log.info("no/short reply to %s (attempt %d/%d)", request.hex(" "), attempt, attempts)
        raise RS172TimeoutError(
            f"no complete reply to {request.hex(' ')} (expected {reply_len} bytes, got {len(reply)})"
        )

    def _command_ack(self, request: bytes, ack: int, what: str) -> None:
        reply = self._transact(request, 1)
        if reply[0] != ack:
            raise p.ProtocolError(f"{what}: expected ack 0x{ack:02x}, got 0x{reply[0]:02x}")

    # --- functions ---------------------------------------------------------

    def battery(self) -> BatteryStatus:
        """Battery power status."""
        return p.parse_battery(self._transact(p.CMD_BATTERY, 1))

    def set_time(self, when: datetime | None = None) -> datetime:
        """Set the logger clock (default: the PC's local time). Returns the time set.

        **Erases the stored log** and restarts logging with the current settings.
        """
        when = (when or datetime.now()).replace(microsecond=0)
        self._command_ack(p.build_set_time(when), p.ACK_SET_TIME, "set time")
        return when

    def read_info(self) -> LoggerInfo:
        """Read the log header (sample count, interval, start time, alarm settings).

        **Stops a running log.**
        """
        return p.parse_header(self._transact(p.CMD_HEADER, p.HEADER_SIZE))

    def download(self, progress: Callable[[int, int], None] | None = None) -> LogData:
        """Download all stored readings. **Stops a running log.**

        ``progress(done_chunks, total_chunks)`` is called after each chunk (each takes ~1.5 s).
        """
        info = self.read_info()
        total = p.chunks_needed(info.sample_count)
        if progress:
            progress(0, total)
        data = bytearray()
        for i in range(total):
            data += self._transact(p.build_read_chunk(i), p.CHUNK_SIZE)
            if progress:
                progress(i + 1, total)
        return LogData(info, p.parse_records(bytes(data), info))

    def setup(self, config: LoggerConfig, *, sync_time: bool = True) -> None:
        """Configure the logger. **Erases all stored data.**

        With ``sync_time`` (default) the clock is set to the PC time first, as the vendor
        software does. With :attr:`StartMode.AUTOMATIC` logging starts immediately.
        """
        if sync_time:
            self.set_time()
        self._command_ack(p.build_setup(config), p.ACK_SETUP, "setup")

    def restore_defaults(self, *, sync_time: bool = True) -> None:
        """Apply the factory default configuration (like the vendor *Default* button)."""
        self.setup(LoggerConfig.default(), sync_time=sync_time)
