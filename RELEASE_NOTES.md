First release.

Import AutoEQ / Wavelet parametric EQ files into a Tanchjim Space Pro USB DAC, save them as presets, and switch between them from the Windows system tray.

## Download

**SpaceProEQ.exe** — Windows 64-bit, standalone. No Python needed.

## Features

- Loads standard AutoEQ / Wavelet `ParametricEQ.txt` files
- Named presets, switchable in one click from the tray icon
- Optional shelf-to-peak conversion (the Space Pro has no shelf filters)
- Reset-to-flat
- Optional start with Windows, launching hidden in the tray

## First run

Windows SmartScreen may warn you because the executable is unsigned — click **More info** → **Run anyway**. Code signing certificates cost money; this is normal for a small open-source tool. If you'd rather not trust the binary, the source is right here and builds in one command (see the README).

Some antivirus software flags PyInstaller one-file builds as a false positive. If yours does, add an exclusion or build from source.

## Notes

- Preamp is whole dB only — a device restriction, so `-5.9 dB` is applied as `-6 dB`
- Presets are stored in `%APPDATA%\SpaceProEQ\presets.json`
- Tick "Start with Windows" *after* moving the .exe to its final location — it records the exact path

## Tested on

One Space Pro unit, one firmware version, Windows only. Reports from other setups are very welcome — open an issue.

The USB protocol is documented in the README if you want to build a version for another platform.

---

SHA256: F6EE4F2A135A90A98D81CE30B787F80785D0E8A33D7787485F0B8D5805B338B4
