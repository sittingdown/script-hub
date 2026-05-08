"""
Key Spammer  —  Hub plugin
Spams E → W → A. Auto-pauses when FiveM loses focus.
Tabs: Dashboard | Settings
"""
import os
import json
import time
import threading
from pathlib import Path

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

import win32gui
import win32process
import win32api
import psutil
from pynput.keyboard import Listener, Controller as KeyCtrl, KeyCode
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton

from hub_sdk import (
    PluginBase, T1, T2, T3, GREEN, AMBER, lbl, sep, kbd,
    make_tabs, section_header, NumericStepper,
    is_plugin_active,
)

# ── config ────────────────────────────────────────────────────────────────────

_CFG = Path(__file__).parent.parent / "cfg" / "key_spammer_config.json"
_DEF = {"hotkey": "f1", "hold": 0.06, "gap": 0.06, "cycle": 0.10}

def _load():
    try:    return {**_DEF, **json.loads(_CFG.read_text())}
    except: return dict(_DEF)

def _save(c):
    try:
        _CFG.parent.mkdir(parents=True, exist_ok=True)
        _CFG.write_text(json.dumps(c, indent=2))
    except: pass

cfg = _load()

# ── VK resolver ───────────────────────────────────────────────────────────────

_VK_MAP = {
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "space":0x20,"enter":0x0D,"esc":0x1B,"escape":0x1B,"tab":0x09,
}

def _vk(s: str) -> int:
    s = s.lower()
    if s in _VK_MAP: return _VK_MAP[s]
    if len(s) == 1:  return ord(s.upper())
    return 0

# ── FiveM detection ───────────────────────────────────────────────────────────

def _fivem_focused() -> bool:
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return "fivem" in psutil.Process(pid).name().lower()
    except:
        return False

# ── bridge ────────────────────────────────────────────────────────────────────

class _Bridge(QObject):
    update = pyqtSignal()
    toggle = pyqtSignal()

_bridge = _Bridge()

# ── spammer core ──────────────────────────────────────────────────────────────

_kbd_ctrl = KeyCtrl()

class _Spammer:
    KEYS = ["e", "w", "a"]

    def __init__(self):
        self.active = False

    def toggle(self):
        self.active = not self.active
        if self.active:
            threading.Thread(target=self._loop, daemon=True).start()
        _bridge.update.emit()

    def stop(self):
        self.active = False
        _bridge.update.emit()

    def _loop(self):
        while self.active:
            if not _fivem_focused():
                _bridge.update.emit()
                time.sleep(0.25)
                continue
            for ch in self.KEYS:
                if not self.active: break
                k = KeyCode.from_char(ch)
                _kbd_ctrl.press(k);  time.sleep(cfg.get("hold",  0.06))
                _kbd_ctrl.release(k); time.sleep(cfg.get("gap",   0.06))
            time.sleep(cfg.get("cycle", 0.10))
        _bridge.update.emit()

_spammer = _Spammer()
_bridge.toggle.connect(_spammer.toggle)

# ── layout helper ─────────────────────────────────────────────────────────────

def _hrow(*widgets):
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(10)
    for i, w in enumerate(widgets):
        h.addWidget(w)
        if i == 0: h.addStretch()
    return h

# ── Plugin ────────────────────────────────────────────────────────────────────

class Plugin(PluginBase):
    NAME        = "Key Spammer"
    DESCRIPTION = "Spams E → W → A on repeat. Auto-pauses the moment FiveM loses window focus."
    ACCENT      = "#60a5fa"
    TAGS        = ["FiveM", "Input"]
    VERSION     = "1.2.3"

    def __init__(self):
        self._hk_gen    = 0
        self._capturing = False
        self._cap_lst   = None
        self._badge     = None   # set by _build_settings

    # ── hub card status ───────────────────────────────────────────────────────

    def status(self):
        if _spammer.active:
            return ("RUNNING", GREEN) if _fivem_focused() else ("PAUSED", AMBER)
        return "STOPPED", T3

    # ── build_page ────────────────────────────────────────────────────────────

    def build_page(self, nav_back):
        container, stack = make_tabs(["Dashboard", "Settings"])
        stack.addWidget(self._build_dashboard())
        stack.addWidget(self._build_settings())   # sets self._badge
        self._start_poll()
        return container

    # ══════════════════════════════════════════════════════════════════════════
    # Dashboard
    # ══════════════════════════════════════════════════════════════════════════

    def _build_dashboard(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addStretch()

        state_lbl = lbl("STOPPED", sz=22, col=T3, bold=True,
                        align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(state_lbl)
        root.addStretch()

        # ── hotkey preview chip ───────────────────────────────────────────────
        hk_row = QHBoxLayout()
        hk_row.addStretch()
        hk_row.addWidget(lbl("Toggle", sz=9, col=T3))
        hk_row.addSpacing(6)
        hk_chip = kbd(cfg.get("hotkey", "f1"))
        hk_row.addWidget(hk_chip)
        hk_row.addStretch()
        root.addLayout(hk_row)

        root.addStretch()
        root.addWidget(lbl(
            "E → W → A  •  auto-pauses when FiveM loses focus",
            sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter,
        ))

        def refresh():
            hk = cfg.get("hotkey", "f1").upper()
            hk_chip.setText(hk)
            if _spammer.active:
                if _fivem_focused():
                    state_lbl.setText("RUNNING")
                    state_lbl.setStyleSheet(
                        f"color:{GREEN}; font-size:22px; font-weight:700; background:transparent;")
                else:
                    state_lbl.setText("PAUSED — focus FiveM")
                    state_lbl.setStyleSheet(
                        f"color:{AMBER}; font-size:22px; font-weight:700; background:transparent;")
            else:
                state_lbl.setText(f"STOPPED — press {hk}")
                state_lbl.setStyleSheet(
                    f"color:{T3}; font-size:22px; font-weight:700; background:transparent;")

        _bridge.update.connect(refresh)
        timer = QTimer(page)
        timer.timeout.connect(_bridge.update.emit)
        timer.start(500)
        refresh()
        return page

    # ══════════════════════════════════════════════════════════════════════════
    # Settings
    # ══════════════════════════════════════════════════════════════════════════

    def _build_settings(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 8, 0, 8)
        root.setSpacing(0)

        # ── Hotkey ────────────────────────────────────────────────────────────
        root.addWidget(section_header("Hotkey"))
        root.addSpacing(8)

        badge = QPushButton(cfg.get("hotkey", "f1").upper())
        badge.setObjectName("badge")
        badge.setCursor(Qt.CursorShape.PointingHandCursor)
        badge.setToolTip("Click to rebind")
        badge.setProperty("capturing", False)
        badge.clicked.connect(self._start_capture)
        self._badge = badge   # _start_capture uses this ref
        root.addLayout(_hrow(lbl("Toggle Spammer", sz=12, col=T2), badge))
        root.addSpacing(20)

        # ── Timing ────────────────────────────────────────────────────────────
        root.addWidget(section_header("Timing"))
        root.addSpacing(8)

        for tname, tkey, tdef, tstep, tmin, tmax in [
            ("Key Hold (s)",    "hold",  0.06, 0.01, 0.01, 1.0),
            ("Key Gap (s)",     "gap",   0.06, 0.01, 0.01, 1.0),
            ("Cycle Delay (s)", "cycle", 0.10, 0.01, 0.01, 2.0),
        ]:
            st = NumericStepper(cfg.get(tkey, tdef), step=tstep,
                                min_val=tmin, max_val=tmax, fmt="{:.2f}")
            def _on_change(v, k=tkey):
                cfg[k] = v
                _save(cfg)
            st.changed.connect(_on_change)
            root.addLayout(_hrow(lbl(tname, sz=12, col=T2), st))
            root.addSpacing(8)

        root.addSpacing(8)
        root.addWidget(sep())
        root.addSpacing(14)
        root.addWidget(lbl("Key sequence: E → W → A  (fixed)", sz=11, col=T3))
        root.addStretch()
        return page

    # ── hotkey poll ───────────────────────────────────────────────────────────

    def _start_poll(self):
        self._hk_gen += 1
        my_gen = self._hk_gen
        vk = _vk(cfg.get("hotkey", "f1"))
        if not vk:
            return

        def poll():
            last = False
            time.sleep(0.06)
            while self._hk_gen == my_gen:
                try:
                    down = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
                    if down and not last and not self._capturing \
                            and is_plugin_active("Key Spammer"):
                        _bridge.toggle.emit()
                    last = down
                except Exception:
                    pass
                time.sleep(0.05)

        threading.Thread(target=poll, daemon=True).start()

    # ── hotkey rebind capture ─────────────────────────────────────────────────

    def _start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self._hk_gen += 1   # stop poll thread

        if self._badge:
            self._badge.setText("…")
            self._badge.setProperty("capturing", True)
            self._badge.style().unpolish(self._badge)
            self._badge.style().polish(self._badge)

        def listen():
            def on_press(key):
                try:
                    k = key.char if hasattr(key, "char") and key.char else key.name
                except Exception:
                    k = str(key).replace("Key.", "")

                cfg["hotkey"] = k.lower()
                _save(cfg)

                def apply():
                    if self._badge:
                        self._badge.setText(k.upper())
                        self._badge.setProperty("capturing", False)
                        self._badge.style().unpolish(self._badge)
                        self._badge.style().polish(self._badge)
                    self._capturing = False
                    self._start_poll()
                    _bridge.update.emit()   # pushes new key to dashboard chip

                QTimer.singleShot(0, apply)
                return False

            with Listener(on_press=on_press) as lst:
                self._cap_lst = lst
                lst.join()

        threading.Thread(target=listen, daemon=True).start()

    # ── on_unload ─────────────────────────────────────────────────────────────

    def on_unload(self):
        """Stop all background threads before plugin is destroyed on hot-reload."""
        # Stop the spammer loop
        try:
            _spammer.stop()
        except Exception:
            pass

        # Kill the poll thread by advancing the generation counter
        self._hk_gen += 1

        # Stop any in-progress key capture listener
        if self._cap_lst is not None:
            try:
                self._cap_lst.stop()
            except Exception:
                pass
            self._cap_lst = None
