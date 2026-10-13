# ft260-eeprom

Read and write the configuration EEPROM of the FTDI FT260 ([datasheet](https://ftdichip.com/wp-content/uploads/2024/11/DS_FT260.pdf))
USB-to-I2C/UART bridge from Linux: VID/PID, manufacturer and product strings, serial number.
No FT_Prog, no Windows, no extra programmer.

On power-up the FT260 loads its configuration from an I2C EEPROM at address 0x50. This tool reaches
that EEPROM through the FT260 itself, using its HID I2C master
(FTDI AN_394, "User Guide for FT260").
It was written to give each board in a production run its own serial number.

## Install

From PyPI (needs Python 3.11+, no uv required):

```bash
pipx install ft260-eeprom      # or: pip install ft260-eeprom
ft260-eeprom --help
```

Or run it straight from a checkout with [uv](https://docs.astral.sh/uv/), which installs the
dependencies declared in the script:

```bash
git clone https://github.com/fgagarin/ft260_eeprom.git
cd ft260_eeprom
./ft260_eeprom.py --help
```

The examples below use `ft260-eeprom`; from a checkout, use `./ft260_eeprom.py` instead.

### Device access on Linux

The tool talks to the FT260 through libusb, so your user needs write access to the USB device node
`/dev/bus/usb/BBB/DDD`. Debian and Raspberry Pi OS create these nodes as `root:root 0664`. Install a
udev rule that gives them to group `plugdev`:

```bash
ft260-eeprom udev-rule            # asks for sudo only if something has to change
ft260-eeprom udev-rule -g dialout --mode 0660
ft260-eeprom udev-rule -n         # print the rule only
```

The command is idempotent: run it again and it reports that the rule is up to date. Your user must
be in the group (`sudo usermod -aG plugdev $USER`, then log in again). If the device cannot be
opened, the error message shows the node's owner and mode and the commands to fix them.

## Usage

```bash
ft260-eeprom list                   # FT260 HID interfaces
ft260-eeprom info                   # USB descriptor and fields found in the EEPROM
ft260-eeprom dump -o board.bin      # raw EEPROM image (hex dump without -o)
ft260-eeprom restore board.bin      # write a raw image back
ft260-eeprom gen-serial BF01xxxxxx -c 5
```

Program a board, previewing first with `-n` / `--dry-run`:

```bash
ft260-eeprom write --serial-mask BF01xxxxxx \
    --manufacturer "OIST" --product "Biofactory bioreactor" --dry-run
```

`--serial-mask` replaces each run of `x` with a random [NanoID](https://github.com/ai/nanoid)
(`0-9A-Z` by default) of the same length. Use `--serial` for a fixed value and `--set-vid` /
`--set-pid` to change the USB IDs. Before writing, the old content is saved to
`ft260-eeprom-<timestamp>.bin` in `--backup-dir`. After writing, the tool reads the EEPROM back to
verify it. Replug the board so the FT260 reloads its configuration.

### Blank EEPROM

A new board usually has a blank EEPROM, so there is nothing to edit. In that case the FT260 runs on
its built-in defaults. `--builtin` starts from an image that FT_Prog wrote with default settings and
the serial number enabled. That image is stored in the script for a 256-byte EEPROM with one-byte
addresses (24C02):

```bash
ft260-eeprom info --builtin
ft260-eeprom write --builtin --serial-mask BF01xxxxxx --manufacturer "OIST" --product "Biofactory bioreactor"
```

To use other settings, such as pin functions or power options, program one board with FT_Prog,
`dump -o reference.bin` it, and then use `write --template reference.bin` on the others.

### Options for other boards

`--vid/--pid` select a board that already has a custom VID/PID. With several FT260 connected,
`--path` (from `list`) picks one. `--eeprom-addr`, `--addr-width`, `--size` and `--page-size`
describe the EEPROM. Run `ft260-eeprom <command> --help` for details.

## I2C scanner

`ft260-i2c-scan` (`./ft260_i2c_scan.py` from a checkout) maps the devices on the I2C bus behind the
FT260, like `i2cdetect`. Each address gets a one-byte write and a one-byte read, and the map shows
which of them the device ACKed:

```
RW read and write ACK   R- read only   -W write only   -- no answer   !! bus error, see below the map   == answers on the main bus too

Main bus, multiplexer channels off
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:                         -- -- -- -- -- -- -- --
...
50: RW -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
...
70: RW -- -- -- -- -- -- --
  0x50 RW, 0x70 RW (TCA9548A)
```

If a TCA9548A multiplexer answers at 0x70, the main bus is scanned with all its channels off, then
each channel is switched on alone and scanned. A channel map shows the devices behind that channel;
addresses that answer on the main bus too are marked `==`. A summary lists the devices per channel,
and all channels are switched off at the end.

```bash
ft260-i2c-scan                     # main bus and all 8 multiplexer channels
ft260-i2c-scan -c 0 -c 3           # only channels 0 and 3
ft260-i2c-scan --probe read        # read probes only, writes nothing to the devices
ft260-i2c-scan --mux-addr 0x74     # multiplexer with A2 strapped high
ft260-i2c-scan --no-mux --first 0 --last 0x7f
```

The FT260 cannot send the zero-length write (SMBus quick command) that `i2cdetect` uses, so the write
probe carries one byte, 0x00 by default (`--write-byte`). A device that ACKs receives it: for most
chips that only sets the register pointer, but a PCF8574-style port expander drives its outputs to
it. Use `--probe read` on boards where that matters. A device that ACKs its address but NACKs the
byte, as some sensors do with an unknown command, counts as answering the write. The multiplexer's
own address is never probed, because a byte written to it selects the channels.

`!!` marks an address where the FT260 reported another error: arbitration lost, bus busy, a stuck
line. The FT260 status bits are listed under the map and in the summary, for example
`0x40 !! read: status 0x12 (error, arbitration lost)`.

`--vid`, `--pid`, `--path` and `--i2c-khz` work as in `ft260-eeprom`.

## How it works

FTDI does not publish the byte layout of the configuration image. The tool locates the fields by
their structure and refuses to write when it cannot find them:

- strings are standard USB string descriptors (`bLength`, `0x03`, UTF-16LE text);
- each string has a pointer, an `(offset, bLength)` byte pair in the header, so strings can change
  length and get repacked;
- VID/PID is the little-endian pair the device enumerates with;
- the checksum is FTDI's 16-bit xor/rotate sum, located by trying every position.

Layout of the FT_Prog default image (256-byte EEPROM, one-byte addresses), as identified so far:

| Offset | Default | Field |
|---|---|---|
| 0x02 | `03 04 30 60` | VID 0x0403, PID 0x6030 |
| 0x08 | `A0 32` | probably bmAttributes (bus powered, remote wake-up) and bMaxPower (100 mA) |
| 0x0E, 0x10, 0x12 | | `(offset, bLength)` of manufacturer, product, serial strings |
| 0x2C | | string descriptors, back to back |
| 0xFE | | checksum of 0x00..0xFD |
| 0x00, 0x06, 0x0A, 0x14..0x2B | | unknown, kept as FT_Prog wrote them |

[`ft260_default.bin`](ft260_default.bin) is the FT_Prog dump the built-in image was taken from. CI
checks that the two still match.

## Limitations

- Tested on Linux (Raspberry Pi OS) with a 24C02 EEPROM. Other platforms supported by hidapi may
  work, but `udev-rule` and the permission hints are Linux only.
- The built-in image covers only a 256-byte EEPROM with one-byte addresses. For other EEPROMs, use
  `--template` with a dump of a board programmed by FT_Prog.
- eFUSE is not supported.

## License

[MIT](LICENSE), © 2026 Okinawa Institute of Science and Technology School Corporation (OIST).
Written by Fedor Gagarin.
