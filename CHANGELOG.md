# Changelog

Entries after v0.1.0 are generated from commit messages by
[git-changelog](https://github.com/pawamoy/git-changelog) during `make release`.

<!-- insertion marker -->
## [v0.1.0](https://github.com/fgagarin/ft260_eeprom/releases/tag/v0.1.0) - 2026-10-12

First release.

- `list`, `info`, `dump`, `write`, `restore` for the FT260 configuration EEPROM, through the FT260's own I2C master.
- `write` changes VID/PID, manufacturer, product and serial number; `--serial-mask BF01xxxxxx` generates serials.
- `write --builtin` programs a blank EEPROM from a built-in FT_Prog default image, no FT_Prog needed.
- `udev-rule` installs a udev rule giving a group (plugdev by default) access to the FT260.
- Explains permission problems when the device cannot be opened.
