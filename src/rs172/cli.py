"""Command line interface: ``rs172 --help``."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import click

from . import __version__, export
from .device import RS172, AmbiguousPortError, DeviceNotFoundError, RS172Error, find_ports, probe
from .models import (
    CAPACITY,
    Interval,
    IntervalUnit,
    LedCycle,
    LoggerConfig,
    LoggerInfo,
    LogData,
    StartMode,
    TempUnit,
)
from .protocol import ProtocolError

LED_CHOICES = {"10": LedCycle.S10, "20": LedCycle.S20, "30": LedCycle.S30, "off": LedCycle.OFF}
UNIT_CHOICES = {u.value: u for u in IntervalUnit}


class Ctx:
    def __init__(self, port: str | None):
        self.port = port

    def open(self) -> RS172:
        return RS172.open(self.port)


def _err(msg: str) -> None:
    click.secho(f"Error: {msg}", fg="red", err=True)


class _Group(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (AmbiguousPortError, DeviceNotFoundError) as e:
            _err(f"{e} (use -p PORT or RS172_PORT; 'rs172 ports' lists the candidates)")
            sys.exit(1)
        except (RS172Error, ProtocolError) as e:
            _err(str(e))
            sys.exit(1)


@click.group(cls=_Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-p", "--port", envvar="RS172_PORT", help="Serial port (COM3, /dev/ttyUSB0). Default: autodetect. Env: RS172_PORT.")
@click.option("-v", "--verbose", count=True, help="Show debug output (-vv for raw bytes).")
@click.version_option(__version__)
@click.pass_context
def main(ctx: click.Context, port: str | None, verbose: int) -> None:
    """Control an RS PRO RS-172 temperature/humidity data logger."""
    logging.basicConfig(level=[logging.WARNING, logging.INFO, logging.DEBUG][min(verbose, 2)],
                        format="%(levelname)s %(name)s: %(message)s")
    ctx.obj = Ctx(port)


@main.command()
@click.option("--probe", "do_probe", is_flag=True,
              help="Send each port the battery query to see whether an RS-172 answers (does not interrupt logging).")
def ports(do_probe: bool) -> None:
    """List serial ports with the logger's USB bridge (CP210x, 10C4:EA60).

    Nothing is sent to the ports unless --probe is given.
    """
    found = find_ports()
    if not found:
        click.echo("No CP210x USB-UART (10C4:EA60) found; is the logger plugged in?")
        sys.exit(1)
    status = {True: "RS-172 answered", False: "no answer (not an RS-172?)", None: "cannot open (in use?)"}
    for port in found:
        click.echo(f"{port:<14} {status[probe(port)]}" if do_probe else port)


@main.command()
@click.pass_obj
def battery(obj: Ctx) -> None:
    """Show the battery power status."""
    with obj.open() as dev:
        click.echo(f"Battery: {dev.battery()}")


@main.command("set-time")
@click.option("--time", "when", type=click.DateTime(["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]),
              help="Time to set (default: this computer's local time).")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_obj
def set_time(obj: Ctx, when: datetime | None, yes: bool) -> None:
    """Correct the logger clock.

    Note: setting the clock ends the running log and erases its data. A new log starts
    immediately with automatic start, or on the next button press with manual start.
    """
    if not yes:
        click.confirm("Setting the clock ends the running log and erases the data stored on the logger "
                      "(a new log starts right away with automatic start, or on the button press with "
                      "manual start). Continue?", abort=True)
    with obj.open() as dev:
        t = dev.set_time(when)
    click.echo(f"Logger clock set to {t:%Y-%m-%d %H:%M:%S}")


def _print_info(info: LoggerInfo) -> None:
    last = info.end_time
    click.echo(f"Start time:     {info.start_time:%Y-%m-%d %H:%M:%S}")
    click.echo(f"Last time:      {last:%Y-%m-%d %H:%M:%S}" if last else "Last time:      -")
    click.echo(f"Sample rate:    {info.interval}")
    pct = 100 * info.sample_count / CAPACITY
    click.echo(f"Samples:        {info.sample_count} / {CAPACITY} ({pct:.1f}% of memory used)")
    u = info.temp_unit.value
    click.echo(f"Temp alarm:     {info.temp_alarm_low} .. {info.temp_alarm_high} °{u}")
    click.echo(f"RH alarm:       {info.rh_alarm_low} .. {info.rh_alarm_high} %RH")


def _print_stats(log: LogData) -> None:
    t, h = log.temperature_stats(), log.humidity_stats()
    if t is None or h is None:
        return
    fmt = "%Y-%m-%d %H:%M:%S"
    click.echo(f"Temp max/min:   {t.max.value:.1f} °C @ {t.max.time:{fmt}} / {t.min.value:.1f} °C @ {t.min.time:{fmt}}, avg {t.avg:.2f} °C")
    click.echo(f"RH max/min:     {h.max.value:.1f} %RH @ {h.max.time:{fmt}} / {h.min.value:.1f} %RH @ {h.min.time:{fmt}}, avg {h.avg:.2f} %RH")


def _confirm_stop(yes: bool) -> None:
    """Reading the log header stops a running log; ask first (on stderr, so stdout stays clean)."""
    if not yes:
        click.confirm("Reading from the logger stops a running recording "
                      "(run setup again to restart it). Continue?", abort=True, err=True)


_yes_option = click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")


@main.command()
@_yes_option
@click.pass_obj
def info(obj: Ctx, yes: bool) -> None:
    """Show the log header (sample count, rate, start time, alarms).

    Note: this stops a running recording; you are asked to confirm unless -y is given.
    """
    _confirm_stop(yes)
    with obj.open() as dev:
        _print_info(dev.read_info())


def _download(dev: RS172) -> LogData:
    with click.progressbar(length=1, label="Downloading", file=sys.stderr) as bar:
        def progress(done: int, total: int) -> None:
            bar.length = max(total, 1)
            bar.update(done - bar.pos)
        return dev.download(progress=progress)


def _write(log: LogData, output: Path, fmt: str | None) -> None:
    fmt = fmt or ("json" if output.suffix.lower() == ".json" else "csv")
    (export.to_json if fmt == "json" else export.to_csv)(log, output)
    click.echo(f"Saved {len(log)} readings to {output}", err=True)


@main.command()
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path),
              help="Write readings to this file (.csv or .json). Without it, CSV is printed to stdout.")
@click.option("-f", "--format", "fmt", type=click.Choice(["csv", "json"]), help="Output format (default: from file extension, else csv).")
@_yes_option
@click.pass_obj
def download(obj: Ctx, output: Path | None, fmt: str | None, yes: bool) -> None:
    """Download all readings from the logger.

    Note: this stops a running recording; you are asked to confirm unless -y is given.
    """
    _confirm_stop(yes)
    with obj.open() as dev:
        log = _download(dev)
    if output:
        _print_info(log.info)
        _print_stats(log)
        _write(log, output, fmt)
    else:
        click.echo(export.to_json(log) if fmt == "json" else export.to_csv(log), nl=False)


def _apply_config(obj: Ctx, cfg: LoggerConfig, yes: bool, download_first: Path | None, sync_time: bool,
                  always_confirm: bool = False, command: str | None = None) -> None:
    click.echo(f"Sample rate:    {cfg.interval}  (memory full after {cfg.record_duration})")
    click.echo(f"LED flash:      {'no light' if cfg.led_cycle is LedCycle.OFF else f'{cfg.led_cycle.value} s'}")
    click.echo(f"Start:          {cfg.start_mode.value}")
    click.echo(f"Memory full:    {'overwrite oldest (circular)' if cfg.circular else 'stop logging'}")
    u = cfg.temp_unit.value
    click.echo(f"Temp alarm:     {cfg.temp_alarm_low} .. {cfg.temp_alarm_high} °{u}")
    click.echo(f"RH alarm:       {cfg.rh_alarm_low} .. {cfg.rh_alarm_high} %RH")
    click.echo(f"Alarm LED:      {'on' if cfg.alarm_led else 'off'}")
    if command:
        click.echo("\nThe same setup without questions:")
        click.secho(f"  {command}\n", bold=True)
    if always_confirm:
        note = f"Stored data is downloaded to {download_first} first, then erased." if download_first \
            else "This permanently erases all data stored on the logger."
        click.confirm(f"{note} Write these settings to the logger?", abort=True)
    elif not yes and not download_first:
        click.confirm("Setup permanently erases all data stored on the logger. Continue?", abort=True)
    with obj.open() as dev:
        if download_first:
            _write(_download(dev), download_first, None)
        dev.setup(cfg, sync_time=sync_time)
    after_full = "the oldest samples are overwritten" if cfg.circular else "logging stops"
    if cfg.start_mode is StartMode.AUTOMATIC:
        full = cfg.memory_full_time(datetime.now().replace(microsecond=0))
        click.echo("Logger configured and logging.")
        click.echo(f"Memory full at {full:%Y-%m-%d %H:%M:%S}; after that {after_full}.")
    else:
        click.echo("Logger configured; press the logger's button to start logging.")
        click.echo(f"Memory full {cfg.record_duration} after the button press; after that {after_full}.")


def _quote(arg: str) -> str:
    return f'"{arg}"' if not arg or any(c.isspace() or c in "\"'&|;<>()$`" for c in arg) else arg


def _setup_command(port: str | None, o: dict, download_first: Path | None, sync_time: bool) -> str:
    """The non-interactive ``rs172 setup`` command line for the options ``o``."""
    args = ["rs172"]
    if port:
        args += ["-p", port]
    args += [
        "setup", "-i", str(o["interval"]), "-u", o["unit"], "--led", o["led"], "--start", o["start"],
        "--circular" if o["circular"] else "--no-circular", "--temp-unit", o["temp_unit"].lower(),
        "--temp-low", str(o["temp_low"]), "--temp-high", str(o["temp_high"]),
        "--rh-low", str(o["rh_low"]), "--rh-high", str(o["rh_high"]),
        "--alarm-led" if o["alarm_led"] else "--no-alarm-led",
    ]
    if download_first:
        args += ["--download-first", str(download_first)]
    if not sync_time:
        args.append("--no-sync-time")
    return " ".join(_quote(a) for a in args)


def _config_from_options(o: dict) -> LoggerConfig:
    try:
        return LoggerConfig(
            interval=Interval(o["interval"], UNIT_CHOICES[o["unit"]]),
            led_cycle=LED_CHOICES[o["led"]],
            start_mode=StartMode.AUTOMATIC if o["start"] == "auto" else StartMode.MANUAL,
            circular=o["circular"],
            temp_unit=TempUnit(o["temp_unit"].upper()),
            temp_alarm_low=o["temp_low"],
            temp_alarm_high=o["temp_high"],
            rh_alarm_low=o["rh_low"],
            rh_alarm_high=o["rh_high"],
            alarm_led=o["alarm_led"],
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e


_d = LoggerConfig.default()
_safety_options = [
    click.option("--download-first", type=click.Path(dir_okay=False, path_type=Path),
                 help="Download the stored data to this file before erasing (skips the confirmation)."),
    click.option("--sync-time/--no-sync-time", default=True, show_default=True,
                 help="Set the logger clock to this computer's time before setup."),
    click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation."),
]


def _with_safety(f):
    for opt in reversed(_safety_options):
        f = opt(f)
    return f


# Help texts shared by the command line options and the interactive prompts.
HELP = {
    "unit": "Sample rate unit.",
    "interval": "Sample rate value.",
    "led": "LED flash cycle in seconds ('off' = longest battery life).",
    "start": "manual: start with the logger button; auto: start immediately.",
    "circular": "When memory is full: overwrite oldest data, or stop.",
    "temp_unit": "Unit of the temperature alarm thresholds.",
    "temp_low": "Temperature alarm low.",
    "temp_high": "Temperature alarm high.",
    "rh_low": "Humidity alarm low (%RH).",
    "rh_high": "Humidity alarm high (%RH).",
    "alarm_led": "Flash the LED on high/low alarms.",
}


def _ask(key: str, label: str, default, type=None):
    click.secho(f"  {HELP[key]}", dim=True)
    if isinstance(default, bool):
        return click.confirm(label, default=default)
    return click.prompt(label, default=default, type=type, show_choices=True)


def _interactive_options(o: dict) -> dict:
    """Ask for every setup option; the values given on the command line are the defaults."""
    click.echo("Logger setup. Press Enter to keep the value in [brackets].\n")
    o = dict(o)
    o["unit"] = _ask("unit", "Sample rate unit", o["unit"], click.Choice(list(UNIT_CHOICES)))
    max_iv = UNIT_CHOICES[o["unit"]].max_value
    o["interval"] = _ask("interval", "Sample rate value", min(max(o["interval"], 1), max_iv), click.IntRange(1, max_iv))
    o["led"] = _ask("led", "LED flash cycle", o["led"], click.Choice(list(LED_CHOICES)))
    o["start"] = _ask("start", "Start mode", o["start"], click.Choice(["manual", "auto"]))
    o["circular"] = _ask("circular", "Overwrite oldest data when memory is full (circular)?", o["circular"])
    o["temp_unit"] = _ask("temp_unit", "Temperature unit", o["temp_unit"].lower(),
                          click.Choice(["c", "f"], case_sensitive=False)).lower()
    lo, hi = (-40, 70) if o["temp_unit"] == "c" else (-40, 158)
    u = f"°{o['temp_unit'].upper()}"
    o["temp_low"] = _ask("temp_low", f"Temperature alarm low ({u})", min(max(o["temp_low"], lo), hi), click.IntRange(lo, hi))
    o["temp_high"] = _ask("temp_high", f"Temperature alarm high ({u})", min(max(o["temp_high"], o["temp_low"]), hi),
                          click.IntRange(o["temp_low"], hi))
    o["rh_low"] = _ask("rh_low", "Humidity alarm low (%RH)", min(max(o["rh_low"], 0), 100), click.IntRange(0, 100))
    o["rh_high"] = _ask("rh_high", "Humidity alarm high (%RH)", min(max(o["rh_high"], o["rh_low"]), 100),
                        click.IntRange(o["rh_low"], 100))
    o["alarm_led"] = _ask("alarm_led", "Flash the LED on alarms?", o["alarm_led"])
    click.echo()
    return o


@main.command()
@click.option("-I", "--interactive", is_flag=True,
              help="Ask for every setting (with explanations), show a summary and confirm before writing.")
@click.option("-i", "--interval", type=int, default=_d.interval.value, show_default=True, help=HELP["interval"])
@click.option("-u", "--unit", type=click.Choice(list(UNIT_CHOICES)), default=_d.interval.unit.value, show_default=True,
              help=HELP["unit"])
@click.option("--led", type=click.Choice(list(LED_CHOICES)), default="10", show_default=True, help=HELP["led"])
@click.option("--start", type=click.Choice(["manual", "auto"]), default="manual", show_default=True,
              help=HELP["start"])
@click.option("--circular/--no-circular", default=_d.circular, show_default=True, help=HELP["circular"])
@click.option("--temp-unit", type=click.Choice(["c", "f"], case_sensitive=False), default="c", show_default=True,
              help=HELP["temp_unit"])
@click.option("--temp-low", type=int, default=_d.temp_alarm_low, show_default=True, help=HELP["temp_low"])
@click.option("--temp-high", type=int, default=_d.temp_alarm_high, show_default=True, help=HELP["temp_high"])
@click.option("--rh-low", type=int, default=_d.rh_alarm_low, show_default=True, help=HELP["rh_low"])
@click.option("--rh-high", type=int, default=_d.rh_alarm_high, show_default=True, help=HELP["rh_high"])
@click.option("--alarm-led/--no-alarm-led", default=_d.alarm_led, show_default=True, help=HELP["alarm_led"])
@_with_safety
@click.pass_obj
def setup(obj: Ctx, interactive, download_first, sync_time, yes, **options) -> None:
    """Configure the logger (erases all stored data).

    Example: rs172 setup -i 5 -u min --start auto --temp-low -10 --temp-high 30

    Or answer questions for each setting: rs172 setup --interactive
    """
    command = None
    if interactive:
        options = _interactive_options(options)
        command = _setup_command(obj.port, options, download_first, sync_time)
    _apply_config(obj, _config_from_options(options), yes, download_first, sync_time,
                  always_confirm=interactive, command=command)


@main.command()
@_with_safety
@click.pass_obj
def defaults(obj: Ctx, download_first, sync_time, yes) -> None:
    """Restore factory default settings (erases all stored data)."""
    _apply_config(obj, LoggerConfig.default(), yes, download_first, sync_time)


@main.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path),
              help="Save the plot to this file (.png, .pdf, .svg, ...) instead of opening a window.")
@click.option("--fahrenheit", is_flag=True, help="Show temperatures in °F.")
@click.option("--alarms", is_flag=True, help="Draw the alarm thresholds (JSON input only).")
@click.option("--title", help="Plot title (default: the input file name).")
def plot(file: Path, output: Path | None, fahrenheit: bool, alarms: bool, title: str | None) -> None:
    """Plot a log saved with 'download' (CSV or JSON).

    Needs the plot extra: pip install "rs172[plot]".
    """
    try:
        import matplotlib
    except ImportError:
        _err('plotting needs matplotlib; install it with: pip install "rs172[plot]"')
        sys.exit(1)
    if output is not None:
        matplotlib.use("Agg")  # no window needed, works without a display
    from . import plot as plotting

    try:
        series = plotting.load(file)
    except (ValueError, KeyError) as e:
        raise click.BadParameter(f"cannot read {file}: {e}", param_hint="FILE") from e
    if not len(series):
        _err(f"{file} contains no readings")
        sys.exit(1)
    if alarms and series.alarms is None:
        click.echo("Note: CSV files contain no alarm thresholds; use a JSON export for --alarms.", err=True)
    fig = plotting.plot(series, fahrenheit=fahrenheit, alarms=alarms, title=title or file.name)
    if output is not None:
        fig.savefig(output, dpi=150)
        click.echo(f"Saved plot of {len(series)} readings to {output}")
    else:
        import matplotlib.pyplot as plt

        plt.show()


if __name__ == "__main__":
    main()
