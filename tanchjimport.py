"""
TanchjImport - Tanchjim Space Pro control app
=============================================

  * imports AutoEQ / Wavelet parametric EQ files
  * full PEQ editor: drag bands on the response curve, scroll for Q
  * every filter type the firmware implements, including the low and
    high shelves the official app never exposes
  * hardware settings (gain, reconstruction filter, output mode, DRE,
    mic gain)
  * profiles: save EQ *and* hardware settings together, switch in one
    click from the window or the tray

PROTOCOL (reverse engineered). VID 0x3302 / PID 0x4307, HID report ID
0x4B, 64-byte reports. Commands share the shape 4B <dir> <group> <len>,
dir 0x01 = write, 0x80 = read.

  EQ
    4B 01 09 18 00 <band 0-9>
        [28:30] frequency    uint16 LE, raw Hz
        [30:32] Q            uint16 LE, value/256
        [32:34] gain         int16  LE, value/256 dB
        [34:36] filter type  uint16 LE
        [8:28]  coefficient blob - ignored by firmware, we send zeros
    4B 01 0A 04 00 00 FF FF   commit bands
    4B 01 03 02 00 <int8 dB>  master preamp - value is byte[5]
    4B 01 04 00               commit
    4B 01 01 00               save / persist
    4B 80 09 1F 00 <band>     read band
    4B 80 03 02               read preamp

  Filter types: 0 OFF, 1 LS, 2 PK, 3 HS, 4 LP, 5 HP, 6 BP
  Gain is ignored for OFF, LP, HP and BP.

  Hardware settings (never touched by EQ writes, and vice versa)
    4B 01 19 3C <v>          output gain      0 Low 2Vrms, 1 High 4Vrms
    4B 01 11 01 <v>          reconstruction filter, 1-5
    4B 01 1D 3C <v>          DAC output mode  0 Class AB, 1 Class H
    4B 01 32 01 <v>          DRE optimization 0 off, 1 on
    4B 01 02 02 <int16 LE>   mic volume gain, value/256 dB

  Channel balance (group 0x16) is NOT implemented. Our decode of it was
  wrong and writing it produced a dangerous volume jump. Do not add it
  back without re-verifying against a fresh capture.

  Undecoded: group 0x85 (polled by the official app, replies 0x39) and
  the [36:38] field in band packets.

Requires: PySide6, hidapi

Not affiliated with Tanchjim. Use at your own risk.
"""

import cmath
import json
import math
import os
import re
import struct
import sys
import threading
import time

APP_NAME = "TanchjImport"
APP_ID = "TanchjImport"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

VID = 0x3302
PID = 0x4307

NUM_BANDS = 10
REPORT_LEN = 64
SAMPLE_RATE = 48000

FREQ_MIN, FREQ_MAX = 20, 20000
Q_MIN, Q_MAX = 0.10, 10.0
GAIN_MIN, GAIN_MAX = -12.0, 12.0
PREAMP_MIN, PREAMP_MAX = -12, 12

DEFAULT_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
DEFAULT_Q = 0.71

TYPE_OFF, TYPE_LS, TYPE_PK, TYPE_HS, TYPE_LP, TYPE_HP, TYPE_BP = 0, 1, 2, 3, 4, 5, 6
TYPE_NAMES = {TYPE_OFF: "OFF", TYPE_LS: "LS", TYPE_PK: "PK", TYPE_HS: "HS",
              TYPE_LP: "LP", TYPE_HP: "HP", TYPE_BP: "BP"}
GAINLESS_TYPES = {TYPE_OFF, TYPE_LP, TYPE_HP, TYPE_BP}

LABEL_TO_CODE = {
    "PK": TYPE_PK, "PEQ": TYPE_PK, "BELL": TYPE_PK,
    "LS": TYPE_LS, "LSC": TYPE_LS, "LSQ": TYPE_LS,
    "HS": TYPE_HS, "HSC": TYPE_HS, "HSQ": TYPE_HS,
    "LP": TYPE_LP, "LPQ": TYPE_LP, "LPF": TYPE_LP,
    "HP": TYPE_HP, "HPQ": TYPE_HP, "HPF": TYPE_HP,
    "BP": TYPE_BP, "BPF": TYPE_BP,
    "OFF": TYPE_OFF,
}

# key: (group, length, kind, options)
SETTINGS = {
    "gain": (0x19, 0x3C, "choice", [(0, "Low gain 2Vrms"), (1, "High gain 4Vrms")]),
    "filter": (0x11, 0x01, "choice", [
        (1, "Low latency fast steep descent"),
        (2, "Fast descent with phase compensation"),
        (3, "Low latency slow descent"),
        (4, "Slow descent with phase compensation"),
        (5, "Non-oversampling"),
    ]),
    "output_mode": (0x1D, 0x3C, "choice", [(0, "Class AB"), (1, "Class H")]),
    "dre": (0x32, 0x01, "choice", [(0, "Off"), (1, "On")]),
    "mic_gain": (0x02, 0x02, "q8_8", None),
}
SETTING_LABELS = {
    "gain": "Output gain",
    "filter": "Reconstruction filter",
    "output_mode": "DAC output mode",
    "dre": "DRE optimization",
    "mic_gain": "Microphone gain (dB)",
}


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "SpaceProEQ")
    os.makedirs(d, exist_ok=True)
    return d


PRESET_FILE = os.path.join(config_dir(), "presets.json")
PROFILE_FILE = os.path.join(config_dir(), "profiles.json")


# --------------------------------------------------------------- parsing

FILTER_RE = re.compile(
    r"Filter\s*(?:\d+)?\s*:\s*(ON|OFF)\s+([A-Z]{2,5})\s+Fc\s+([\d.]+)\s*Hz\s+"
    r"Gain\s+(-?[\d.]+)\s*dB\s+Q\s+([\d.]+)", re.IGNORECASE)
PREAMP_RE = re.compile(r"Preamp\s*:\s*(-?[\d.]+)\s*dB", re.IGNORECASE)


def parse_peq_text(text):
    warnings = []
    m = PREAMP_RE.search(text)
    preamp = float(m.group(1)) if m else 0.0

    bands, matched = [], False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("preamp"):
            continue
        m = FILTER_RE.search(line)
        if not m:
            if line.lower().startswith("filter"):
                warnings.append(f"Could not read: {line[:60]}")
            continue
        matched = True
        state, kind, fc, gain, q = m.groups()
        if state.upper() == "OFF":
            continue
        fc, gain, q = float(fc), float(gain), float(q)
        code = LABEL_TO_CODE.get(kind.upper())
        if code is None:
            warnings.append(f"Skipped unsupported type {kind} at {fc:g} Hz")
            continue
        if not (FREQ_MIN <= fc <= FREQ_MAX):
            warnings.append(f"Clamped {fc:g} Hz")
            fc = max(FREQ_MIN, min(FREQ_MAX, fc))
        if not (Q_MIN <= q <= Q_MAX):
            warnings.append(f"Clamped Q {q:g}")
            q = max(Q_MIN, min(Q_MAX, q))
        if not (GAIN_MIN <= gain <= GAIN_MAX):
            warnings.append(f"Clamped {gain:+g} dB")
            gain = max(GAIN_MIN, min(GAIN_MAX, gain))
        bands.append({"freq": fc, "gain": gain, "q": q, "type": code})

    if not matched:
        raise ValueError("No filter lines found. Expected lines like:\n"
                         "Filter 1: ON PK Fc 200 Hz Gain -6 dB Q 0.5")
    if len(bands) > NUM_BANDS:
        warnings.append(f"File has {len(bands)} filters; using the first {NUM_BANDS}")
        bands = bands[:NUM_BANDS]
    if not (PREAMP_MIN <= preamp <= PREAMP_MAX):
        warnings.append("Clamped preamp")
        preamp = max(PREAMP_MIN, min(PREAMP_MAX, preamp))
    return preamp, bands, warnings


# ------------------------------------------------- frequency response


def biquad_coeffs(ftype, f0, gain_db, q, fs=SAMPLE_RATE):
    f0 = max(1.0, min(f0, fs / 2 - 1))
    q = max(0.01, q)
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)

    if ftype == TYPE_PK:
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif ftype == TYPE_LS:
        sa = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) - (A - 1) * cw + sa),
             2 * A * ((A - 1) - (A + 1) * cw),
             A * ((A + 1) - (A - 1) * cw - sa)]
        a = [(A + 1) + (A - 1) * cw + sa,
             -2 * ((A - 1) + (A + 1) * cw),
             (A + 1) + (A - 1) * cw - sa]
    elif ftype == TYPE_HS:
        sa = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) + (A - 1) * cw + sa),
             -2 * A * ((A - 1) + (A + 1) * cw),
             A * ((A + 1) + (A - 1) * cw - sa)]
        a = [(A + 1) - (A - 1) * cw + sa,
             2 * ((A - 1) - (A + 1) * cw),
             (A + 1) - (A - 1) * cw - sa]
    elif ftype == TYPE_LP:
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif ftype == TYPE_HP:
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif ftype == TYPE_BP:
        b = [alpha, 0.0, -alpha]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    else:
        return [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]

    a0 = a[0]
    return [x / a0 for x in b], [x / a0 for x in a]


def band_response_db(band, freqs, fs=SAMPLE_RATE):
    ftype = band.get("type", TYPE_PK)
    gain = 0.0 if ftype in GAINLESS_TYPES else band["gain"]
    if ftype == TYPE_OFF or (ftype == TYPE_PK and abs(gain) < 1e-9):
        return [0.0] * len(freqs)
    b, a = biquad_coeffs(ftype, band["freq"], gain, band["q"], fs)
    b0, b1, b2 = b
    a0, a1, a2 = a
    out = []
    log10, exp = math.log10, cmath.exp
    tau = 2 * math.pi / fs
    for f in freqs:
        z = exp(-1j * (tau * f))
        zz = z * z
        den = a0 + a1 * z + a2 * zz
        mag = abs((b0 + b1 * z + b2 * zz) / den) if den != 0 else 1e-9
        out.append(20 * log10(mag if mag > 1e-9 else 1e-9))
    return out


def response_db(bands, preamp_db, freqs, fs=SAMPLE_RATE):
    out = [float(preamp_db)] * len(freqs)
    for band in bands:
        vals = band_response_db(band, freqs, fs)
        for i, v in enumerate(vals):
            out[i] += v
    return out


def log_freqs(n=280, lo=FREQ_MIN, hi=FREQ_MAX):
    lo_l, hi_l = math.log10(lo), math.log10(hi)
    return [10 ** (lo_l + (hi_l - lo_l) * i / (n - 1)) for i in range(n)]


# --------------------------------------------------------------- packets


def _blank():
    return bytearray(REPORT_LEN)


def _cmd(group, length, payload=b""):
    p = _blank()
    p[0:4] = bytes([0x4B, 0x01, group, length])
    p[4:4 + len(payload)] = payload
    return bytes(p)


def build_band_packet(index, freq, gain_db, q, ftype=TYPE_PK):
    pkt = _blank()
    pkt[0:4] = bytes([0x4B, 0x01, 0x09, 0x18])
    pkt[5] = index
    struct.pack_into("<H", pkt, 28, int(round(freq)))
    struct.pack_into("<H", pkt, 30, int(round(q * 256)))
    struct.pack_into("<h", pkt, 32, int(round(gain_db * 256)))
    struct.pack_into("<H", pkt, 34, int(ftype))
    struct.pack_into("<H", pkt, 36, 20)
    return bytes(pkt)


def build_band_commit():
    p = _blank()
    p[0:8] = bytes([0x4B, 0x01, 0x0A, 0x04, 0x00, 0x00, 0xFF, 0xFF])
    return bytes(p)


def build_preamp(preamp_db):
    """Value goes in byte[5] - byte[4] is a fixed 0x00."""
    return _cmd(0x03, 0x02, bytes([0x00, int(round(preamp_db)) & 0xFF]))


def build_sequence(preamp_db, bands):
    packets = []
    for i in range(NUM_BANDS):
        if i < len(bands):
            b = bands[i]
            packets.append(build_band_packet(i, b["freq"], b["gain"], b["q"],
                                             b.get("type", TYPE_PK)))
        else:
            packets.append(build_band_packet(i, DEFAULT_FREQS[i], 0.0, DEFAULT_Q))
    packets.append(build_band_commit())
    packets.append(build_preamp(preamp_db))
    packets.append(_cmd(0x04, 0x00))
    packets.append(_cmd(0x01, 0x00))
    return packets


def build_single_band(index, band):
    return [build_band_packet(index, band["freq"], band["gain"], band["q"],
                              band.get("type", TYPE_PK)),
            build_band_commit()]


def build_setting_packet(key, value):
    group, length, kind, _ = SETTINGS[key]
    if kind == "q8_8":
        payload = struct.pack("<h", int(round(float(value) * 256)))
    else:
        payload = bytes([int(value) & 0xFF])
    return [_cmd(group, length, payload), _cmd(0x04, 0x00)]


def blank_band(i):
    return {"freq": DEFAULT_FREQS[i], "gain": 0.0, "q": DEFAULT_Q,
            "type": TYPE_PK}


def pad_bands(bands):
    out = [dict(b) for b in (bands or [])][:NUM_BANDS]
    while len(out) < NUM_BANDS:
        out.append(blank_band(len(out)))
    return out


# --------------------------------------------------------------- device

DEVICE_LOCK = threading.RLock()


def _locked(fn):
    def wrapper(*a, **kw):
        with DEVICE_LOCK:
            return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


def find_device():
    import hid

    for d in hid.enumerate():
        if d["vendor_id"] == VID and d["product_id"] == PID:
            return d
    return None


def _open():
    import hid

    d = hid.device()
    d.open(VID, PID)
    d.set_nonblocking(0)
    return d


def _await(dev, group, timeout=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = dev.read(REPORT_LEN, timeout_ms=150)
        if not data:
            continue
        b = bytes(data)
        if len(b) >= 6 and b[0] == 0x4B and b[1] == 0x80 and b[2] == group:
            return b
    return None


@_locked
def send_packets(packets):
    dev = _open()
    try:
        for p in packets:
            dev.write(p)
            time.sleep(0.02)
    finally:
        dev.close()


def decode_band(b):
    b = bytes(b)
    if len(b) < 38:
        return None
    return {"band": b[5],
            "freq": struct.unpack("<H", b[28:30])[0],
            "q": struct.unpack("<H", b[30:32])[0] / 256,
            "gain": struct.unpack("<h", b[32:34])[0] / 256,
            "type": struct.unpack("<H", b[34:36])[0]}


@_locked
def read_bands():
    out = {}
    dev = _open()
    try:
        for i in range(NUM_BANDS):
            req = _blank()
            req[0:6] = bytes([0x4B, 0x80, 0x09, 0x1F, 0x00, i])
            dev.write(bytes(req))
            reply = _await(dev, 0x09)
            if reply and reply[5] == i:
                out[i] = decode_band(reply)
    finally:
        dev.close()
    return out


@_locked
def read_preamp():
    """Reply carries the value in byte[5], matching the write layout."""
    dev = _open()
    try:
        req = _blank()
        req[0:4] = bytes([0x4B, 0x80, 0x03, 0x02])
        dev.write(bytes(req))
        reply = _await(dev, 0x03)
        return struct.unpack("<b", reply[5:6])[0] if reply else None
    finally:
        dev.close()


@_locked
def read_settings():
    out = {}
    dev = _open()
    try:
        for key, (group, length, kind, _opts) in SETTINGS.items():
            req = _blank()
            req[0:4] = bytes([0x4B, 0x80, group, length])
            dev.write(bytes(req))
            reply = _await(dev, group)
            if not reply:
                continue
            if kind == "q8_8":
                out[key] = struct.unpack("<h", reply[4:6])[0] / 256
            else:
                out[key] = reply[4]
    finally:
        dev.close()
    return out


# --------------------------------------------------------------- storage


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else default
    except Exception:
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_presets():
    data = _load_json(PRESET_FILE, [])
    for p in data:
        for b in p.get("bands", []):
            b.setdefault("type", TYPE_PK)
    return data


def save_presets(p):
    _save_json(PRESET_FILE, p)


def load_profiles():
    return _load_json(PROFILE_FILE, [])


def save_profiles(p):
    _save_json(PROFILE_FILE, p)


def build_profile_packets(profile):
    """EQ then hardware settings, as one ordered burst."""
    packets = list(build_sequence(profile.get("preamp", 0),
                                  profile.get("bands", [])))
    for key, value in (profile.get("settings") or {}).items():
        if key in SETTINGS:
            packets.extend(build_setting_packet(key, value))
    return packets


# --------------------------------------------------------------- startup


def exe_command():
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

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, exe_command())
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass
# =====================================================================
#  UI  (PySide6 / Qt)
# =====================================================================

from PySide6.QtCore import (QObject, QPointF, QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QSystemTrayIcon,
                               QTabWidget, QVBoxLayout, QWidget)

BG        = "#12141f"
CARD      = "#1a1d2b"
CARD_HI   = "#232739"
STROKE    = "#2b3145"
GRID      = "#242939"
GRID_HI   = "#38405a"
TEXT      = "#eef0f6"
MUTED     = "#98a1ba"
ACCENT    = "#8b5cf6"
ACCENT_HI = "#a78bfa"
REF_C     = "#f59e0b"

BAND_COLOURS = ["#a78bfa", "#60a5fa", "#34d399", "#fbbf24", "#f87171",
                "#f472b6", "#22d3ee", "#a3e635", "#fb923c", "#c084fc"]

SEND_DEBOUNCE_MS = 110

STYLE = f"""
QWidget {{ background: {BG}; color: {TEXT};
           font-family: "Segoe UI", sans-serif; font-size: 12px; }}
/* Labels inherited the window background and painted it over the card,
   which is what made headers look like dark boxes and text look dim. */
QLabel {{ background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QFrame#card {{ background: {CARD}; border-radius: 12px; }}
QFrame#row  {{ background: {CARD_HI}; border-radius: 8px; }}
QLabel#h1 {{ font-size: 18px; font-weight: 600; }}
QLabel#h2 {{ font-size: 13px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QPushButton {{ background: transparent; border: 1px solid {STROKE};
               border-radius: 8px; padding: 6px 12px; }}
QPushButton:hover {{ background: {CARD_HI}; }}
QPushButton#primary {{ background: {ACCENT}; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HI}; }}
QComboBox, QSpinBox, QDoubleSpinBox {{ background: {CARD}; border: 1px solid {STROKE};
                                       border-radius: 6px; padding: 3px 6px; }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{ background: {CARD}; selection-background-color: {ACCENT}; }}
QListWidget {{ background: transparent; border: none; }}
QListWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {ACCENT}; }}
QListWidget::item:hover {{ background: {CARD_HI}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {CARD}; padding: 8px 20px; margin-right: 4px;
                border-radius: 8px; color: {MUTED}; }}
QTabBar::tab:selected {{ background: {ACCENT}; color: {TEXT}; font-weight: 600; }}
QSlider::groove:horizontal {{ height: 4px; background: {STROKE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT_HI}; width: 13px;
                              margin: -5px 0; border-radius: 6px; }}
QPlainTextEdit {{ background: {CARD}; border: none; border-radius: 10px;
                  color: {MUTED}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {STROKE}; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


class NoWheelMixin:
    """
    Scrolling the band list used to land on a spin box and silently change
    the value under the cursor. Wheel events are now only accepted when the
    widget actually has focus; otherwise they pass through to the scroll
    area.
    """

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class SpinBox(NoWheelMixin, QSpinBox):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setFocusPolicy(Qt.StrongFocus)


class DoubleSpinBox(NoWheelMixin, QDoubleSpinBox):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setFocusPolicy(Qt.StrongFocus)


class ComboBox(NoWheelMixin, QComboBox):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setFocusPolicy(Qt.StrongFocus)


def card(name="card"):
    f = QFrame()
    f.setObjectName(name)
    return f


def app_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(CARD))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setBrush(QColor(ACCENT_HI))
    for i, h in enumerate([22, 38, 14, 30, 44]):
        p.drawRoundedRect(10 + i * 9, 54 - h, 5, h, 2, 2)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- graph


class Graph(QWidget):
    """
    Response curve drawn with QPainter. Qt anti-aliases natively, so there
    is no supersampling and no bitmap conversion - this is why it is far
    quicker than the previous renderer.
    """

    bandChanged = Signal(int, dict, bool)
    bandSelected = Signal(int)

    DECADES = [20, 30, 40, 50, 70, 100, 200, 300, 400, 500, 700,
               1000, 2000, 3000, 4000, 5000, 7000, 10000, 20000]
    LABELS = {20: "20", 50: "50", 100: "100", 200: "200", 500: "500",
              1000: "1k", 2000: "2k", 5000: "5k", 10000: "10k", 20000: "20k"}
    PAD_L, PAD_R, PAD_T, PAD_B = 40, 12, 12, 22
    R, R_BIG = 7, 9

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(240)
        self.setMouseTracking(True)
        self.bands = pad_bands([])
        self.preamp = 0.0
        self.reference = None
        self.range_db = 15
        self.freqs = log_freqs(280)
        self.selected = None
        self.hover = None
        self._drag = None
        self._cache = {}

    # ---- state

    def set_state(self, bands, preamp, reference=None, selected=None):
        self.bands = bands
        self.preamp = preamp
        self.reference = reference
        self.selected = selected
        self._ref_curve = None
        self.update()

    def _band_curve(self, i, band):
        key = (band.get("type"), round(band["freq"], 2),
               round(band["gain"], 2), round(band["q"], 3))
        hit = self._cache.get(i)
        if hit and hit[0] == key:
            return hit[1]
        vals = band_response_db(band, self.freqs)
        self._cache[i] = (key, vals)
        return vals

    def _total(self):
        n = len(self.freqs)
        out = [float(self.preamp)] * n
        for i, b in enumerate(self.bands):
            vals = self._band_curve(i, b)
            for j in range(n):
                out[j] += vals[j]
        return out

    # ---- mapping

    def _box(self):
        return QRectF(self.PAD_L, self.PAD_T,
                      self.width() - self.PAD_L - self.PAD_R,
                      self.height() - self.PAD_T - self.PAD_B)

    def x_of(self, f):
        b = self._box()
        lo, hi = math.log10(FREQ_MIN), math.log10(FREQ_MAX)
        f = max(FREQ_MIN, min(FREQ_MAX, f))
        return b.left() + (math.log10(f) - lo) / (hi - lo) * b.width()

    def f_of(self, x):
        b = self._box()
        lo, hi = math.log10(FREQ_MIN), math.log10(FREQ_MAX)
        t = (x - b.left()) / max(1.0, b.width())
        return 10 ** (lo + max(0.0, min(1.0, t)) * (hi - lo))

    def y_of(self, db):
        b = self._box()
        r = self.range_db
        db = max(-r, min(r, db))
        return b.top() + (r - db) / (2 * r) * b.height()

    def db_of(self, y):
        b = self._box()
        r = self.range_db
        t = (y - b.top()) / max(1.0, b.height())
        return r - max(0.0, min(1.0, t)) * 2 * r

    # ---- interaction

    def _hit(self, pos):
        best, bd = None, 1e9
        for i, b in enumerate(self.bands):
            if b.get("type") == TYPE_OFF:
                continue
            gain = 0.0 if b["type"] in GAINLESS_TYPES else b["gain"]
            d = math.hypot(pos.x() - self.x_of(b["freq"]),
                           pos.y() - self.y_of(gain))
            if d < 15 and d < bd:
                best, bd = i, d
        return best

    def mouseMoveEvent(self, e):
        if self._drag is None:
            h = self._hit(e.position())
            if h != self.hover:
                self.hover = h
                self.setCursor(Qt.PointingHandCursor if h is not None
                               else Qt.ArrowCursor)
                self.update()
            return
        i = self._drag
        b = dict(self.bands[i])
        b["freq"] = round(self.f_of(e.position().x()))
        if b["type"] not in GAINLESS_TYPES:
            b["gain"] = round(self.db_of(e.position().y()) * 2) / 2
        self.bands[i] = b
        self.update()
        self.bandChanged.emit(i, b, False)

    def mousePressEvent(self, e):
        i = self._hit(e.position())
        self.selected = i
        self.bandSelected.emit(-1 if i is None else i)
        self._drag = i
        self.update()

    def mouseReleaseEvent(self, _e):
        if self._drag is not None:
            i, self._drag = self._drag, None
            self.bandChanged.emit(i, self.bands[i], True)
        self.update()

    def wheelEvent(self, e):
        i = self.hover if self.hover is not None else self.selected
        if i is None:
            return
        b = dict(self.bands[i])
        step = 1.07 if e.angleDelta().y() > 0 else 1 / 1.07
        b["q"] = round(max(Q_MIN, min(Q_MAX, b["q"] * step)), 3)
        self.bands[i] = b
        self.update()
        self.bandChanged.emit(i, b, True)

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    # ---- paint

    def _path(self, curve):
        path = QPainterPath()
        for j, (f, v) in enumerate(zip(self.freqs, curve)):
            pt = QPointF(self.x_of(f), self.y_of(v))
            path.moveTo(pt) if j == 0 else path.lineTo(pt)
        return path

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        box = self._box()
        p.fillRect(self.rect(), QColor(CARD))

        curve = self._total()
        peak = max((abs(v) for v in curve), default=1.0)
        ref = None
        if self.reference:
            if getattr(self, "_ref_curve", None) is None:
                self._ref_curve = response_db(self.reference[0],
                                              self.reference[1], self.freqs)
            ref = self._ref_curve
            peak = max(peak, max((abs(v) for v in ref), default=1.0))
        self.range_db = 15 if peak <= 14 else (25 if peak <= 24 else 40)
        step = 5 if self.range_db <= 15 else 10

        f = QFont("Segoe UI", 7)
        p.setFont(f)

        p.setPen(QPen(QColor(GRID), 1))
        for fr in self.DECADES:
            x = self.x_of(fr)
            p.drawLine(QPointF(x, box.top()), QPointF(x, box.bottom()))
        db = -self.range_db
        while db <= self.range_db:
            y = self.y_of(db)
            p.setPen(QPen(QColor(GRID_HI if db == 0 else GRID), 1))
            p.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(0, y - 7, self.PAD_L - 6, 14),
                       Qt.AlignRight | Qt.AlignVCenter, f"{db:+d}")
            db += step
        p.setPen(QColor(MUTED))
        for fr in self.DECADES:
            if fr in self.LABELS:
                p.drawText(QRectF(self.x_of(fr) - 20, box.bottom() + 4, 40, 14),
                           Qt.AlignHCenter, self.LABELS[fr])

        path = self._path(curve)

        fill = QPainterPath(path)
        fill.lineTo(QPointF(box.right(), box.bottom()))
        fill.lineTo(QPointF(box.left(), box.bottom()))
        fill.closeSubpath()
        grad = QLinearGradient(0, box.top(), 0, box.bottom())
        c = QColor(ACCENT)
        c.setAlpha(120)
        grad.setColorAt(0.0, c)
        c2 = QColor(ACCENT)
        c2.setAlpha(0)
        grad.setColorAt(1.0, c2)
        p.fillPath(fill, QBrush(grad))

        if ref:
            pen = QPen(QColor(REF_C), 1.6)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawPath(self._path(ref))

        p.setPen(QPen(QColor(ACCENT_HI), 2))
        p.drawPath(path)

        p.setPen(QPen(QColor(STROKE), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(box)

        p.setFont(QFont("Segoe UI", 7, QFont.Bold))
        for i, b in enumerate(self.bands):
            if b.get("type") == TYPE_OFF:
                continue
            gain = 0.0 if b["type"] in GAINLESS_TYPES else b["gain"]
            cx, cy = self.x_of(b["freq"]), self.y_of(gain)
            active = (i == self.selected or i == self.hover)
            r = self.R_BIG if active else self.R
            col = QColor(BAND_COLOURS[i % len(BAND_COLOURS)])
            col.setAlpha(240 if active else 180)
            p.setBrush(QBrush(col))
            p.setPen(QPen(QColor(255, 255, 255, 200 if active else 90), 1.4))
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.setPen(QColor(18, 20, 31))
            p.drawText(QRectF(cx - r, cy - r, r * 2, r * 2),
                       Qt.AlignCenter, str(i + 1))
        p.end()


# ------------------------------------------------------------- band row


class BandRow(QFrame):
    changed = Signal(int, dict, bool)
    picked = Signal(int)

    def __init__(self, index):
        super().__init__()
        self.setObjectName("row")
        self.index = index
        self.band = blank_band(index)
        self._quiet = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(5)

        top = QHBoxLayout()
        dot = QLabel("\u25cf")
        dot.setStyleSheet(
            f"color: {BAND_COLOURS[index % len(BAND_COLOURS)]}; font-size: 14px;")
        top.addWidget(dot)
        n = QLabel(str(index + 1))
        n.setObjectName("muted")
        n.setFixedWidth(14)
        top.addWidget(n)

        self.type_box = ComboBox()
        self.type_box.addItems(list(TYPE_NAMES.values()))
        self.type_box.setCurrentText("PK")
        self.type_box.setFixedWidth(70)
        self.type_box.currentIndexChanged.connect(lambda _i: self._emit())
        top.addWidget(self.type_box)
        top.addStretch(1)
        self.summary = QLabel("+0.0 dB")
        self.summary.setObjectName("muted")
        top.addWidget(self.summary)
        lay.addLayout(top)

        bot = QHBoxLayout()
        bot.setSpacing(4)

        self.freq = SpinBox()
        self.freq.setRange(FREQ_MIN, FREQ_MAX)
        self.freq.setValue(DEFAULT_FREQS[index])
        self.freq.setSuffix(" Hz")
        self.freq.setFixedWidth(82)

        self.gain = DoubleSpinBox()
        self.gain.setRange(GAIN_MIN, GAIN_MAX)
        self.gain.setSingleStep(0.5)
        self.gain.setDecimals(1)
        self.gain.setSuffix(" dB")
        self.gain.setFixedWidth(78)

        self.qbox = DoubleSpinBox()
        self.qbox.setRange(Q_MIN, Q_MAX)
        self.qbox.setSingleStep(0.1)
        self.qbox.setDecimals(2)
        self.qbox.setPrefix("Q ")
        self.qbox.setValue(DEFAULT_Q)
        self.qbox.setFixedWidth(74)

        for w in (self.freq, self.gain, self.qbox):
            w.setButtonSymbols(QAbstractSpinBox.NoButtons)
            w.setAlignment(Qt.AlignCenter)
            w.valueChanged.connect(lambda _v: self._emit())
            bot.addWidget(w)
        bot.addStretch(1)
        lay.addLayout(bot)

    def mousePressEvent(self, e):
        self.picked.emit(self.index)
        super().mousePressEvent(e)

    def set_selected(self, yes):
        self.setStyleSheet(
            f"QFrame#row {{ background: {'#2c3350' if yes else CARD_HI};"
            f" border-radius: 8px; }}")

    def load(self, band):
        self._quiet = True
        try:
            self.band = dict(band)
            self.type_box.setCurrentText(TYPE_NAMES.get(band.get("type", TYPE_PK), "PK"))
            self.freq.setValue(int(round(band["freq"])))
            self.gain.setValue(band["gain"])
            self.qbox.setValue(band["q"])
            gainless = band.get("type") in GAINLESS_TYPES
            self.gain.setEnabled(not gainless)
            self.summary.setText("gain n/a" if gainless
                                 else f"{band['gain']:+.1f} dB")
        finally:
            self._quiet = False

    def _emit(self):
        if self._quiet:
            return
        code = next((c for c, n in TYPE_NAMES.items()
                     if n == self.type_box.currentText()), TYPE_PK)
        band = {"freq": float(self.freq.value()), "gain": self.gain.value(),
                "q": self.qbox.value(), "type": code}
        self.band = band
        gainless = code in GAINLESS_TYPES
        self.gain.setEnabled(not gainless)
        self.summary.setText("gain n/a" if gainless
                             else f"{band['gain']:+.1f} dB")
        self.changed.emit(self.index, band, True)


# ---------------------------------------------------------------- worker


class Worker(QObject):
    done = Signal(str)
    state = Signal(object, object, object)

    def run(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()

# ---------------------------------------------------------------- window


class Main(QMainWindow):
    logged = Signal(str)
    bandsRead = Signal(object, object)
    settingsRead = Signal(object)

    def __init__(self, start_hidden=False):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1180, 800)

        self.presets = load_presets()
        self.profiles = load_profiles()
        self.bands = pad_bands([])
        self.preamp = 0.0
        self.selected = None
        self.current_preset = None
        self.setting_widgets = {}
        self._loading = False
        self._dirty = set()

        self._send_timer = QTimer(self)
        self._send_timer.setSingleShot(True)
        self._send_timer.setInterval(SEND_DEBOUNCE_MS)
        self._send_timer.timeout.connect(self._flush)

        self.logged.connect(self._append_log)
        self.bandsRead.connect(self._apply_read)
        self.settingsRead.connect(self._apply_settings)

        self._build()
        self._tray()
        QTimer.singleShot(250, self.read_device)
        if not start_hidden:
            self.show()

    # ------------------------------------------------------------ build

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        t = QLabel(APP_NAME)
        t.setObjectName("h1")
        head.addWidget(t)
        sub = QLabel("Space Pro")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch(1)
        self.status = QLabel("\u25cf connecting")
        self.status.setObjectName("muted")
        head.addWidget(self.status)
        lay.addLayout(head)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._eq_tab(), "Equalizer")
        self.tabs.addTab(self._device_tab(), "Device")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(64)
        lay.addWidget(self.log)

    def _eq_tab(self):
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(12)

        # ---- left: presets + bands
        left = QVBoxLayout()
        left.setSpacing(10)

        pc = card()
        pl = QVBoxLayout(pc)
        pl.setContentsMargins(12, 10, 12, 10)
        hdr = QHBoxLayout()
        h = QLabel("EQ presets")
        h.setObjectName("h2")
        hdr.addWidget(h)
        hdr.addStretch(1)
        add = QPushButton("+")
        add.setFixedWidth(30)
        add.clicked.connect(self.import_file)
        hdr.addWidget(add)
        rm = QPushButton("\u2715")
        rm.setFixedWidth(30)
        rm.clicked.connect(self.delete_preset)
        hdr.addWidget(rm)
        pl.addLayout(hdr)
        self.preset_list = QListWidget()
        self.preset_list.setFixedHeight(110)
        self.preset_list.itemClicked.connect(
            lambda it: self.load_preset(it.text()))
        pl.addWidget(self.preset_list)
        left.addWidget(pc)

        bc = card()
        bl = QVBoxLayout(bc)
        bl.setContentsMargins(12, 10, 12, 10)
        hdr2 = QHBoxLayout()
        h2 = QLabel("Bands")
        h2.setObjectName("h2")
        hdr2.addWidget(h2)
        hdr2.addStretch(1)
        hint = QLabel("drag on the graph \u00b7 scroll for Q")
        hint.setObjectName("muted")
        hdr2.addWidget(hint)
        bl.addLayout(hdr2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 6, 0)
        hv.setSpacing(5)
        self.rows = []
        for i in range(NUM_BANDS):
            r = BandRow(i)
            r.changed.connect(self.band_changed)
            r.picked.connect(self.select_band)
            hv.addWidget(r)
            self.rows.append(r)
        hv.addStretch(1)
        scroll.setWidget(holder)
        bl.addWidget(scroll, 1)
        left.addWidget(bc, 1)

        wrap = QWidget()
        wrap.setLayout(left)
        wrap.setFixedWidth(320)
        lay.addWidget(wrap)

        # ---- right: graph
        gc = card()
        gl = QVBoxLayout(gc)
        gl.setContentsMargins(14, 12, 14, 12)
        gl.setSpacing(8)

        top = QHBoxLayout()
        gh = QLabel("Frequency response")
        gh.setObjectName("h2")
        top.addWidget(gh)
        top.addStretch(1)
        self.ref_label = QLabel("")
        self.ref_label.setStyleSheet(f"color: {REF_C};")
        top.addWidget(self.ref_label)
        gl.addLayout(top)

        self.graph = Graph()
        self.graph.bandChanged.connect(self.band_changed)
        self.graph.bandSelected.connect(
            lambda i: self.select_band(None if i < 0 else i))
        gl.addWidget(self.graph, 1)

        pre = QHBoxLayout()
        pl2 = QLabel("Preamp")
        pl2.setObjectName("muted")
        pl2.setFixedWidth(52)
        pre.addWidget(pl2)
        self.preamp_slider = QSlider(Qt.Horizontal)
        self.preamp_slider.setRange(PREAMP_MIN, PREAMP_MAX)
        self.preamp_slider.valueChanged.connect(self._preamp_moved)
        self.preamp_slider.sliderReleased.connect(self._queue_preamp)
        pre.addWidget(self.preamp_slider, 1)
        self.preamp_label = QLabel("0 dB")
        self.preamp_label.setFixedWidth(52)
        pre.addWidget(self.preamp_label)
        gl.addLayout(pre)

        foot = QHBoxLayout()
        self.live = QPushButton("Live send: on")
        self.live.setCheckable(True)
        self.live.setChecked(True)
        self.live.toggled.connect(
            lambda on: self.live.setText(f"Live send: {'on' if on else 'off'}"))
        foot.addWidget(self.live)
        foot.addStretch(1)
        for text, fn in [("Re-read DAC", self.read_device),
                         ("Flatten", self.flatten),
                         ("Save preset", self.save_preset)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            foot.addWidget(b)
        ap = QPushButton("Apply")
        ap.setObjectName("primary")
        ap.clicked.connect(self.apply_eq)
        foot.addWidget(ap)
        gl.addLayout(foot)

        lay.addWidget(gc, 1)
        return page

    def _device_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 10, 0, 0)
        outer.setSpacing(12)

        # ---- profiles: EQ + every hardware setting, in one click
        pc = card()
        pl = QVBoxLayout(pc)
        pl.setContentsMargins(18, 14, 18, 14)
        h = QLabel("Profiles")
        h.setObjectName("h2")
        pl.addWidget(h)
        d = QLabel("A profile stores the EQ and every hardware setting "
                   "together. Applying one restores the whole state.")
        d.setObjectName("muted")
        d.setWordWrap(True)
        pl.addWidget(d)

        row = QHBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.setFixedHeight(120)
        self.profile_list.itemDoubleClicked.connect(
            lambda it: self.apply_profile(it.text()))
        row.addWidget(self.profile_list, 1)

        btns = QVBoxLayout()
        for text, fn in [("Apply", self.apply_selected_profile),
                         ("Save current as...", self.save_profile),
                         ("Delete", self.delete_profile)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            b.setFixedWidth(150)
            btns.addWidget(b)
        btns.addStretch(1)
        row.addLayout(btns)
        pl.addLayout(row)
        outer.addWidget(pc)

        # ---- hardware settings
        sc = card()
        sl = QVBoxLayout(sc)
        sl.setContentsMargins(18, 14, 18, 14)
        sl.setSpacing(6)
        h2 = QLabel("Hardware settings")
        h2.setObjectName("h2")
        sl.addWidget(h2)
        d2 = QLabel("Stored separately from the EQ - changing these never "
                    "disturbs your bands, and applying an EQ preset never "
                    "disturbs these.")
        d2.setObjectName("muted")
        d2.setWordWrap(True)
        sl.addWidget(d2)

        for key, (group, length, kind, opts) in SETTINGS.items():
            r = card("row")
            rl = QHBoxLayout(r)
            rl.setContentsMargins(14, 8, 14, 8)
            lab = QLabel(SETTING_LABELS[key])
            lab.setMinimumWidth(220)
            rl.addWidget(lab)
            rl.addStretch(1)
            if kind == "choice":
                w = ComboBox()
                for _v, name in opts:
                    w.addItem(name)
                w.setMinimumWidth(300)
                w.currentIndexChanged.connect(
                    lambda _i, k=key: self.setting_changed(k))
            else:
                w = DoubleSpinBox()
                w.setRange(-15, 15)
                w.setSingleStep(0.5)
                w.setDecimals(1)
                w.setSuffix(" dB")
                w.setMinimumWidth(300)
                w.editingFinished.connect(lambda k=key: self.setting_changed(k))
            self.setting_widgets[key] = w
            rl.addWidget(w)
            g = QLabel(f"0x{group:02X}")
            g.setStyleSheet(f"color: {MUTED};")
            g.setFixedWidth(44)
            g.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rl.addWidget(g)
            sl.addWidget(r)

        bar = QHBoxLayout()
        rb = QPushButton("Read from DAC")
        rb.clicked.connect(self.read_settings_async)
        bar.addWidget(rb)
        self.startup_btn = QPushButton()
        self.startup_btn.setCheckable(True)
        self.startup_btn.setChecked(startup_enabled())
        self._sync_startup_text()
        self.startup_btn.toggled.connect(self.toggle_startup)
        bar.addStretch(1)
        bar.addWidget(self.startup_btn)
        sl.addLayout(bar)
        outer.addWidget(sc)

        note = QLabel(
            "Channel balance is not available: our decode of that field was "
            "wrong and writing it caused a dangerous volume jump. Use the "
            "official app for balance.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        outer.addWidget(note)
        outer.addStretch(1)

        wrap = QScrollArea()
        wrap.setWidgetResizable(True)
        wrap.setWidget(page)
        page = wrap

        self._refresh_profiles()
        return page

    # ------------------------------------------------------------ helpers

    def _append_log(self, msg):
        self.log.appendPlainText(msg)

    def say(self, msg):
        self.logged.emit(msg)

    def _sync_startup_text(self):
        on = self.startup_btn.isChecked()
        self.startup_btn.setText(
            f"Start with Windows: {'on' if on else 'off'}")

    def _sync_graph(self):
        ref = None
        if self.current_preset:
            p = next((x for x in self.presets
                      if x["name"] == self.current_preset), None)
            if p:
                ref = (pad_bands(p["bands"]), p["preamp"])
        self.graph.set_state(self.bands, self.preamp, ref, self.selected)

    def select_band(self, i):
        self.selected = i
        for n, r in enumerate(self.rows):
            r.set_selected(n == i)
        self._sync_graph()

    def load_bands(self, bands, preamp):
        self.bands = pad_bands(bands)
        self.preamp = preamp
        self.preamp_slider.blockSignals(True)
        self.preamp_slider.setValue(int(round(preamp)))
        self.preamp_slider.blockSignals(False)
        self.preamp_label.setText(f"{preamp:+.0f} dB")
        for i, r in enumerate(self.rows):
            r.load(self.bands[i])
        self._sync_graph()

    # ------------------------------------------------------------ device

    def read_device(self):
        self.say("Reading from the DAC...")

        def work():
            try:
                d = find_device()
            except Exception as e:
                self.say(f"hidapi not usable: {e}")
                return
            if not d:
                self.say("DAC not found.")
                self.settingsRead.emit(None)
                return
            try:
                bands, pre, st = read_bands(), read_preamp(), read_settings()
            except Exception as e:
                self.say(f"Read failed: {e}")
                return
            got = [{"freq": v["freq"], "gain": v["gain"], "q": v["q"],
                    "type": v["type"]}
                   for i in range(NUM_BANDS) if (v := bands.get(i))]
            self.bandsRead.emit(got, pre)
            self.settingsRead.emit(st)

        threading.Thread(target=work, daemon=True).start()

    def _apply_read(self, bands, preamp):
        self.status.setText("\u25cf connected")
        self.status.setStyleSheet("color: #34d399;")
        if bands:
            self.load_bands(bands, 0 if preamp is None else preamp)
            self.say(f"Loaded {len(bands)} bands, preamp "
                     f"{0 if preamp is None else preamp:+d} dB")

    def read_settings_async(self):
        def work():
            try:
                self.settingsRead.emit(read_settings())
            except Exception as e:
                self.say(f"Could not read settings: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _apply_settings(self, got):
        if not got:
            self.status.setText("\u25cf not found")
            self.status.setStyleSheet("color: #f87171;")
            return
        self._loading = True
        try:
            for key, value in got.items():
                _g, _l, kind, opts = SETTINGS.get(key, (0, 0, None, None))
                w = self.setting_widgets.get(key)
                if w is None:
                    continue
                if kind == "choice":
                    name = next((n for v, n in opts if v == value), None)
                    if name:
                        w.setCurrentText(name)
                else:
                    w.setValue(float(value))
            self.say("Read hardware settings")
        finally:
            self._loading = False

    def current_settings(self):
        out = {}
        for key, (_g, _l, kind, opts) in SETTINGS.items():
            w = self.setting_widgets.get(key)
            if w is None:
                continue
            if kind == "choice":
                out[key] = next((v for v, n in opts
                                 if n == w.currentText()), opts[0][0])
            else:
                out[key] = w.value()
        return out

    def setting_changed(self, key):
        if self._loading:
            return
        _g, _l, kind, opts = SETTINGS[key]
        w = self.setting_widgets[key]
        if kind == "choice":
            value = next((v for v, n in opts if n == w.currentText()), None)
            if value is None:
                return
            shown = w.currentText()
        else:
            value = max(-15.0, min(15.0, w.value()))
            shown = f"{value:g} dB"
        self._send(build_setting_packet(key, value),
                   f"{SETTING_LABELS[key]} \u2192 {shown}")

    # ------------------------------------------------------------ editing

    def band_changed(self, index, band, commit):
        self.bands[index] = band
        if commit:
            self.rows[index].load(band)
            self.select_band(index)
        else:
            self.selected = index
        if not self.live.isChecked():
            return
        self._dirty.add(index)
        self._send_timer.start()

    def _flush(self):
        pending, self._dirty = sorted(self._dirty), set()
        packets = []
        for i in pending:
            packets.extend(build_single_band(i, self.bands[i]))
        if getattr(self, "_preamp_dirty", False):
            self._preamp_dirty = False
            packets += [build_preamp(self.preamp), _cmd(0x04, 0x00)]
        if packets:
            self._send(packets, None)

    def _preamp_moved(self, value):
        self.preamp = float(value)
        self.preamp_label.setText(f"{self.preamp:+.0f} dB")
        self._sync_graph()

    def _queue_preamp(self):
        if not self.live.isChecked():
            return
        self._preamp_dirty = True
        self._send_timer.start()

    # ------------------------------------------------------------ writes

    def _send(self, packets, msg):
        def work():
            try:
                send_packets(packets)
                if msg:
                    self.say(msg)
            except Exception as e:
                self.say(f"FAILED: {e}")

        threading.Thread(target=work, daemon=True).start()

    def apply_eq(self):
        self._send(build_sequence(self.preamp, self.bands), "Applied EQ")

    def flatten(self):
        self.load_bands([], 0)
        self._send(build_sequence(0.0, []), "Flattened")

    # ------------------------------------------------------------ presets

    def _refresh_presets(self):
        self.preset_list.clear()
        for p in self.presets:
            self.preset_list.addItem(QListWidgetItem(p["name"]))
        self._rebuild_tray()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a parametric EQ file", "", "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                preamp, bands, warns = parse_peq_text(f.read())
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        for w in warns:
            self.say(f"  ! {w}")
        name = os.path.splitext(os.path.basename(path))[0][:40]
        self._store_preset(name, preamp, bands)

    def save_preset(self):
        self._store_preset(self.current_preset or "New preset",
                           self.preamp, [dict(b) for b in self.bands])

    def _store_preset(self, default, preamp, bands):
        name, ok = QInputDialog.getText(self, APP_NAME, "Preset name:",
                                        text=default)
        if not ok or not name.strip():
            return
        name = name.strip()
        self.presets = [p for p in self.presets if p["name"] != name]
        self.presets.append({"name": name, "preamp": preamp, "bands": bands})
        save_presets(self.presets)
        self._refresh_presets()
        self.load_preset(name)
        self.say(f'Saved preset "{name}"')

    def load_preset(self, name):
        p = next((x for x in self.presets if x["name"] == name), None)
        if not p:
            return
        self.current_preset = name
        self.load_bands(p["bands"], p["preamp"])
        self.ref_label.setText(f"reference: {name}")
        self.say(f'Loaded "{name}" into the editor')

    def delete_preset(self):
        it = self.preset_list.currentItem()
        if not it:
            return
        name = it.text()
        if QMessageBox.question(self, APP_NAME,
                                f'Delete preset "{name}"?') != QMessageBox.Yes:
            return
        self.presets = [p for p in self.presets if p["name"] != name]
        save_presets(self.presets)
        if self.current_preset == name:
            self.current_preset = None
            self.ref_label.setText("")
        self._refresh_presets()
        self._sync_graph()

    # ------------------------------------------------------------ profiles

    def _refresh_profiles(self):
        self.profile_list.clear()
        for p in self.profiles:
            self.profile_list.addItem(QListWidgetItem(p["name"]))
        self._rebuild_tray()

    def save_profile(self):
        name, ok = QInputDialog.getText(self, APP_NAME,
                                        "Profile name (EQ + all settings):")
        if not ok or not name.strip():
            return
        name = name.strip()
        self.profiles = [p for p in self.profiles if p["name"] != name]
        self.profiles.append({
            "name": name,
            "preamp": self.preamp,
            "bands": [dict(b) for b in self.bands],
            "settings": self.current_settings(),
        })
        save_profiles(self.profiles)
        self._refresh_profiles()
        self.say(f'Saved profile "{name}" (EQ + hardware settings)')

    def apply_selected_profile(self):
        it = self.profile_list.currentItem()
        if it:
            self.apply_profile(it.text())

    def apply_profile(self, name):
        p = next((x for x in self.profiles if x["name"] == name), None)
        if not p:
            return
        self.load_bands(p.get("bands", []), p.get("preamp", 0))
        self._loading = True
        try:
            for key, value in (p.get("settings") or {}).items():
                _g, _l, kind, opts = SETTINGS.get(key, (0, 0, None, None))
                w = self.setting_widgets.get(key)
                if w is None:
                    continue
                if kind == "choice":
                    nm = next((n for v, n in opts if v == value), None)
                    if nm:
                        w.setCurrentText(nm)
                else:
                    w.setValue(float(value))
        finally:
            self._loading = False
        self._send(build_profile_packets(p), f'Applied profile "{name}"')

    def delete_profile(self):
        it = self.profile_list.currentItem()
        if not it:
            return
        name = it.text()
        if QMessageBox.question(self, APP_NAME,
                                f'Delete profile "{name}"?') != QMessageBox.Yes:
            return
        self.profiles = [p for p in self.profiles if p["name"] != name]
        save_profiles(self.profiles)
        self._refresh_profiles()

    # ------------------------------------------------------------ startup

    def toggle_startup(self, on):
        try:
            set_startup(on)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            self.startup_btn.setChecked(startup_enabled())
        self._sync_startup_text()

    # ------------------------------------------------------------ tray

    def _tray(self):
        self.tray_icon = QSystemTrayIcon(app_icon(), self)
        self.tray_icon.setToolTip(APP_NAME)
        self.tray_icon.activated.connect(
            lambda r: self.show_window()
            if r == QSystemTrayIcon.Trigger else None)
        self._rebuild_tray()
        self.tray_icon.show()

    def _rebuild_tray(self):
        if not hasattr(self, "tray_icon"):
            return
        menu = QMenu()
        act = QAction("Show window", self)
        act.triggered.connect(self.show_window)
        menu.addAction(act)
        if self.profiles:
            menu.addSeparator()
            head = QAction("Profiles", self)
            head.setEnabled(False)
            menu.addAction(head)
            for p in self.profiles:
                a = QAction(p["name"], self)
                a.triggered.connect(
                    lambda _c=False, n=p["name"]: self.apply_profile(n))
                menu.addAction(a)
        if self.presets:
            menu.addSeparator()
            head = QAction("EQ presets", self)
            head.setEnabled(False)
            menu.addAction(head)
            for p in self.presets:
                a = QAction(p["name"], self)
                a.triggered.connect(
                    lambda _c=False, n=p["name"]: self.apply_preset_direct(n))
                menu.addAction(a)
        menu.addSeparator()
        q = QAction("Quit", self)
        q.triggered.connect(QApplication.quit)
        menu.addAction(q)
        self.tray_icon.setContextMenu(menu)
        self._menu = menu

    def apply_preset_direct(self, name):
        p = next((x for x in self.presets if x["name"] == name), None)
        if p:
            self._send(build_sequence(p["preamp"], p["bands"]),
                       f'Applied "{name}"')

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        e.ignore()
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    w = Main(start_hidden="--tray" in sys.argv)
    w._refresh_presets()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
