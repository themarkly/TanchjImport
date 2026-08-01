# Building Space Pro EQ into an .exe

Run every command in **PowerShell**. `$py` is your real Python:

```powershell
$py = "C:\Users\artet\AppData\Local\Python\pythoncore-3.14-64\python.exe"
```

## 1. Install the dependencies

```powershell
& $py -m pip install hidapi pystray pillow pyinstaller
```

- `hidapi` — talks to the DAC (already installed)
- `pystray` — the system tray icon
- `pillow` — draws the tray icon image
- `pyinstaller` — makes the .exe

## 2. Test it as a script first

Always confirm it runs before freezing it:

```powershell
& $py C:\Users\artet\Downloads\space_pro_eq.py
```

Add a preset, apply it, close the window (it should vanish to the tray),
and reopen it from the tray icon. If all that works, build the exe.

## 3. Build the .exe

```powershell
cd C:\Users\artet\Downloads
& $py -m PyInstaller --onefile --noconsole --name "SpaceProEQ" space_pro_eq.py
```

What the flags do:

- `--onefile` — one self-contained .exe instead of a folder of files
- `--noconsole` — no black terminal window behind the GUI
- `--name` — what the .exe is called

The result lands at:

```
C:\Users\artet\Downloads\dist\SpaceProEQ.exe
```

Move that anywhere you like — Desktop, Program Files, wherever. It needs
no Python installed to run. The `build` folder and `SpaceProEQ.spec` file
are just leftovers; you can delete them.

## 4. Turn on autostart

Run the .exe, tick **Start with Windows (hidden in tray)**, done.

Important: tick this **after** moving the .exe to its final location. The
setting records the exact path, so if you move the .exe afterwards you
need to untick and re-tick it.

At login the app starts straight into the tray with no window.

## Where presets live

```
%APPDATA%\SpaceProEQ\presets.json
```

Back that file up, or copy it to another PC, to carry your presets over.

## Known annoyances

**Windows SmartScreen** may warn on first run because the .exe is
unsigned. Click *More info* -> *Run anyway*. Code signing certificates
cost money; for a personal tool this is normal.

**Antivirus false positives** happen with PyInstaller one-file builds
fairly often — the self-extracting stub looks odd to heuristics. If your
AV quarantines it, add an exclusion. Dropping `--onefile` (producing a
folder instead) usually avoids this if it becomes a problem.

**Rebuilding**: if you change the .py, just re-run the PyInstaller
command. Untick and re-tick autostart if the .exe path changed.
