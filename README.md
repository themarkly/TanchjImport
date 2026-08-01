# TanchjImport

Import AutoEQ / Wavelet parametric EQ files straight into a **Tanchjim Space Pro** USB DAC, save them as presets, and switch between them from the Windows system tray.

The official app makes you dial in all ten bands by hand. This lets you point at a `.txt` file and be done.

> Not affiliated with Tanchjim. Use at your own risk.

---

## Features

- Loads standard AutoEQ / Wavelet `ParametricEQ.txt` files
- Saves named presets and switches between them in one click from the tray
- Optional shelf-to-peak conversion (the Space Pro has no shelf filters)
- Reset-to-flat button
- Start with Windows, launching hidden in the tray
- Clamps out-of-range values instead of sending garbage to the device

## Install

Download `SpaceProEQ.exe` from [Releases](../../releases) and run it. Nothing else needed.

Or run from source:

```powershell
pip install hidapi pystray pillow
python space_pro_eq.py
```

Building your own `.exe`:

```powershell
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name "SpaceProEQ" space_pro_eq.py
```

## Usage

1. Get a PEQ file — [AutoEQ](https://github.com/jaakkopasanen/AutoEq), [peqdb.com](https://peqdb.com), or your own.
2. **Add from file...**, give it a name.
3. **Apply to DAC**, or pick it from the tray icon later.

Presets live in `%APPDATA%\SpaceProEQ\presets.json`. Copy that file to move presets between machines.

Expected file format:

```
Preamp: -5.9 dB
Filter 1: ON PK Fc 20 Hz Gain 1.92 dB Q 1.700
Filter 2: ON PK Fc 169 Hz Gain -2.86 dB Q 0.750
...
```

## The shelf problem

The Space Pro only supports **peak/bell filters**. AutoEQ profiles normally use low- and high-shelf filters at the frequency extremes, so they can't be represented exactly.

By default this app converts shelves to peaks: it moves the filter an octave past the corner frequency (low shelf → half, high shelf → double) and caps Q at 0.7, so the peak's skirt covers the region the shelf would have held flat. Converted bands are marked in the table.

It's an approximation, not a match. If you'd rather they were skipped, untick the conversion option — you'll get a warning per skipped filter instead.

For best results, regenerate your profile with AutoEQ constrained to peak-only filters rather than converting a shelf-based one after the fact.

---

## Protocol

Reverse engineered from USB captures (Wireshark + USBPcap) of the official Windows app. Documented here so anyone can build a Linux/macOS/Android/C version.

**Device:** VID `0x3302`, PID `0x4307` — HID interrupt OUT endpoint `0x05`, report ID `0x4B`, 64-byte reports.

### Band set — `4B 01 09 18 00 <band 0-9>`

| Offset | Field | Encoding |
|---|---|---|
| `[5]` | Band index | 0–9 |
| `[8:28]` | Coefficient blob | **Ignored by firmware** — see below |
| `[28:30]` | Frequency | uint16 LE, raw Hz |
| `[30:32]` | Q | uint16 LE, value ÷ 256 |
| `[32:34]` | Gain | int16 LE, value ÷ 256 dB |
| `[34:36]` | Filter type | always `2` (peak) |
| `[36:38]` | Unknown | 20 / 0 / 5 in different contexts |

### Other commands

| Command | Meaning |
|---|---|
| `4B 01 0A 04 00 00 FF FF` | Commit after band writes |
| `4B 01 03 02 00 <int8 dB>` | Master preamp gain (whole dB only) |
| `4B 01 04 00` | Commit after preamp |
| `4B 01 01 00` | Sent at end of a preset load (save/persist) |
| `4B 80 ...` | Reads/queries — `0x80` is the read bit |

Write order used by the official app: 10× band set → band commit → preamp → preamp commit → save.

### About that coefficient blob

Bytes `[8:28]` carry what look like biquad filter coefficients, and they change wildly with every parameter tweak — which sent me down a long rabbit hole trying to match them against the RBJ audio EQ cookbook at various sample rates. Nothing fit.

It turned out not to matter: **zeroing all 20 bytes and sending only frequency, Q and gain applies the EQ correctly**, and the official app then displays the new settings. The firmware computes its own coefficients; the blob is app-side bookkeeping.

The frequency was never encoded in there at all — it's the plain uint16 at `[28:30]`, in raw Hz.

### Verification

- Frequency: 14/14 labeled test points (200 Hz – 20 kHz) plus all 10 factory band defaults (31–16000 Hz)
- Q: typed values 1–5 → exactly 256/512/768/1024/1280; factory default 181 = 0.707
- Gain: ±10 through ±6 dB in 1 dB steps, exact, no rounding error

---

## Known limits and unknowns

- **Preamp is whole dB only** — a device restriction. `-5.9 dB` is sent as `-6 dB`.
- **±12 dB gain clamp is a guess.** Captures only ever proved ±10 dB.
- **`[36:38]` is unexplained.** Varies by context with no observed audible effect.
- **`4B 01 01 00`** is assumed to be save/persist; not conclusively confirmed.
- Tested on **one unit, one firmware version, Windows only.** Reports from other setups welcome.

## Contributing

Issues and PRs welcome — especially from other Space Pro owners. Filling in the unknowns above, or confirming behaviour on different firmware, would be the most useful thing.

## License

MIT

## Credits

Protocol reverse-engineered by [@themarkly](https://github.com/themarkly). Implementation written with AI assistance.
# TanchjImport
Python script to Import any EQ file to Tanchjim Space Pro DAC, as the official App does not provide such option.
