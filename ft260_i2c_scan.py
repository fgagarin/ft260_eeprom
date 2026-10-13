#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.2",
#     "hidapi>=0.14",
#     "nanoid>=2.0",
# ]
# ///
"""Scan the I2C bus behind an FT260 and draw a map of the addresses that answer.

Every address gets two probes: a one-byte write and a one-byte read.  The map
shows which of them the device ACKed, so write-only and read-only devices stand
out.  The FT260 cannot send the zero-length write `i2cdetect` uses, so a device
that ACKs the write probe also receives its byte (--write-byte, 0x00): a
register pointer for most chips, but output levels for a PCF8574-style port
expander.  Use --probe read where that matters.  A device that ACKs its address
but NACKs the byte (an unknown command, for example) counts as answering.

Other errors the FT260 reports (arbitration lost, bus busy, ...) show as `!!`,
with the status bits listed under the map.

A TCA9548A multiplexer at 0x70 is scanned channel by channel: the main bus is
scanned with every channel off, then each channel is switched on alone and
scanned again.  A channel map shows only the devices that appeared with that
channel; addresses that also answer on the main bus are marked `==`.  The
multiplexer's own address is never probed, since a byte written to it selects
the channels.  All channels are switched off at the end.

Shares the FT260 driver with ft260_eeprom.py; `ft260-eeprom udev-rule` sets up
device access on Linux.
"""

from dataclasses import dataclass, replace

import click

from ft260_eeprom import (
    FT260,
    HEX8,
    HEX16,
    DeviceConfig,
    FT260Error,
    I2CError,
    __version__,
    describe_status,
    open_bus,
)

MUX_CHANNELS = 8
PROBE_NAMES = {"R": "read", "W": "write"}
CELLS = {
    "RW": ("RW", "green"),
    "R": ("R-", "yellow"),
    "W": ("-W", "yellow"),
    "": ("--", None),
}
ERROR_CELL = ("!!", "red")
MAIN_CELL = ("==", "bright_black")
LEGEND = "RW read and write ACK   R- read only   -W write only   -- no answer   !! bus error, see below the map"
LEGEND_CHANNEL = LEGEND + "   == answers on the main bus too"


@dataclass(frozen=True)
class Answer:
    acks: str = ""  # probes the address ACKed: "R", "W", "RW" or ""
    error: str = ""  # FT260 status of the probes that failed with a bus error
    main: bool = False  # answers on the main bus too, shown in the main map

    @property
    def cell(self) -> tuple[str, str | None]:
        return ERROR_CELL if self.error and not self.main else MAIN_CELL if self.main else CELLS[self.acks]


Scan = dict[int, Answer]


@dataclass
class Mux:
    bus: FT260
    addr: int

    def select(self, mask: int) -> None:
        self.bus.write(self.addr, bytes([mask]))
        if (got := self.bus.read(self.addr, 1)[0]) != mask:
            raise FT260Error(f"TCA9548A at 0x{self.addr:02X} reads back 0x{got:02X} after writing 0x{mask:02X}")


def probe(bus: FT260, addr: int, probes: str, write_byte: int) -> Answer:
    acks, errors = "", []
    for code in probes:
        try:
            if bus.probe(addr, None if code == "R" else bytes([write_byte])):
                acks += code
        except I2CError as e:
            errors.append(f"{PROBE_NAMES[code]}: {describe_status(e.status)}")
    return Answer(acks, "; ".join(errors))


def scan(bus: FT260, addrs: range, probes: str, write_byte: int, known: Scan) -> Scan:
    """Probe `addrs`, except those in `known`, whose answer is given."""
    return {a: known[a] if a in known else probe(bus, a, probes, write_byte) for a in addrs}


def only_new(found: Scan, main: Scan) -> Scan:
    """Mark the addresses that answer on the main bus too."""

    def answers(a: Answer | None) -> bool:
        return a is not None and bool(a.acks or a.error)

    return {a: replace(r, main=True) if answers(r) and answers(main.get(a)) else r for a, r in found.items()}


def render(found: Scan) -> str:
    lines = ["    " + " ".join(f"{c:2x}" for c in range(16))]
    for row in range(0, 0x80, 16):
        cells = []
        for a in range(row, row + 16):
            if a not in found:
                cells.append("  ")
            else:
                text, color = found[a].cell
                cells.append(click.style(text, fg=color, bold=color not in (None, "bright_black")))
        lines.append((f"{row:02x}: " + " ".join(cells)).rstrip())
    return "\n".join(lines)


def describe(found: Scan, names: dict[int, str] | None = None) -> list[str]:
    """Devices found on one line, then a line per address with a bus error."""
    names = names or {}
    devices = [
        f"0x{a:02X} {r.cell[0]}" + (f" ({names[a]})" if a in names else "")
        for a, r in found.items()
        if r.acks and not r.error and not r.main
    ]
    lines = [", ".join(devices) or "nothing"]
    for a, r in found.items():
        if r.error and not r.main:
            acked = f"; {' and '.join(PROBE_NAMES[c] for c in r.acks)} ACK" if r.acks else ""
            lines.append(f"0x{a:02X} {ERROR_CELL[0]} {r.error}{acked}")
    return lines


def show(title: str, found: Scan, names: dict[int, str] | None = None) -> None:
    click.echo(click.style(title, bold=True))
    click.echo(render(found))
    click.echo("".join(f"  {line}\n" for line in describe(found, names)))


@click.command(context_settings={"help_option_names": ["-h", "--help"], "show_default": True})
@click.version_option(__version__)
@click.option("--vid", type=HEX16, default="0x0403", help="VID the FT260 currently enumerates with.")
@click.option("--pid", type=HEX16, default="0x6030", help="PID the FT260 currently enumerates with.")
@click.option("--path", help="Device path of the I2C interface (see `ft260-eeprom list`), for several FT260.")
@click.option("--i2c-khz", type=click.IntRange(60, 3400), default=100, help="I2C clock.")
@click.option("--first", type=HEX8, default="0x08", help="First address to probe.")
@click.option("--last", type=HEX8, default="0x77", help="Last address to probe.")
@click.option(
    "--probe",
    "probes",
    type=click.Choice(["both", "write", "read"]),
    default="both",
    help="Probes to send: one-byte write, one-byte read, or both.",
)
@click.option(
    "--write-byte",
    type=HEX8,
    default="0x00",
    help="Byte the write probe sends; a device that ACKs receives it.",
)
@click.option("--mux-addr", type=HEX8, default="0x70", help="Address of the TCA9548A multiplexer.")
@click.option("--no-mux", is_flag=True, help="Do not look for a multiplexer, scan the main bus only.")
@click.option(
    "-c",
    "--channel",
    "channels",
    type=click.IntRange(0, MUX_CHANNELS - 1),
    multiple=True,
    help="Multiplexer channel to scan, repeatable. Default: all.",
)
def cli(
    vid: int,
    pid: int,
    path: str | None,
    i2c_khz: int,
    first: int,
    last: int,
    probes: str,
    write_byte: int,
    mux_addr: int,
    no_mux: bool,
    channels: tuple[int, ...],
) -> None:
    """Map the I2C devices behind an FT260, including each channel of a TCA9548A multiplexer."""
    if not first <= last <= 0x7F:
        raise click.UsageError("need --first <= --last <= 0x7F")
    addrs = range(first, last + 1)
    probes = {"both": "RW", "write": "W", "read": "R"}[probes]

    with open_bus(DeviceConfig(vid, pid, path, i2c_khz)) as (_, bus):
        mux = None
        if not no_mux:
            if bus.probe(mux_addr, b"\0"):  # 0x00: all channels off
                mux = Mux(bus, mux_addr)
                mux.select(0)
            else:
                click.echo(f"No TCA9548A at 0x{mux_addr:02X}, scanning the main bus only.\n")

        click.echo(f"{LEGEND_CHANNEL if mux else LEGEND}\n")
        names = {mux_addr: "TCA9548A"} if mux else {}
        main = scan(bus, addrs, probes, write_byte, {mux_addr: Answer(probes)} if mux else {})
        show("Main bus" + (", multiplexer channels off" if mux else ""), main, names)
        if not mux:
            return

        found: dict[int, Scan] = {}
        try:
            for ch in channels or range(MUX_CHANNELS):
                mux.select(1 << ch)
                found[ch] = only_new(scan(bus, addrs, probes, write_byte, {mux_addr: Answer(probes)}), main)
                show(f"TCA9548A 0x{mux_addr:02X}, channel {ch}", found[ch])
        finally:
            try:
                mux.select(0)
            except FT260Error as e:
                click.echo(f"Could not switch the multiplexer channels off: {e.message}", err=True)

    click.echo(click.style("Summary", bold=True))
    for title, f in [("main bus", main), *((f"channel {ch}", f) for ch, f in found.items())]:
        first, *errors = describe(f, names if f is main else None)
        click.echo(f"  {title:<11} {first}")
        for line in errors:
            click.echo(f"  {'':<11} {line}")


if __name__ == "__main__":
    cli()
