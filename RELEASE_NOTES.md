Second release. The big one is that the dac supports seven filter types, not
one, and the official app only ever exposes peaking.

## Download

**TanchjImport.exe** — Windows 64-bit, standalone. No Python needed.

It's about 47 MB now, up from the first release. That's PySide6, the whole Qt
runtime ships inside the exe. First launch is a little slow because onefile
builds unpack to a temp folder every time.

## What's new

- **All 7 filter types.** Low shelf, high shelf, low pass, high pass and
  bandpass are all implemented in the firmware and all work. AutoEQ profiles
  with shelves now import as they are, no more faking them with peaks.
- **Hardware settings.** Output gain, reconstruction filter (all 5), Class AB/H
  output mode, DRE, mic gain. Independent from the EQ, so changing one never
  disturbs the other.
- **Reads the dac on launch.** The app shows what's actually on the device
  instead of assuming, using the 0x80 direction bit.
- **A real PEQ editor.** Drag a band on the response curve to move it, scroll
  to change Q, and the dac follows live.
- **Profiles.** EQ plus every hardware setting saved together, so one click from
  the tray restores a whole state.
- New UI on PySide6, replacing customtkinter and pystray.

## Warning: channel balance

An earlier build had channel balance on group 0x16. My decode was wrong and
writing it caused a sudden volume jump, loud enough to be dangerous. It's
removed and documented in the README as a warning instead. Use the official app
if you need balance.

## First run

Windows SmartScreen may warn you because the exe is unsigned. Click **More
info** → **Run anyway**. Code signing certificates cost money, so this is normal
for a small open source tool. If you'd rather not trust the binary, the source
is right here and builds in one command, see BUILD_EXE.md.

Some antivirus software flags PyInstaller onefile builds as a false positive. If
yours does, add an exclusion or build from source.

## Notes

- Preamp is whole dB only, a device restriction, so `-5.9 dB` is applied as `-6 dB`
- Presets and profiles live in `%APPDATA%\SpaceProEQ\presets.json` and `profiles.json`
- Tick "Start with Windows" *after* moving the exe to its final location, it records the exact path

## Tested on

One Space Pro unit, one firmware version, Windows only. Reports from other
setups are very welcome, open an issue.

The USB protocol is documented in the README if you want to build a version for
another platform.

---

SHA256: E7B073FEE6F2FF8B85A957424D500FB01F1C42C52ADB2F184B698F9FAF864DAA
