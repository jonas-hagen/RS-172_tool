import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from rs172 import cli
from rs172 import protocol as p
from rs172.device import RS172
from rs172.models import Interval, IntervalUnit, LoggerConfig

from test_device import FakeLogger

DATA = Path(__file__).parent / "data"


@pytest.fixture
def fake(monkeypatch):
    f = FakeLogger((DATA / "header_17samples.bin").read_bytes(), (DATA / "chunk00_17samples.bin").read_bytes())
    monkeypatch.setattr(RS172, "open", classmethod(lambda cls, port=None, **kw: cls(f, command_gap=0)))
    return f


def run(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input, catch_exceptions=False)


def test_battery(fake):
    r = run("battery")
    assert r.exit_code == 0 and "70%-100%" in r.output


def test_info(fake):
    r = run("info", "-y")
    assert "Samples:        17 / 16350 (0.1% of memory used)" in r.output
    assert "Start time:     2026-11-24 09:20:31" in r.output
    assert "Last time:      2026-11-24 09:36:31" in r.output
    assert "End time" not in r.output  # reading stops logging, so no projection here


def test_download_csv_file(fake, tmp_path):
    out = tmp_path / "d.csv"
    r = run("download", "-y", "-o", str(out))
    assert r.exit_code == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "index,time,temperature_c,temperature_f,humidity_rh"
    assert lines[1] == "0,2026-11-24T09:20:31,23.7,74.66,42.9"
    assert len(lines) == 18
    assert "avg 23.36 °C" in r.output


def test_download_json_file(fake, tmp_path):
    out = tmp_path / "d.json"
    run("download", "-y", "-o", str(out))
    d = json.loads(out.read_text())
    assert d["info"]["sample_count"] == 17
    assert d["stats"]["humidity_rh"]["max"] == 42.9
    assert len(d["readings"]) == 17


def test_setup_requires_confirmation(fake):
    r = run("setup", input="n\n")
    assert r.exit_code == 1
    assert fake.sent == []


def test_setup(fake):
    r = run("setup", "-i", "5", "-u", "min", "--start", "auto", "--led", "off", "--temp-low", "-10", "-y")
    assert r.exit_code == 0, r.output
    assert fake.sent[0][0] == 0x58
    # LED off, flags 0x40 (no circular) | 0x04 (Tmin negative) | 0x02 (minutes)
    assert fake.sent[1].hex() == "484605280a5a1e"
    # 5-minute interval: last sample 16349 x 5 min = 56 days 18:25 after now
    full = datetime.now().replace(microsecond=0) + timedelta(minutes=5 * 16349)
    line = next(l for l in r.output.splitlines() if l.startswith("Memory full at "))
    shown = datetime.strptime(line[15:34], "%Y-%m-%d %H:%M:%S")
    assert abs(shown - full) < timedelta(seconds=5)
    assert line.endswith("after that logging stops.")


def test_setup_manual_reports_duration(fake):
    r = run("setup", "--circular", "-y", "--no-sync-time")
    assert "press the logger's button" in r.output
    assert "Memory full 1 day, 21:25:00 after the button press; after that the oldest samples are overwritten." in r.output


def test_setup_download_first(fake, tmp_path):
    out = tmp_path / "backup.csv"
    r = run("setup", "--download-first", str(out), "--no-sync-time")
    assert r.exit_code == 0
    assert out.exists()
    assert fake.sent == [p.CMD_HEADER, p.build_read_chunk(0), p.build_setup(LoggerConfig.default())]


def test_setup_invalid(fake):
    r = run("setup", "--rh-low", "95", "-y")
    assert r.exit_code == 2 and fake.sent == []


def test_defaults(fake):
    run("defaults", "-y", "--no-sync-time")
    assert fake.sent == [p.build_setup(LoggerConfig.default())]


def test_setup_interactive(fake):
    answers = "\n".join([
        "min",   # unit
        "5",     # interval
        "off",   # led
        "auto",  # start
        "n",     # circular
        "c",     # temp unit
        "-10",   # temp low
        "",      # temp high -> keep default 40
        "",      # rh low -> 30
        "",      # rh high -> 90
        "y",     # alarm led
        "y",     # confirm
    ]) + "\n"
    r = run("setup", "--interactive", "--no-sync-time", input=answers)
    assert r.exit_code == 0, r.output
    assert HELP_LED in r.output  # explanations are shown
    assert "memory full after 56 days" in r.output  # summary before confirmation
    assert fake.sent == [bytes.fromhex("484605280a5a1e")]


def test_setup_interactive_rejects_and_reasks(fake):
    answers = "s\n99\n30\n10\nmanual\nn\nc\n-50\n0\n-5\n40\n30\n90\ny\ny\n"
    r = run("setup", "-I", "--no-sync-time", input=answers)
    assert r.exit_code == 0, r.output
    assert "not in the range" in r.output
    cfg = LoggerConfig(interval=Interval(30, IntervalUnit.SECONDS))
    assert fake.sent == [p.build_setup(cfg)]


def test_setup_interactive_abort_writes_nothing(fake):
    r = run("setup", "-I", "-y", input="\n" * 11 + "n\n")
    assert r.exit_code == 1
    assert fake.sent == []


HELP_LED = cli.HELP["led"]


@pytest.mark.parametrize("cmd", ["info", "download"])
def test_reading_asks_before_stopping_recording(fake, cmd):
    r = run(cmd, input="n\n")
    assert r.exit_code == 1
    assert "stops a running recording" in r.output
    assert fake.sent == []  # nothing was sent to the logger


def test_download_stdout_confirmed(fake):
    r = CliRunner().invoke(cli.main, ["download"], input="y\n", catch_exceptions=False)
    assert r.exit_code == 0
    assert r.stdout.startswith("index,time,")  # prompt went to stderr, CSV stays clean
    assert "stops a running recording" in r.stderr


def test_set_time_asks_first(fake):
    r = run("set-time", input="n\n")
    assert r.exit_code == 1
    assert "ends the running log and erases the data" in r.output
    assert fake.sent == []


def test_battery_does_not_ask(fake):
    r = run("battery")  # battery queries do not interrupt logging, so no prompt
    assert r.exit_code == 0 and "Continue?" not in r.output


def test_setup_interactive_shows_equivalent_command(fake, monkeypatch):
    answers = "min\n5\noff\nauto\nn\nc\n-10\n\n\n\ny\ny\n"
    r = run("setup", "-I", "--no-sync-time", input=answers)
    assert r.exit_code == 0, r.output
    cmd = ("rs172 setup -i 5 -u min --led off --start auto --no-circular --temp-unit c "
           "--temp-low -10 --temp-high 40 --rh-low 30 --rh-high 90 --alarm-led --no-sync-time")
    assert cmd in r.output
    # running the printed command writes exactly the same frame
    sent = list(fake.sent)
    fake.sent.clear()
    r2 = run(*cmd.split()[1:], "-y")
    assert r2.exit_code == 0, r2.output
    assert fake.sent == sent


def test_setup_command_quotes_paths_and_keeps_port():
    o = dict(interval=10, unit="s", led="10", start="manual", circular=True, temp_unit="F",
             temp_low=-40, temp_high=158, rh_low=0, rh_high=100, alarm_led=False)
    cmd = cli._setup_command("COM3", o, Path("my backup.csv"), True)
    assert cmd.startswith("rs172 -p COM3 setup -i 10 -u s ")
    assert "--circular --temp-unit f --temp-low -40 --temp-high 158" in cmd
    assert cmd.endswith('--no-alarm-led --download-first "my backup.csv"')
