#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.2",
#     "hidapi>=0.14",
#     "nanoid>=2.0",
# ]
# ///
"""Read and write the FT260 configuration EEPROM: VID/PID, USB strings, serial number.

On power-up the FT260 loads its configuration from an I2C EEPROM at 0x50.  This
script reaches that EEPROM through the FT260 itself, using its HID I2C master
(AN_394), so no extra programmer is needed.

FTDI does not publish the byte layout of the configuration image (only FT_Prog
knows it).  The script finds the fields inside an existing image by their
structure and refuses to write when it cannot:

* strings are standard USB string descriptors: bLength, 0x03, UTF-16LE text;
* pointers to them are (offset[|0x80], bLength) byte pairs before the strings;
* VID/PID is the little-endian pair the device enumerates with;
* the checksum is FTDI's 16-bit xor/rotate sum, located by trying every position.

Installed from PyPI it is the `ft260-eeprom` command; from a source checkout,
`./ft260_eeprom.py` runs it through uv with the dependencies declared above.

Run `info` first and check that it found every field you want to change.

A blank EEPROM has nothing to edit.  `write --builtin` starts from an image
FT_Prog wrote with default settings (serial number enabled, 256-byte EEPROM
with one-byte addresses), stored in this script with its field map:
  ft260-eeprom write --builtin --serial-mask BF01xxxxxx \
      --manufacturer OIST --product "Biofactory bioreactor"
then replug the board.  For other settings (pins, power), program one board
with FT_Prog, `dump -o reference.bin` it and use `write --template reference.bin`.

Linux access: the `hid` module talks libusb, so it needs write access to the
USB device node /dev/bus/usb/BBB/DDD (it detaches hid-ft260 while open).  Debian
creates those nodes as root:root 0664; `ft260-eeprom udev-rule` installs
/etc/udev/rules.d/60-ft260.rules that gives them to group plugdev (via sudo).
"""

import grp
import os
import pwd
import re
import shlex
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Self

import click
import hid
from nanoid import generate as nanoid_generate

__version__ = "0.1.0"

FT260_VID = 0x0403
FT260_PID = 0x6030
I2C_INTERFACE = 0  # interface 0 is the I2C bridge, 1 is the UART

# HID reports, AN_394 "User Guide for FT260"
REPORT_SYSTEM_SETTING = 0xA1
REPORT_I2C_STATUS = 0xC0
REPORT_I2C_READ_REQUEST = 0xC2
REPORT_I2C_DATA_FIRST = 0xD0  # 0xD0..0xDE carry 4..60 data bytes
REPORT_I2C_DATA_LAST = 0xDE
REPORT_MAX_DATA = 60
SYS_I2C_RESET = 0x20
SYS_SET_I2C_CLOCK = 0x22

I2C_NONE = 0x00
I2C_START = 0x02
I2C_REPEATED_START = 0x03
I2C_STOP = 0x04
I2C_START_AND_STOP = 0x06

STATUS_BUSY = 0x01
STATUS_ERROR = 0x02
STATUS_ADDRESS_NACK = 0x04
STATUS_DATA_NACK = 0x08
STATUS_BITS = {
    0x01: "controller busy",
    0x02: "error",
    0x04: "address NACK",
    0x08: "data NACK",
    0x10: "arbitration lost",
    0x20: "controller idle",
    0x40: "bus busy",
}

STRING_NAMES = ("manufacturer", "product", "serial")
FILL_BYTES = (0x00, 0xFF)
PRINTABLE = frozenset(map(chr, range(0x20, 0x7F)))
MAX_STRING_CHARS = 126  # bLength is one byte
DEFAULT_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Plain FTDI sum, and the FT-X variant that skips the user area 0x24..0x7F.
CHECKSUM_SKIPS: tuple[tuple[int, int] | None, ...] = (None, (0x24, 0x80))
UDEV_RULES_FILE = Path("/etc/udev/rules.d/60-ft260.rules")
SYSFS_USB = Path("/sys/bus/usb/devices")


class FT260Error(click.ClickException):
    pass


class OpenError(FT260Error):
    pass


def describe_status(status: int) -> str:
    names = ", ".join(n for bit, n in STATUS_BITS.items() if status & bit)
    return f"status 0x{status:02X} ({names})"


class I2CError(FT260Error):
    def __init__(self, status: int):
        super().__init__(f"I2C transfer failed, {describe_status(status)}")
        self.status = status


# --------------------------------------------------------------------------- FT260 I2C


class FT260:
    """I2C master of an FT260, driven through its HID reports."""

    def __init__(self, path: bytes, khz: int):
        self._dev = hid.device()
        try:
            self._dev.open_path(path)
        except OSError as e:
            raise OpenError(f"cannot open {path.decode()}: {e}") from e
        self._drain()
        self._dev.send_feature_report([REPORT_SYSTEM_SETTING, SYS_SET_I2C_CLOCK, khz & 0xFF, khz >> 8])

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self._dev.close()

    def _drain(self) -> None:
        self._dev.set_nonblocking(1)
        while self._dev.read(64):
            pass
        self._dev.set_nonblocking(0)

    def _send(self, report: list[int]) -> None:
        if self._dev.write(report) < 0:
            raise FT260Error(
                f"HID write of report 0x{report[0]:02X} failed ({self._dev.error()}); "
                "the FT260 stopped accepting requests, replug it"
            )

    def reset(self) -> None:
        self._dev.send_feature_report([REPORT_SYSTEM_SETTING, SYS_I2C_RESET])

    def wait(self, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while (status := self._dev.get_feature_report(REPORT_I2C_STATUS, 5)[1]) & STATUS_BUSY:
            if time.monotonic() > deadline:
                self.reset()
                raise FT260Error("I2C controller stays busy")
            time.sleep(0.001)
        if status & STATUS_ERROR:
            self.reset()
            raise I2CError(status)

    def write(self, addr: int, data: bytes, flag: int = I2C_START_AND_STOP) -> None:
        chunks = [data[i : i + REPORT_MAX_DATA] for i in range(0, len(data), REPORT_MAX_DATA)] or [b""]
        for i, chunk in enumerate(chunks):
            f = I2C_NONE
            if i == 0:
                f |= flag & I2C_REPEATED_START
            if i == len(chunks) - 1:
                f |= flag & I2C_STOP
            report_id = REPORT_I2C_DATA_FIRST + max(len(chunk) - 1, 0) // 4
            padded = chunk.ljust(4 * (report_id - REPORT_I2C_DATA_FIRST + 1), b"\0")
            self._send([report_id, addr, f, len(chunk), *padded])
            self.wait()

    def read(self, addr: int, n: int, flag: int = I2C_START_AND_STOP) -> bytes:
        self._send([REPORT_I2C_READ_REQUEST, addr, flag, n & 0xFF, n >> 8])
        out = bytearray()
        while len(out) < n:
            report = self._dev.read(64, 1000)
            if not report:
                self.wait()  # raises the NACK if that is why no data came
                raise FT260Error("I2C read timeout")
            if REPORT_I2C_DATA_FIRST <= report[0] <= REPORT_I2C_DATA_LAST:
                out += bytes(report[2 : 2 + report[1]])
        self.wait()
        return bytes(out)

    def probe(self, addr: int, write: bytes | None = None) -> bool:
        """Address `addr` with a 1-byte read, or a write of `write`; True if it ACKs.

        The FT260 cannot do zero-length writes (SMBus quick command), so a write
        probe has to send data, which the device takes if it ACKs.  A NACK on
        that data still counts: the device ACKed its address and only refused
        the byte.  Other bus errors raise I2CError.
        """
        if write == b"":
            raise ValueError("the FT260 cannot do zero-length writes")
        try:
            if write is None:
                self._send([REPORT_I2C_READ_REQUEST, addr, I2C_START_AND_STOP, 1, 0])
                self.wait()
                self._dev.read(64, 100)  # the byte read, discarded
            else:
                self.write(addr, write)
        except I2CError as e:
            if e.status & STATUS_ADDRESS_NACK:
                self._drain()
                return False
            if write is not None and e.status & STATUS_DATA_NACK:
                return True
            raise
        return True


@dataclass
class Eeprom:
    """24Cxx-style I2C EEPROM behind the FT260."""

    bus: FT260
    i2c_addr: int
    addr_width: int
    page_size: int
    write_ms: float = 10

    def _target(self, offset: int) -> tuple[int, bytes]:
        if self.addr_width == 1:  # 24C04/08/16 put address bits 8..10 into the slave address
            return self.i2c_addr + (offset >> 8), bytes([offset & 0xFF])
        return self.i2c_addr, offset.to_bytes(2, "big")

    @staticmethod
    def _spans(offset: int, n: int, boundary: int) -> Iterator[tuple[int, int]]:
        end = offset + n
        while offset < end:
            step = min(end - offset, boundary - offset % boundary)
            yield offset, step
            offset += step

    def read(self, offset: int, n: int) -> bytes:
        out = bytearray()
        for off, k in self._spans(offset, n, 256):
            addr, address_bytes = self._target(off)
            self.bus.write(addr, address_bytes, I2C_START)
            out += self.bus.read(addr, k, I2C_REPEATED_START | I2C_STOP)
        return bytes(out)

    def write(self, offset: int, data: bytes) -> None:
        for off, k in self._spans(offset, len(data), self.page_size):
            addr, address_bytes = self._target(off)
            self.bus.write(addr, address_bytes + data[off - offset : off - offset + k])
            time.sleep(self.write_ms / 1000)

    def write_changed(self, old: bytes, new: bytes) -> int:
        """Write only the pages of `new` that differ from `old`; return their count."""
        pages = 0
        for start in range(0, len(new), self.page_size):
            chunk = new[start : start + self.page_size]
            if old[start : start + self.page_size] != chunk:
                self.write(start, chunk)
                pages += 1
        return pages


# --------------------------------------------------------------------------- image layout


@dataclass
class StringDesc:
    offset: int
    value: str
    pointer: int | None = None  # offset of the (offset, bLength) pair that points here
    pointer_bit: int = 0  # bit FTDI ORs into the stored offset

    @property
    def length(self) -> int:
        return 2 + 2 * len(self.value)

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass
class Checksum:
    offset: int  # the 16-bit sum is stored here and covers bytes [0, offset)
    skip: tuple[int, int] | None  # byte range left out of the sum


@dataclass
class Layout:
    strings: dict[str, StringDesc]
    vid_pid: int | None
    checksum: Checksum | None
    checksum_candidates: list[Checksum]


def encode_string(value: str) -> bytes:
    return bytes([2 + 2 * len(value), 0x03]) + value.encode("utf-16-le")


def ftdi_checksum(img: bytes, end: int, skip: tuple[int, int] | None = None) -> int:
    c = 0xAAAA
    for i in range(0, end, 2):
        if skip and skip[0] <= i < skip[1]:
            continue
        c ^= img[i] | img[i + 1] << 8
        c = (c << 1 | c >> 15) & 0xFFFF
    return c


def find_string_descriptors(img: bytes) -> list[StringDesc]:
    found, i = [], 0
    while i < len(img) - 3:
        n = img[i]
        if img[i + 1] == 0x03 and n >= 4 and n % 2 == 0 and i + n <= len(img):
            text = img[i + 2 : i + n].decode("utf-16-le", errors="replace")
            if all(c in PRINTABLE for c in text):
                found.append(StringDesc(i, text))
                i += n
                continue
        i += 1
    return found


def find_pointer(img: bytes, desc: StringDesc, limit: int) -> tuple[int | None, int]:
    hits = [
        (j, img[j] ^ (desc.offset & 0xFF))
        for j in range(limit - 1)
        if img[j + 1] == desc.length and img[j] ^ (desc.offset & 0xFF) in (0x00, 0x80)
    ]
    return hits[0] if len(hits) == 1 else (None, 0)


def name_strings(descs: list[StringDesc], live: dict | None) -> dict[str, StringDesc]:
    """Match descriptors to names by the strings the device reports, the rest by order."""
    named: dict[str, StringDesc] = {}
    if live:
        for name in STRING_NAMES:
            match = [d for d in descs if live.get(name) and d.value == live[name] and d not in named.values()]
            if match:
                named[name] = match[0]
    spare = iter(d for d in descs if d not in named.values())
    for name in STRING_NAMES:
        if name not in named and (d := next(spare, None)):
            named[name] = d
    return dict(sorted(named.items(), key=lambda kv: kv[1].offset))


def analyze(img: bytes, live: dict | None = None) -> Layout:
    descs = find_string_descriptors(img)
    header_end = descs[0].offset if descs else len(img)
    for d in descs:
        d.pointer, d.pointer_bit = find_pointer(img, d, header_end)

    vid_pid = None
    pairs = [(live["vid"], live["pid"])] if live else []
    for pair in [*pairs, (FT260_VID, FT260_PID)]:
        pattern = struct.pack("<HH", *pair)
        hits = [i for i in range(header_end - 3) if img[i : i + 4] == pattern]
        if len(hits) == 1:
            vid_pid = hits[0]
            break

    candidates = [
        Checksum(end, skip)
        for skip in CHECKSUM_SKIPS
        for end in range(2, len(img) - 1, 2)
        if ftdi_checksum(img, end, skip) == int.from_bytes(img[end : end + 2], "little")
    ]
    strings_end = max((d.end for d in descs), default=0)
    covering = [c for c in candidates if c.offset >= strings_end] or candidates
    checksum = covering[0] if len(covering) == 1 else None

    return Layout(name_strings(descs, live), vid_pid, checksum, candidates)


# --------------------------------------------------------------------------- built-in image

# Image written by FT_Prog with default settings (DS_FT260 table 9.3) and the
# serial number enabled, for a 256-byte EEPROM with one-byte addresses.  FTDI
# does not document the byte layout; the fields named below were identified in
# that image, the unknown bytes are kept as FT_Prog wrote them.
BUILTIN_HEADER = bytes.fromhex(
    "4531"  # 0x00 unknown
    "0304 3060"  # 0x02 VID, PID (little-endian)
    "0800"  # 0x06 unknown
    "a032"  # 0x08 probably bmAttributes (bus powered, remote wake-up) and bMaxPower (100 mA)
    "4654 4449"  # 0x0A unknown, reads "FTDI"
    "2c0a 360c 4212"  # 0x0E (offset, bLength) of the manufacturer, product and serial strings
    "6020 cfbe"  # 0x14 unknown, probably pin settings
    "0000 0000 0000 0000"  # 0x18 unknown
    "4000 0000 0000 0000 0000 0000"  # 0x20 unknown; strings follow at 0x2C
)
BUILTIN_SIZE = 256
BUILTIN_VID_PID = 0x02
BUILTIN_POINTERS = {"manufacturer": 0x0E, "product": 0x10, "serial": 0x12}
BUILTIN_STRINGS = {"manufacturer": "FTDI", "product": "FT260", "serial": "FT260I2C"}
BUILTIN_CHECKSUM = Checksum(0xFE, None)  # FTDI sum of 0x00..0xFD


def builtin_image() -> tuple[bytes, Layout]:
    img = bytearray(BUILTIN_SIZE)
    img[: len(BUILTIN_HEADER)] = BUILTIN_HEADER
    strings, pos = {}, len(BUILTIN_HEADER)
    for name, ptr in BUILTIN_POINTERS.items():
        blob = encode_string(BUILTIN_STRINGS[name])
        img[pos : pos + len(blob)] = blob
        img[ptr], img[ptr + 1] = pos, len(blob)
        strings[name] = StringDesc(pos, BUILTIN_STRINGS[name], ptr)
        pos += len(blob)
    cs = BUILTIN_CHECKSUM
    img[cs.offset : cs.offset + 2] = ftdi_checksum(img, cs.offset, cs.skip).to_bytes(2, "little")
    return bytes(img), Layout(strings, BUILTIN_VID_PID, cs, [cs])


def is_blank(img: bytes) -> bool:
    return all(b == img[0] for b in img) and img[0] in FILL_BYTES


def relayout_strings(out: bytearray, layout: Layout, values: dict[str, str]) -> None:
    """Repack all strings back to back when a length changes, and fix their pointers."""
    descs = list(layout.strings.values())
    if missing := [n for n, d in layout.strings.items() if d.pointer is None]:
        raise FT260Error(
            f"no pointer found for {', '.join(missing)}: string lengths cannot change, "
            "keep every string at its current length"
        )
    for a, b in pairwise(descs):
        if any(x not in FILL_BYTES for x in out[a.end : b.offset]):
            raise FT260Error(f"unknown data between strings at 0x{a.end:02X}, lengths cannot change")

    start, end = descs[0].offset, descs[-1].end
    cs = layout.checksum
    limit = cs.offset if cs and cs.offset >= end else len(out)
    fill = out[end] if end < limit and out[end] in FILL_BYTES else 0x00
    while end < limit and out[end] in FILL_BYTES:
        end += 1
    needed = sum(2 + 2 * len(values[n]) for n in layout.strings)
    if start + needed > end:
        raise FT260Error(f"strings need {needed} bytes, only {end - start} available at 0x{start:02X}")

    out[start:end] = bytes([fill]) * (end - start)
    pos = start
    for name, d in layout.strings.items():
        blob = encode_string(values[name])
        out[pos : pos + len(blob)] = blob
        out[d.pointer] = (pos | d.pointer_bit) & 0xFF
        out[d.pointer + 1] = len(blob)
        pos += len(blob)


def build_image(
    img: bytes,
    layout: Layout,
    *,
    vid: int | None = None,
    pid: int | None = None,
    strings: dict[str, str | None],
    allow_no_checksum: bool = False,
) -> bytes:
    out = bytearray(img)

    if vid is not None or pid is not None:
        if layout.vid_pid is None:
            raise FT260Error("VID/PID not found in the image, cannot change them")
        cur_vid, cur_pid = struct.unpack_from("<HH", out, layout.vid_pid)
        struct.pack_into("<HH", out, layout.vid_pid, cur_vid if vid is None else vid, cur_pid if pid is None else pid)

    changed = {n: v for n, v in strings.items() if v is not None}
    for name in changed:
        if name not in layout.strings:
            raise FT260Error(
                f"no {name} string descriptor in the image; start from the built-in image with --builtin "
                "or from an FT_Prog reference with --template"
            )
    if any(len(v) != len(layout.strings[n].value) for n, v in changed.items()):
        relayout_strings(out, layout, {n: changed.get(n, d.value) for n, d in layout.strings.items()})
    else:
        for name, value in changed.items():
            blob = encode_string(value)
            off = layout.strings[name].offset
            out[off : off + len(blob)] = blob

    if layout.checksum:
        cs = layout.checksum
        out[cs.offset : cs.offset + 2] = ftdi_checksum(out, cs.offset, cs.skip).to_bytes(2, "little")
    elif not allow_no_checksum and bytes(out) != img:
        raise FT260Error(
            "checksum location not identified (see `info`); the FT260 would likely reject the image. "
            "Use --no-checksum to write anyway"
        )
    return bytes(out)


def check_image(new: bytes, layout: Layout, expected: dict[str, str]) -> None:
    """Re-parse a built image and make sure it says what we meant."""
    values = [d.value for d in find_string_descriptors(new)]
    for name, value in expected.items():
        if value not in values:
            raise FT260Error(f"internal check failed: {name} {value!r} not found in the new image")
    if (cs := layout.checksum) and ftdi_checksum(new, cs.offset, cs.skip) != int.from_bytes(
        new[cs.offset : cs.offset + 2], "little"
    ):
        raise FT260Error("internal check failed: checksum does not verify")


# --------------------------------------------------------------------------- helpers


def generate_serial(mask: str, alphabet: str) -> str:
    """Replace every run of lowercase 'x' in `mask` with a NanoID of the same length."""
    if "x" not in mask:
        raise click.BadParameter(f"mask {mask!r} has no 'x' placeholders")
    return re.sub(r"x+", lambda m: nanoid_generate(alphabet, len(m[0])), mask)


def validate_string(name: str, value: str) -> str:
    if bad := set(value) - PRINTABLE:
        raise click.BadParameter(f"{name} has non-printable/non-ASCII characters: {sorted(bad)!r}")
    if not 1 <= len(value) <= MAX_STRING_CHARS:
        raise click.BadParameter(f"{name} must be 1..{MAX_STRING_CHARS} characters")
    return value


def hexdump(data: bytes) -> str:
    lines = []
    for off in range(0, len(data), 16):
        row = data[off : off + 16]
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"{off:04X}  {row[:8].hex(' '):<23}  {row[8:].hex(' '):<23}  {text}")
    return "\n".join(lines)


def live_info(dev: dict) -> dict:
    return {
        "vid": dev["vendor_id"],
        "pid": dev["product_id"],
        "manufacturer": dev.get("manufacturer_string") or "",
        "product": dev.get("product_string") or "",
        "serial": dev.get("serial_number") or "",
    }


def describe(img: bytes, layout: Layout) -> list[str]:
    if is_blank(img):
        return [f"EEPROM is blank (all 0x{img[0]:02X}): `write --builtin` writes the default image"]
    lines = []
    if layout.vid_pid is None:
        lines.append("  VID/PID       not found")
    else:
        vid, pid = struct.unpack_from("<HH", img, layout.vid_pid)
        lines.append(f"  VID/PID       {vid:04X}:{pid:04X}  @0x{layout.vid_pid:02X}")
    for name in STRING_NAMES:
        if d := layout.strings.get(name):
            ptr = f"pointer @0x{d.pointer:02X}" if d.pointer is not None else "pointer not found"
            lines.append(f"  {name:<13} {d.value!r}  @0x{d.offset:02X}, {d.length} bytes, {ptr}")
        else:
            lines.append(f"  {name:<13} not found")
    if cs := layout.checksum:
        skip = f", skips 0x{cs.skip[0]:02X}..0x{cs.skip[1] - 1:02X}" if cs.skip else ""
        value = int.from_bytes(img[cs.offset : cs.offset + 2], "little")
        lines.append(f"  checksum      0x{value:04X} @0x{cs.offset:02X} (FTDI sum{skip})")
    elif layout.checksum_candidates:
        where = ", ".join(f"0x{c.offset:02X}" for c in layout.checksum_candidates)
        lines.append(f"  checksum      ambiguous, matches at {where}: writing refused")
    else:
        lines.append("  checksum      not found: writing refused")
    return lines


# --------------------------------------------------------------------------- CLI


class IntParam(click.ParamType):
    """Integer accepting 0x.. / 0o.. / decimal."""

    name = "int"

    def __init__(self, bits: int):
        self.bits = bits

    def convert(self, value, param, ctx):
        if isinstance(value, int):
            return value
        try:
            n = int(value, 0)
        except ValueError:
            self.fail(f"{value!r} is not an integer", param, ctx)
        if not 0 <= n < 1 << self.bits:
            self.fail(f"{value} does not fit in {self.bits} bits", param, ctx)
        return n


class OctalMode(click.ParamType):
    name = "octal"

    def convert(self, value, param, ctx) -> int:
        if isinstance(value, int):
            return value
        if not re.fullmatch(r"0?[0-7]{3}", value):
            self.fail(f"{value!r} is not an octal mode like 0660", param, ctx)
        return int(value, 8)


HEX8, HEX16 = IntParam(8), IntParam(16)
OCTAL_MODE = OctalMode()
IMAGE_PATH = click.Path(exists=True, dir_okay=False, path_type=Path)


@dataclass
class DeviceConfig:
    vid: int
    pid: int
    path: str | None
    i2c_khz: int


@dataclass
class Config(DeviceConfig):
    eeprom_addr: int
    addr_width: int
    size: int
    page_size: int


def find_device(cfg: DeviceConfig) -> dict:
    if cfg.path:
        devs = [d for d in hid.enumerate() if d["path"].decode() == cfg.path]
    else:
        devs = [d for d in hid.enumerate(cfg.vid, cfg.pid) if d["interface_number"] in (I2C_INTERFACE, -1)]
    if not devs:
        raise FT260Error(
            f"no FT260 I2C interface found ({cfg.vid:04X}:{cfg.pid:04X}); try `{self_command(cfg, 'list')}`, "
            "--vid/--pid"
        )
    if len(devs) > 1:
        paths = "\n  ".join(d["path"].decode() for d in devs)
        raise FT260Error(f"several FT260 found, choose one with --path:\n  {paths}")
    return devs[0]


@contextmanager
def open_bus(cfg: DeviceConfig) -> Iterator[tuple[dict, FT260]]:
    dev = find_device(cfg)
    try:
        bus = FT260(dev["path"], cfg.i2c_khz)
    except OpenError as e:
        e.message += "\n\n" + open_failure_hint(dev["path"].decode(), cfg)
        raise
    with bus:
        yield dev, bus


@contextmanager
def open_eeprom(cfg: Config) -> Iterator[tuple[dict, Eeprom]]:
    with open_bus(cfg) as (dev, bus):
        yield dev, Eeprom(bus, cfg.eeprom_addr, cfg.addr_width, cfg.page_size)


# --------------------------------------------------------------------------- Linux access


def usb_node(path: str) -> Path | None:
    """Device node behind a hidapi path: libusb gives "1-2:1.0", hidraw gives /dev/hidrawN."""
    if path.startswith("/dev/"):
        return Path(path)
    try:
        bus, dev = (int((SYSFS_USB / path.split(":")[0] / f).read_text()) for f in ("busnum", "devnum"))
    except (OSError, ValueError):
        return None
    return Path(f"/dev/bus/usb/{bus:03d}/{dev:03d}")


def usb_nodes(ids: list[tuple[int, int]]) -> list[Path]:
    """Device nodes of the connected USB devices with these VID/PIDs."""
    nodes = []
    for d in sorted(SYSFS_USB.glob("*")):
        try:
            vid, pid = (int((d / f).read_text(), 16) for f in ("idVendor", "idProduct"))
        except (OSError, ValueError):
            continue
        if (vid, pid) in ids and (node := usb_node(d.name)):
            nodes.append(node)
    return nodes


def node_status(node: Path) -> str:
    st = node.stat()
    try:
        owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except KeyError:
        owner = f"{st.st_uid}:{st.st_gid}"
    return f"{stat.filemode(st.st_mode)} {owner}"


def login_user() -> str:
    return os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name


def in_group(user: str, group: str) -> bool:
    gr = grp.getgrnam(group)
    return gr.gr_gid in os.getgrouplist(user, pwd.getpwnam(user).pw_gid)


def self_command(cfg: DeviceConfig, *args: str) -> str:
    """How to run ft260-eeprom (also from ft260-i2c-scan) with the same --vid/--pid."""
    argv0 = sys.argv[0]
    cmd = [os.path.join(os.path.dirname(argv0), "ft260_eeprom.py") if argv0.endswith(".py") else "ft260-eeprom"]
    if (cfg.vid, cfg.pid) != (FT260_VID, FT260_PID):
        cmd += ["--vid", f"0x{cfg.vid:04X}", "--pid", f"0x{cfg.pid:04X}"]
    return shlex.join([*cmd, *args])


def open_failure_hint(path: str, cfg: DeviceConfig) -> str:
    node = usb_node(path)
    if node is None or not node.exists():
        return f"Could not find the device node of {path}; check `lsusb` and `{self_command(cfg, 'list')}`."
    user = login_user()
    groups = " ".join(grp.getgrgid(g).gr_name for g in os.getgroups())
    lines = [f"Device node: {node} ({node_status(node)}), you are {user} (groups: {groups})."]
    if not os.access(node, os.R_OK | os.W_OK):
        lines += [
            "You have no write access to it.  Diagnose:",
            f"  ls -l {node}",
            f"  cat {UDEV_RULES_FILE}",
            f"  udevadm test $(udevadm info -q path -n {node}) 2>&1 | grep -iE 'rules|mode|group'",
            "Fix: install the udev rule for group plugdev (asks for sudo, safe to rerun):",
            f"  {self_command(cfg, 'udev-rule')}",
        ]
        if not in_group(user, "plugdev"):
            lines.append(f"  sudo usermod -aG plugdev {user}   # then log in again")
        elif grp.getgrnam("plugdev").gr_gid not in os.getgroups():
            lines.append(f"  {user} is in plugdev, but this session started before that: log in again")
    else:
        lines += [
            "Access looks fine, so the device is probably held by another process:",
            f"  sudo lsof {node}",
            f"  sudo fuser -v {node}",
        ]
    return "\n".join(lines)


def udev_rule(ids: list[tuple[int, int]], group: str, mode: int) -> str:
    lines = [
        f"# FT260 access for group {group}, written by ft260_eeprom.py udev-rule.",
        "# hidapi/libusb opens /dev/bus/usb/BBB/DDD and detaches hid-ft260 while in use.",
    ]
    lines += [
        f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="{vid:04x}", '
        f'ATTR{{idProduct}}=="{pid:04x}", GROUP="{group}", MODE="{mode:04o}"'
        for vid, pid in ids
    ]
    return "\n".join(lines) + "\n"


def run_as_root(args: list[str], input: str | None = None) -> None:
    if os.geteuid() != 0:
        args = ["sudo", *args]
    click.echo(f"$ {shlex.join(args)}")
    try:
        subprocess.run(args, input=input, text=True, check=True, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as e:
        raise FT260Error(f"{shlex.join(args)} failed: {e}") from e


def save_backup(img: bytes, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ft260-eeprom-{datetime.now():%Y%m%d-%H%M%S-%f}.bin"
    path.write_bytes(img)
    return path


def program(ee: Eeprom, old: bytes, new: bytes, yes: bool) -> None:
    if not yes:
        click.confirm("Write the EEPROM?", abort=True)
    pages = ee.write_changed(old, new)
    if ee.read(0, len(new)) != new:
        raise FT260Error("verification failed: EEPROM content differs from what was written")
    click.echo(f"Wrote {pages} page(s), verified. Replug the device so the FT260 reloads its configuration.")


@click.group(context_settings={"help_option_names": ["-h", "--help"], "show_default": True})
@click.version_option(__version__)
@click.option("--vid", type=HEX16, default="0x0403", help="VID the FT260 currently enumerates with.")
@click.option("--pid", type=HEX16, default="0x6030", help="PID the FT260 currently enumerates with.")
@click.option("--path", help="Device path of the I2C interface (see `list`), for several FT260.")
@click.option("--eeprom-addr", type=HEX8, default="0x50", help="7-bit I2C address of the EEPROM.")
@click.option(
    "--addr-width",
    type=click.IntRange(1, 2),
    default=1,
    help="Memory address bytes: 1 for 24C02..24C16, 2 for 24C32 and up.",
)
@click.option("--size", type=click.IntRange(16, 65536), default=256, help="Bytes to read.")
@click.option("--page-size", type=click.IntRange(1, 32), default=8, help="EEPROM write page size.")
@click.option("--i2c-khz", type=click.IntRange(60, 3400), default=100, help="I2C clock.")
@click.pass_context
def cli(ctx: click.Context, **opts) -> None:
    """Read and write the FT260 configuration EEPROM (VID/PID, strings, serial number)."""
    ctx.obj = Config(**opts)


@cli.command("list")
@click.pass_obj
def list_cmd(cfg: Config) -> None:
    """List HID interfaces with --vid/--pid (use --vid 0 --pid 0 for all)."""
    devs = hid.enumerate(cfg.vid, cfg.pid)
    if not devs:
        click.echo("nothing found")
    for d in devs:
        click.echo(
            f"{d['vendor_id']:04X}:{d['product_id']:04X} if{d['interface_number']}  {d['path'].decode()}\n"
            f"    manufacturer={d['manufacturer_string']!r} product={d['product_string']!r} serial={d['serial_number']!r}"
        )


@cli.command()
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Save to a file instead of printing a hex dump.",
)
@click.pass_obj
def dump(cfg: Config, output: Path | None) -> None:
    """Read the raw EEPROM."""
    with open_eeprom(cfg) as (_, ee):
        img = ee.read(0, cfg.size)
    if output:
        output.write_bytes(img)
        click.echo(f"{len(img)} bytes saved to {output}")
    else:
        click.echo(hexdump(img))


@cli.command()
@click.option("-f", "--file", "image_file", type=IMAGE_PATH, help="Parse an image file instead of the EEPROM.")
@click.option("-b", "--builtin", is_flag=True, help="Show the built-in default image instead of the EEPROM.")
@click.pass_obj
def info(cfg: Config, image_file: Path | None, builtin: bool) -> None:
    """Show the USB descriptor the device reports and the fields found in the EEPROM."""
    if image_file and builtin:
        raise click.UsageError("--file and --builtin are mutually exclusive")
    if builtin:
        img, layout = builtin_image()
        click.echo(f"Built-in image ({len(img)} bytes):")
        click.echo("\n".join(describe(img, layout)))
        return
    if image_file:
        img, live = image_file.read_bytes(), None
    else:
        with open_eeprom(cfg) as (dev, ee):
            img, live = ee.read(0, cfg.size), live_info(dev)
        click.echo("USB (as enumerated now):")
        click.echo(f"  VID/PID       {live['vid']:04X}:{live['pid']:04X}")
        for name in STRING_NAMES:
            click.echo(f"  {name:<13} {live[name]!r}")
    click.echo(f"EEPROM image ({len(img)} bytes):")
    click.echo("\n".join(describe(img, analyze(img, live))))


@cli.command()
@click.option("-t", "--template", type=IMAGE_PATH, help="Start from this image instead of the current EEPROM content.")
@click.option(
    "-b",
    "--builtin",
    is_flag=True,
    help="Start from the built-in FT_Prog default image instead of the EEPROM content, e.g. for a blank EEPROM.",
)
@click.option("-s", "--serial", help="Serial number to write.")
@click.option("-m", "--serial-mask", help="Generate the serial: each run of 'x' becomes a NanoID, e.g. BF01xxxxxx.")
@click.option("--alphabet", default=DEFAULT_ALPHABET, help="NanoID alphabet for --serial-mask.")
@click.option("--manufacturer", help="Manufacturer string.")
@click.option("--product", help="Product description string.")
@click.option("--set-vid", type=HEX16, help="New USB VID.")
@click.option("--set-pid", type=HEX16, help="New USB PID.")
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), help="Also save the new image here.")
@click.option(
    "--backup-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Where the old EEPROM content is saved before writing.",
)
@click.option("--no-checksum", is_flag=True, help="Write even if the checksum location is unknown.")
@click.option(
    "-n", "--dry-run", is_flag=True, help="Show the changes, write nothing. Needs no device with --template/--builtin."
)
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_obj
def write(
    cfg: Config,
    template: Path | None,
    builtin: bool,
    serial: str | None,
    serial_mask: str | None,
    alphabet: str,
    manufacturer: str | None,
    product: str | None,
    set_vid: int | None,
    set_pid: int | None,
    output: Path | None,
    backup_dir: Path,
    no_checksum: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Change the serial number, strings or VID/PID in the EEPROM."""
    if serial and serial_mask:
        raise click.UsageError("--serial and --serial-mask are mutually exclusive")
    if template and builtin:
        raise click.UsageError("--template and --builtin are mutually exclusive")
    if builtin and (cfg.addr_width != 1 or cfg.size < BUILTIN_SIZE):
        raise FT260Error(f"the built-in image is for EEPROMs with one-byte addresses and --size {BUILTIN_SIZE}")
    if serial_mask:
        serial = generate_serial(serial_mask, alphabet)
    strings = {"manufacturer": manufacturer, "product": product, "serial": serial}
    for name, value in strings.items():
        if value is not None:
            validate_string(name, value)

    with ExitStack() as stack:
        ee, current, live = None, None, None
        if not ((template or builtin) and dry_run):
            dev, ee = stack.enter_context(open_eeprom(cfg))
            live = live_info(dev)
            current = ee.read(0, cfg.size)
        if builtin:
            base, layout = builtin_image()
        elif template:
            base = template.read_bytes()
            if len(base) > cfg.size:
                raise FT260Error(f"template is {len(base)} bytes, larger than --size {cfg.size}")
            layout = analyze(base, None)
        else:
            if is_blank(current):
                raise FT260Error("EEPROM is blank, nothing to edit: start from the built-in image with --builtin")
            base = current
            layout = analyze(base, live)

        new = build_image(base, layout, vid=set_vid, pid=set_pid, strings=strings, allow_no_checksum=no_checksum)
        expected = {n: strings[n] if strings[n] is not None else d.value for n, d in layout.strings.items()}
        check_image(new, layout, expected)

        click.echo("Built-in image:" if builtin else "Before:")
        click.echo("\n".join(describe(base, layout)))
        click.echo("After:")
        click.echo("\n".join(describe(new, analyze(new, None))))
        if output:
            output.write_bytes(new)
            click.echo(f"Image saved to {output}")
        if dry_run:
            return

        old = current[: len(new)]
        if old == new:
            click.echo("EEPROM already holds this image, nothing to write.")
        else:
            click.echo(f"Old content saved to {save_backup(current, backup_dir)}")
            program(ee, old, new, yes)
    if serial is not None:
        click.echo(f"Serial: {serial}")


@cli.command()
@click.argument("image", type=IMAGE_PATH)
@click.option(
    "--backup-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Where the old EEPROM content is saved before writing.",
)
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_obj
def restore(cfg: Config, image: Path, backup_dir: Path, yes: bool) -> None:
    """Write a raw image (from `dump` or a backup) to the EEPROM as is."""
    new = image.read_bytes()
    if len(new) > cfg.size:
        raise FT260Error(f"image is {len(new)} bytes, larger than --size {cfg.size}")
    with open_eeprom(cfg) as (_, ee):
        current = ee.read(0, cfg.size)
        if current[: len(new)] == new:
            click.echo("EEPROM already holds this image, nothing to write.")
            return
        click.echo(f"Old content saved to {save_backup(current, backup_dir)}")
        program(ee, current[: len(new)], new, yes)


@cli.command("udev-rule")
@click.option("-g", "--group", default="plugdev", help="Group that gets access to the FT260.")
@click.option("--mode", type=OCTAL_MODE, default="0660", help="Permissions of the device node.")
@click.option(
    "--rules-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=UDEV_RULES_FILE,
    help="Rules file to write.",
)
@click.option("-n", "--dry-run", is_flag=True, help="Print the rule, change nothing.")
@click.pass_obj
def udev_rule_cmd(cfg: Config, group: str, mode: int, rules_file: Path, dry_run: bool) -> None:
    """Install a udev rule giving GROUP access to the FT260; safe to rerun.

    Covers the stock 0403:6030 and --vid/--pid.  Uses sudo for writing the rule
    and reloading udev, only when something has to change.
    """
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        raise FT260Error(f"group {group!r} does not exist; create it with `sudo groupadd {group}`") from None
    ids = sorted({(FT260_VID, FT260_PID), (cfg.vid, cfg.pid)})
    rule = udev_rule(ids, group, mode)
    if dry_run:
        click.echo(f"{rules_file}:\n{rule}", nl=False)
        return

    try:
        current = rules_file.read_text()
    except FileNotFoundError:
        current = None
    if current == rule:
        click.echo(f"{rules_file} is up to date.")
    else:
        click.echo(f"{'Updating' if current is not None else 'Creating'} {rules_file}:\n{rule}", nl=False)
        run_as_root(["install", "-D", "-m", "644", "/dev/stdin", str(rules_file)], input=rule)
        run_as_root(["udevadm", "control", "--reload-rules"])

    def stale(node: Path) -> bool:
        st = node.stat()
        return st.st_gid != gid or stat.S_IMODE(st.st_mode) != mode

    if current != rule or any(stale(n) for n in usb_nodes(ids)):
        for vid, pid in ids:
            run_as_root(
                [
                    "udevadm",
                    "trigger",
                    "--action=change",
                    "--subsystem-match=usb",
                    f"--attr-match=idVendor={vid:04x}",
                    f"--attr-match=idProduct={pid:04x}",
                ]
            )
        run_as_root(["udevadm", "settle"])

    nodes = usb_nodes(ids)
    for node in nodes:
        click.echo(f"{node}: {node_status(node)}" + ("  (not applied, replug the device)" if stale(node) else ""))
    if not nodes:
        click.echo("No FT260 connected now; the rule applies when one is plugged in.")
    user = login_user()
    if not in_group(user, group):
        click.echo(f"{user} is not in group {group}: sudo usermod -aG {group} {user}, then log in again.")


@cli.command("gen-serial")
@click.argument("mask")
@click.option("-c", "--count", type=click.IntRange(1), default=1, help="How many serials to print.")
@click.option("--alphabet", default=DEFAULT_ALPHABET, help="NanoID alphabet.")
def gen_serial(mask: str, count: int, alphabet: str) -> None:
    """Generate serials from MASK such as BF01xxxxxx; no device needed."""
    for _ in range(count):
        click.echo(generate_serial(mask, alphabet))


if __name__ == "__main__":
    cli()
