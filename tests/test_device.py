from datetime import datetime
from pathlib import Path

import pytest

from rs172 import protocol as p
from rs172.device import RS172, RS172TimeoutError
from rs172.models import LoggerConfig

DATA = Path(__file__).parent / "data"


class FakeLogger:
    """Minimal emulation of the device on the byte level."""

    def __init__(self, header: bytes, memory: bytes = b"", ignore_first: int = 0):
        self.timeout = 1
        self.header = header
        self.memory = memory.ljust(p.MEMORY_SIZE, b"\x00")
        self.sent: list[bytes] = []
        self._out = b""
        self._ignore = ignore_first
        self.closed = False

    def reset_input_buffer(self):
        self._out = b""

    def write(self, data: bytes):
        self.sent.append(bytes(data))
        if self._ignore:
            self._ignore -= 1
            return
        if data == p.CMD_BATTERY:
            self._out = b"\x04"
        elif data[0] == 0x58:
            self._out = b"\xee"
        elif data == p.CMD_HEADER:
            self._out = self.header
        elif data[:6] == bytes([0x68] * 6):
            i = data[6] % p.CHUNK_COUNT
            self._out = self.memory[i * p.CHUNK_SIZE : (i + 1) * p.CHUNK_SIZE]
        elif len(data) == 7:
            self._out = b"\x1e" if data[4] <= data[3] or data[1] & 0x0C else b""
        return len(data)

    def read(self, n):
        out, self._out = self._out[:n], self._out[n:]
        return out

    def close(self):
        self.closed = True


def make(fake):
    return RS172(fake, command_gap=0)


def test_battery_and_close():
    fake = FakeLogger(b"")
    with make(fake) as dev:
        assert dev.battery().description == "70%-100%"
    assert fake.closed


def test_download_real_fixture():
    fake = FakeLogger((DATA / "header_17samples.bin").read_bytes(), (DATA / "chunk00_17samples.bin").read_bytes())
    calls = []
    data = make(fake).download(progress=lambda d, t: calls.append((d, t)))
    assert len(data) == 17
    assert data.readings[0].temperature_c == 23.7 and data.readings[0].humidity == 42.9
    assert calls == [(0, 1), (1, 1)]
    assert fake.sent == [p.CMD_HEADER, p.build_read_chunk(0)]


def test_download_straddling_chunks():
    n = 400  # 1600 bytes -> 3 chunks, records straddle chunk borders
    mem = b"".join(int(i).to_bytes(2, "big", signed=True) + int(1000 - i).to_bytes(2, "big") for i in range(n))
    hdr = bytes.fromhex("dd") + (n * 4).to_bytes(2, "big") + bytes.fromhex("0000 6001 ce 261124120000 28005a1e 2378 aa".replace(" ", ""))
    data = make(FakeLogger(hdr, mem)).download()
    assert len(data) == n
    assert [r.temperature_c for r in data.readings] == [i / 10 for i in range(n)]
    assert data.readings[-1].humidity == (1000 - n + 1) / 10
    assert data.end == datetime(2026, 11, 24, 12, 6, 39)


def test_empty_log():
    hdr = bytes.fromhex("dd00000000600 1ce261124120000 28005a1e2378aa".replace(" ", ""))
    fake = FakeLogger(hdr)
    data = make(fake).download()
    assert len(data) == 0 and data.temperature_stats() is None
    assert fake.sent == [p.CMD_HEADER]


def test_retry_when_device_ignores_command():
    fake = FakeLogger(b"", ignore_first=1)
    assert make(fake).battery().raw == 4
    assert len(fake.sent) == 2


def test_timeout():
    fake = FakeLogger(b"", ignore_first=10)
    with pytest.raises(RS172TimeoutError):
        RS172(fake, command_gap=0, retries=1).battery()
    assert len(fake.sent) == 2


def test_setup_sends_time_then_config():
    fake = FakeLogger(b"")
    make(fake).setup(LoggerConfig.default())
    assert fake.sent[0][0] == 0x58
    assert fake.sent[1] == p.build_setup(LoggerConfig.default())


def test_set_time_explicit():
    fake = FakeLogger(b"")
    when = make(fake).set_time(datetime(2026, 1, 2, 3, 4, 5, 999))
    assert when == datetime(2026, 1, 2, 3, 4, 5)
    assert fake.sent == [bytes.fromhex("58260102030405")]


class SilentPort(FakeLogger):
    """A CP210x port with some other device behind it: never answers."""

    def __init__(self):
        super().__init__(b"")

    def write(self, data):
        self.sent.append(bytes(data))
        return len(data)


@pytest.fixture
def ports(monkeypatch):
    """Simulated CP210x ports: name -> fake device, or None for a port that cannot be opened."""
    from rs172 import device

    table: dict = {}
    opened: list[str] = []

    def open_serial(port, timeout):
        opened.append(port)
        if table.get(port) is None:
            raise device.RS172Error(f"cannot open {port}: busy")
        return table[port]

    from rs172 import cli

    monkeypatch.setattr(device, "find_ports", lambda: sorted(table))
    monkeypatch.setattr(cli, "find_ports", lambda: sorted(table))
    monkeypatch.setattr(device, "_open_serial", open_serial)
    monkeypatch.setattr(RS172, "COMMAND_GAP", 0)
    return table, opened


def test_probe(ports):
    from rs172 import device

    table, _ = ports
    table.update(COM3=FakeLogger(b""), COM4=SilentPort(), COM5=None)
    assert device.probe("COM3") is True
    assert table["COM3"].sent == [p.CMD_BATTERY] and table["COM3"].closed
    assert device.probe("COM4") is False
    assert device.probe("COM5") is None


def test_autodetect_single_port_sends_nothing_before_use(ports):
    table, opened = ports
    logger = FakeLogger(b"")
    table.update(COM7=logger)
    with RS172.open() as dev:
        assert opened == ["COM7"] and logger.sent == []  # opened once, no probe
        assert dev.battery().raw == 4


def test_autodetect_refuses_to_guess_without_sending(ports):
    from rs172 import device

    table, opened = ports
    table.update(COM3=SilentPort(), COM7=FakeLogger(b""))
    with pytest.raises(device.AmbiguousPortError, match=r"several CP210x ports found \(COM3, COM7\)"):
        RS172.open()
    assert opened == []
    assert table["COM3"].sent == table["COM7"].sent == []


def test_autodetect_no_cp210x(ports):
    from rs172 import device

    with pytest.raises(device.DeviceNotFoundError, match="is the logger plugged in"):
        RS172.open()


def test_cli_ports(ports):
    from click.testing import CliRunner

    from rs172 import cli

    table, opened = ports
    table.update(COM3=FakeLogger(b""), COM4=SilentPort(), COM5=None)
    r = CliRunner().invoke(cli.main, ["ports"])
    assert r.exit_code == 0 and r.output.split() == ["COM3", "COM4", "COM5"]
    assert opened == []  # plain listing sends nothing

    r = CliRunner().invoke(cli.main, ["ports", "--probe"])
    assert "COM3           RS-172 answered" in r.output
    assert "COM4           no answer" in r.output
    assert "COM5           cannot open" in r.output

    r = CliRunner().invoke(cli.main, ["battery"])
    assert r.exit_code == 1
    assert "Error: several CP210x ports found (COM3, COM4, COM5)" in r.output
    assert "-p PORT" in r.output
