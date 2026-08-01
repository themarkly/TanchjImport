"""
Space Pro EQ
============

Loads AutoEQ / Wavelet style parametric EQ text files, saves them as
named presets, and pushes them to a Tanchjim Space Pro DAC over USB HID.
Lives in the Windows system tray for one-click preset switching.

Protocol (reverse engineered, verified against USB captures):
  Device      VID 0x3302 / PID 0x4307, HID report ID 0x4B, 64-byte reports
  Band set    4B 01 09 18 00 <band 0-9>
                [28:30] frequency   uint16 LE, raw Hz
                [30:32] Q           uint16 LE, value/256
                [32:34] gain        int16  LE, value/256 dB
                [34:36] filter type (always 2 = peak)
                [8:28]  ignored by firmware
  Band commit 4B 01 0A 04 00 00 FF FF
  Preamp      4B 01 03 02 00 <int8 dB>   (whole dB only)
  Preamp cmt  4B 01 04 00
  Save        4B 01 01 00

Not affiliated with Tanchjim. Use at your own risk.
Herobrine in code.
"""

import json
import os
import re
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "Space Pro EQ"
APP_ID = "SpaceProEQ"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

VID = 0x3302
PID = 0x4307

NUM_BANDS = 10
REPORT_LEN = 64

FREQ_MIN, FREQ_MAX = 20, 20000
Q_MIN, Q_MAX = 0.10, 10.0
GAIN_MIN, GAIN_MAX = -12.0, 12.0
PREAMP_MIN, PREAMP_MAX = -12, 12

DEFAULT_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
DEFAULT_Q = 0.71

TRAILER = bytes([0x02, 0x00, 0x05, 0x00])


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "SpaceProEQ")
    os.makedirs(d, exist_ok=True)
    return d


PRESET_FILE = os.path.join(config_dir(), "presets.json")


# --------------------------------------------------------------- parsing

FILTER_RE = re.compile(
    r"Filter\s*(?:\d+)?\s*:\s*(ON|OFF)\s+([A-Z]{2,4})\s+Fc\s+([\d.]+)\s*Hz\s+"
    r"Gain\s+(-?[\d.]+)\s*dB\s+Q\s+([\d.]+)",
    re.IGNORECASE,
)
PREAMP_RE = re.compile(r"Preamp\s*:\s*(-?[\d.]+)\s*dB", re.IGNORECASE)

PEAK_TYPES = {"PK", "PEQ", "BELL"}
LOW_SHELF_TYPES = {"LS", "LSC", "LSQ"}
HIGH_SHELF_TYPES = {"HS", "HSC", "HSQ"}


def shelf_to_peak(freq, gain, q, is_low):
    """Wide peak pushed an octave past the corner - a cheap shelf stand-in."""
    new_freq = freq / 2.0 if is_low else freq * 2.0
    new_freq = max(FREQ_MIN, min(FREQ_MAX, new_freq))
    new_q = max(Q_MIN, min(q, 0.7))
    return new_freq, gain, new_q


def parse_peq_text(text, convert_shelves=True):
    """Returns (preamp_db, [ {freq,gain,q,note} ], [warnings])."""
    warnings = []

    m = PREAMP_RE.search(text)
    preamp = float(m.group(1)) if m else 0.0

    bands = []
    matched_any = False

    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("preamp"):
            continue

        m = FILTER_RE.search(line)
        if not m:
            if line.lower().startswith("filter"):
                warnings.append(f"Could not read: {line[:60]}")
            continue

        matched_any = True
        state, kind, fc, gain, q = m.groups()
        kind = kind.upper()

        if state.upper() == "OFF":
            continue

        fc, gain, q = float(fc), float(gain), float(q)
        note = ""

        if kind in PEAK_TYPES:
            pass
        elif kind in LOW_SHELF_TYPES or kind in HIGH_SHELF_TYPES:
            if not convert_shelves:
                warnings.append(f"Skipped {kind} at {fc:g} Hz (shelves unsupported)")
                continue
            fc, gain, q = shelf_to_peak(fc, gain, q, kind in LOW_SHELF_TYPES)
            note = f"from {kind}"
        else:
            warnings.append(f"Skipped unknown filter type {kind} at {fc:g} Hz")
            continue

        if not (FREQ_MIN <= fc <= FREQ_MAX):
            warnings.append(f"Clamped {fc:g} Hz into {FREQ_MIN}-{FREQ_MAX} Hz")
            fc = max(FREQ_MIN, min(FREQ_MAX, fc))
        if not (Q_MIN <= q <= Q_MAX):
            warnings.append(f"Clamped Q {q:g} into {Q_MIN}-{Q_MAX}")
            q = max(Q_MIN, min(Q_MAX, q))
        if not (GAIN_MIN <= gain <= GAIN_MAX):
            warnings.append(f"Clamped {gain:+g} dB into {GAIN_MIN:+g}/{GAIN_MAX:+g} dB")
            gain = max(GAIN_MIN, min(GAIN_MAX, gain))

        bands.append({"freq": fc, "gain": gain, "q": q, "note": note})

    if not matched_any:
        raise ValueError(
            "No filter lines found. Expected lines like:\n"
            "Filter 1: ON PK Fc 200 Hz Gain -6 dB Q 0.5"
        )

    if len(bands) > NUM_BANDS:
        warnings.append(f"File has {len(bands)} filters; using the first {NUM_BANDS}")
        bands = bands[:NUM_BANDS]

    if not (PREAMP_MIN <= preamp <= PREAMP_MAX):
        warnings.append(f"Clamped preamp into {PREAMP_MIN}/{PREAMP_MAX} dB")
        preamp = max(PREAMP_MIN, min(PREAMP_MAX, preamp))

    return preamp, bands, warnings


# --------------------------------------------------------------- packets


def _blank():
    return bytearray(REPORT_LEN)


def build_band_packet(index, freq, gain_db, q):
    pkt = _blank()
    pkt[0:4] = bytes([0x4B, 0x01, 0x09, 0x18])
    pkt[5] = index
    struct.pack_into("<H", pkt, 28, int(round(freq)))
    struct.pack_into("<H", pkt, 30, int(round(q * 256)))
    struct.pack_into("<h", pkt, 32, int(round(gain_db * 256)))
    pkt[34:38] = TRAILER
    return bytes(pkt)


def build_sequence(preamp_db, bands):
    packets = []
    for i in range(NUM_BANDS):
        if i < len(bands):
            b = bands[i]
            packets.append(build_band_packet(i, b["freq"], b["gain"], b["q"]))
        else:
            packets.append(build_band_packet(i, DEFAULT_FREQS[i], 0.0, DEFAULT_Q))

    commit = _blank()
    commit[0:8] = bytes([0x4B, 0x01, 0x0A, 0x04, 0x00, 0x00, 0xFF, 0xFF])
    packets.append(bytes(commit))

    pre = _blank()
    pre[0:5] = bytes([0x4B, 0x01, 0x03, 0x02, 0x00])
    pre[5] = int(round(preamp_db)) & 0xFF
    packets.append(bytes(pre))

    pcmt = _blank()
    pcmt[0:4] = bytes([0x4B, 0x01, 0x04, 0x00])
    packets.append(bytes(pcmt))

    save = _blank()
    save[0:4] = bytes([0x4B, 0x01, 0x01, 0x00])
    packets.append(bytes(save))

    return packets


# --------------------------------------------------------------- device


def find_device():
    import hid

    for d in hid.enumerate():
        if d["vendor_id"] == VID and d["product_id"] == PID:
            return d
    return None


def send_packets(packets):
    import hid

    dev = hid.device()
    dev.open(VID, PID)
    try:
        for p in packets:
            dev.write(p)
            time.sleep(0.02)
    finally:
        dev.close()


# --------------------------------------------------------------- storage


def load_presets():
    try:
        with open(PRESET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_presets(presets):
    tmp = PRESET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=2)
    os.replace(tmp, PRESET_FILE)


# --------------------------------------------------------------- startup


def exe_command():
    """Command Windows should run at login."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --tray'


def startup_enabled():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_ID)
        return True
    except Exception:
        return False


def set_startup(enable):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, exe_command())
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------- tray icon


def make_icon_image():
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=12, fill=(28, 30, 38, 255))
    for i, h in enumerate([22, 38, 14, 30, 44]):
        x = 10 + i * 9
        d.rectangle([x, size - 10 - h, x + 5, size - 10], fill=(90, 200, 250, 255))
    return img


# --------------------------------------------------------------- gui


class App(tk.Tk):
    def __init__(self, start_hidden=False):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("800x600")
        self.minsize(720, 540)

        self.presets = load_presets()
        self.current = None
        self.tray = None
        self.busy = False

        self._build_ui()
        self._refresh_preset_list()
        self.refresh_device()

        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._start_tray()

        if start_hidden:
            self.after(200, self.hide_to_tray)

    # ---------------- layout

    def _build_ui(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=10, pady=8)

        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 10))

        ttk.Label(left, text="Presets", font=("", 10, "bold")).pack(anchor="w")

        self.listbox = tk.Listbox(left, width=26, height=16, exportselection=False)
        self.listbox.pack(fill="y", expand=True, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self.on_select_preset)
        self.listbox.bind("<Double-Button-1>", lambda e: self.on_apply())

        for text, cmd in [
            ("Add from file...", self.on_add),
            ("Rename", self.on_rename),
            ("Delete", self.on_delete),
        ]:
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=2)

        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        head = ttk.Frame(right)
        head.pack(fill="x")
        self.name_label = ttk.Label(
            head, text="No preset selected", font=("", 11, "bold")
        )
        self.name_label.pack(side="left")
        self.dev_label = ttk.Label(head, text="Device: checking...")
        self.dev_label.pack(side="right")

        cols = ("band", "freq", "gain", "q", "note")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", height=11)
        for c, w, t in [
            ("band", 55, "Band"),
            ("freq", 100, "Freq (Hz)"),
            ("gain", 100, "Gain (dB)"),
            ("q", 80, "Q"),
            ("note", 200, "Note"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c != "note" else "w")
        self.tree.pack(fill="both", expand=True, pady=6)

        row = ttk.Frame(right)
        row.pack(fill="x")
        self.preamp_label = ttk.Label(row, text="Preamp: -")
        self.preamp_label.pack(side="left")

        self.apply_btn = ttk.Button(
            row, text="Apply to DAC", command=self.on_apply, state="disabled"
        )
        self.apply_btn.pack(side="right")
        ttk.Button(row, text="Reset to flat", command=self.on_reset).pack(
            side="right", padx=6
        )
        ttk.Button(row, text="Refresh device", command=self.refresh_device).pack(
            side="right", padx=6
        )

        opts = ttk.Frame(right)
        opts.pack(fill="x", pady=(8, 0))
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(
            opts,
            text="Start with Windows (hidden in tray)",
            variable=self.startup_var,
            command=self.on_startup_toggle,
        ).pack(side="left")

        self.log_box = tk.Text(right, height=7, wrap="word", state="disabled")
        self.log_box.pack(fill="both", pady=(8, 0))

    # ---------------- helpers

    def log(self, msg):
        def do():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        try:
            self.after(0, do)
        except Exception:
            pass

    def refresh_device(self):
        try:
            d = find_device()
        except Exception as e:
            self.dev_label.configure(text="Device: hidapi error", foreground="#b00")
            self.log(f"hidapi not usable: {e}")
            return
        if d:
            self.dev_label.configure(
                text=f"Device: {d.get('product_string') or 'Space Pro'}",
                foreground="#070",
            )
        else:
            self.dev_label.configure(text="Device: not found", foreground="#b00")

    def _refresh_preset_list(self):
        self.listbox.delete(0, "end")
        for p in self.presets:
            self.listbox.insert("end", p["name"])
        self._rebuild_tray_menu()

    def _show_preset(self, preset):
        self.tree.delete(*self.tree.get_children())
        if not preset:
            self.name_label.configure(text="No preset selected")
            self.preamp_label.configure(text="Preamp: -")
            self.apply_btn.configure(state="disabled")
            return

        self.name_label.configure(text=preset["name"])
        self.preamp_label.configure(
            text=f"Preamp: {preset['preamp']:+.0f} dB (whole dB only)"
        )
        bands = preset["bands"]
        for i, b in enumerate(bands):
            self.tree.insert(
                "",
                "end",
                values=(
                    i + 1,
                    f"{b['freq']:g}",
                    f"{b['gain']:+.2f}",
                    f"{b['q']:g}",
                    b.get("note", ""),
                ),
            )
        for i in range(len(bands), NUM_BANDS):
            self.tree.insert(
                "",
                "end",
                values=(i + 1, DEFAULT_FREQS[i], "0.00", f"{DEFAULT_Q:g}", "unused"),
            )
        self.apply_btn.configure(state="disabled" if self.busy else "normal")

    # ---------------- preset actions

    def on_select_preset(self, _evt=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.current = self.presets[sel[0]]
        self._show_preset(self.current)

    def on_add(self):
        path = filedialog.askopenfilename(
            title="Choose a parametric EQ text file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            preamp, bands, warns = parse_peq_text(text, True)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
            return

        default = os.path.splitext(os.path.basename(path))[0][:40]
        name = simpledialog.askstring(
            APP_NAME, "Preset name:", initialvalue=default, parent=self
        )
        if not name or not name.strip():
            return
        name = name.strip()

        if any(p["name"] == name for p in self.presets):
            if not messagebox.askyesno(APP_NAME, f'Replace existing preset "{name}"?'):
                return
            self.presets = [p for p in self.presets if p["name"] != name]

        self.presets.append({"name": name, "preamp": preamp, "bands": bands})
        save_presets(self.presets)
        self._refresh_preset_list()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(len(self.presets) - 1)
        self.on_select_preset()

        self.log(f'Saved preset "{name}" ({len(bands)} bands)')
        for w in warns:
            self.log(f"  ! {w}")

    def on_rename(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        p = self.presets[sel[0]]
        name = simpledialog.askstring(
            APP_NAME, "New name:", initialvalue=p["name"], parent=self
        )
        if not name or not name.strip():
            return
        p["name"] = name.strip()
        save_presets(self.presets)
        self._refresh_preset_list()
        self.listbox.selection_set(sel[0])
        self._show_preset(p)

    def on_delete(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        p = self.presets[sel[0]]
        if not messagebox.askyesno(APP_NAME, f'Delete preset "{p["name"]}"?'):
            return
        self.presets.pop(sel[0])
        save_presets(self.presets)
        self.current = None
        self._refresh_preset_list()
        self._show_preset(None)

    # ---------------- device actions

    def _run_write(self, packets, done_msg):
        if self.busy:
            return
        self.busy = True
        try:
            self.apply_btn.configure(state="disabled")
        except Exception:
            pass

        def work():
            try:
                send_packets(packets)
                self.log(done_msg)
                if self.tray:
                    try:
                        self.tray.notify(done_msg, APP_NAME)
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"FAILED: {e}")
            finally:
                self.busy = False
                self.after(
                    0,
                    lambda: self.apply_btn.configure(
                        state="normal" if self.current else "disabled"
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def on_apply(self):
        if not self.current:
            return
        self.log(f'Applying "{self.current["name"]}"...')
        self._run_write(
            build_sequence(self.current["preamp"], self.current["bands"]),
            f'Applied "{self.current["name"]}"',
        )

    def apply_by_name(self, name):
        for p in self.presets:
            if p["name"] == name:
                self.log(f'Applying "{name}"...')
                self._run_write(
                    build_sequence(p["preamp"], p["bands"]), f'Applied "{name}"'
                )
                return

    def on_reset(self):
        self.log("Resetting to flat...")
        self._run_write(build_sequence(0.0, []), "Reset to flat")

    def on_startup_toggle(self):
        try:
            set_startup(self.startup_var.get())
            self.log(
                "Start with Windows enabled"
                if self.startup_var.get()
                else "Start with Windows disabled"
            )
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not change startup setting.\n\n{e}")
            self.startup_var.set(startup_enabled())

    # ---------------- tray

    def _start_tray(self):
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except Exception as e:
            self.log(f"Tray unavailable ({e}). Closing the window will exit.")
            self.protocol("WM_DELETE_WINDOW", self.quit_app)
            return

        import pystray

        self.tray = pystray.Icon(APP_ID, make_icon_image(), APP_NAME)
        self._rebuild_tray_menu()
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _rebuild_tray_menu(self):
        if not self.tray:
            return
        import pystray

        items = [
            pystray.MenuItem(
                "Show window", lambda *_: self.after(0, self.show_window), default=True
            )
        ]
        if self.presets:
            items.append(pystray.Menu.SEPARATOR)
            for p in self.presets:
                items.append(
                    pystray.MenuItem(
                        p["name"],
                        lambda *_a, _n=p["name"]: self.after(
                            0, lambda: self.apply_by_name(_n)
                        ),
                    )
                )
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reset to flat", lambda *_: self.after(0, self.on_reset)),
            pystray.MenuItem("Quit", lambda *_: self.after(0, self.quit_app)),
        ]
        self.tray.menu = pystray.Menu(*items)

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self):
        if self.tray:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App(start_hidden="--tray" in sys.argv).mainloop()
