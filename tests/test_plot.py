import builtins
from pathlib import Path

import pytest
from click.testing import CliRunner

from rs172 import cli, to_csv, to_json
from rs172.device import RS172

from test_device import FakeLogger

pytest.importorskip("matplotlib")
import matplotlib

matplotlib.use("Agg")

from rs172 import plot as plotting  # noqa: E402

DATA = Path(__file__).parent / "data"


@pytest.fixture
def log():
    f = FakeLogger((DATA / "header_17samples.bin").read_bytes(), (DATA / "chunk00_17samples.bin").read_bytes())
    return RS172(f, command_gap=0).download()


@pytest.mark.parametrize("ext", ["csv", "json"])
def test_load_roundtrip(log, tmp_path, ext):
    path = tmp_path / f"log.{ext}"
    (to_csv if ext == "csv" else to_json)(log, path)
    s = plotting.load(path)
    assert len(s) == 17
    assert s.times[0] == log.readings[0].time
    assert s.temperature_c == [r.temperature_c for r in log.readings]
    assert s.humidity == [r.humidity for r in log.readings]
    if ext == "json":
        assert s.alarms == plotting.Alarms(0, 40, 30, 90)
    else:
        assert s.alarms is None


def test_load_rejects_unknown_type(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("")
    with pytest.raises(ValueError):
        plotting.load(p)


@pytest.mark.parametrize("extra", [[], ["--fahrenheit", "--alarms"]])
def test_plot_cli_saves_png(log, tmp_path, extra):
    src = tmp_path / "log.json"
    to_json(log, src)
    out = tmp_path / "log.png"
    r = CliRunner().invoke(cli.main, ["plot", str(src), "-o", str(out), *extra], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "17 readings" in r.output


def test_plot_cli_without_matplotlib(log, tmp_path, monkeypatch):
    src = tmp_path / "log.csv"
    to_csv(log, src)
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = CliRunner().invoke(cli.main, ["plot", str(src), "-o", str(tmp_path / "x.png")])
    assert r.exit_code == 1
    assert 'pip install "rs172[plot]"' in r.output


def _series(temps, rh):
    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1)
    return plotting.Series([t0 + timedelta(minutes=i) for i in range(len(temps))], temps, rh)


@pytest.mark.parametrize("fahrenheit, span", [(False, 4), (True, 7.2)])
def test_minimum_y_span(fahrenheit, span):
    fig = plotting.plot(_series([22.0, 22.4, 22.1], [33.0, 34.0, 33.5]), fahrenheit=fahrenheit)
    ax_t, ax_h = fig.axes
    lo, hi = ax_t.get_ylim()
    assert hi - lo == pytest.approx(span)
    assert lo < (22.0 * 9 / 5 + 32 if fahrenheit else 22.0)  # data stays centred inside
    assert ax_h.get_ylim() == pytest.approx((31, 36))


def test_rh_span_stays_within_0_100():
    fig = plotting.plot(_series([20.0, 21.0], [98.0, 99.5]))
    assert fig.axes[1].get_ylim() == pytest.approx((95, 100))


def test_wide_range_untouched():
    fig = plotting.plot(_series([0.0, 30.0], [20.0, 80.0]))
    lo, hi = fig.axes[0].get_ylim()
    assert hi - lo > 30
