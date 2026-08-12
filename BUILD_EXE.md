# Building TanchjImport into an .exe

Run every command in **PowerShell**. `$py` is your real Python:

```powershell
$py = "C:\Users\artet\AppData\Local\Python\pythoncore-3.14-64\python.exe"
```

## 1. Install the dependencies

```powershell
& $py -m pip install PySide6 hidapi pyinstaller
```

- `PySide6` — the Qt UI, graph rendering and tray icon
- `hidapi` — talks to the DAC
- `pyinstaller` — makes the .exe

## 2. Test it as a script first

Always confirm it runs before freezing it:

```powershell
& $py tanchjimport.py
```

Import a preset, drag a band on the graph, close the window (it should
vanish to the tray), and reopen it from the tray icon. If all that works,
build the exe.

## 3. Build the .exe

```powershell
& $py -m PyInstaller --onefile --noconsole --name "TanchjImport" tanchjimport.py
```

What the flags do:

- `--onefile` — one self-contained .exe instead of a folder of files
- `--noconsole` — no black terminal window behind the GUI
- `--name` — what the .exe is called

The result lands in `dist\TanchjImport.exe`. Move it anywhere you like —
it needs no Python installed to run. The `build` folder and the `.spec`
file are just leftovers; you can delete them.

PySide6 builds are considerably larger than the old customtkinter ones —
expect tens of megabytes. `--onefile` also makes startup slower, since the
whole thing unpacks to a temp folder on each launch. Dropping `--onefile`
gives you a folder that starts much faster.

## 4. Turn on autostart

Run the .exe, tick **Start with Windows**, done.

Important: tick this **after** moving the .exe to its final location. The
setting records the exact path, so if you move the .exe afterwards you
need to untick and re-tick it.

At login the app starts straight into the tray with no window (it passes
`--tray` to itself).

## Where presets and profiles live

```
%APPDATA%\SpaceProEQ\presets.json
%APPDATA%\SpaceProEQ\profiles.json
```

Back those up, or copy them to another PC, to carry your setup over.

## Known annoyances

**Windows SmartScreen** may warn on first run because the .exe is
unsigned. Click *More info* -> *Run anyway*. Code signing certificates
cost money; for a personal tool this is normal.

**Antivirus false positives** happen with PyInstaller one-file builds
fairly often — the self-extracting stub looks odd to heuristics. If your
AV quarantines it, add an exclusion. Dropping `--onefile` usually avoids
this if it becomes a problem.

**Rebuilding**: if you change the .py, just re-run the PyInstaller
command. Untick and re-tick autostart if the .exe path changed.
