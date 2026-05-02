"""
Plugin Template  —  copy this file, rename it (remove the leading _), and fill it in.
The hub ignores any file starting with _ so this won't appear as a card.

Quickstart
----------
1. Copy this file to plugins/my_tool.py
2. Fill in NAME, DESCRIPTION, ACCENT, TAGS
3. Implement build_page() — return any QWidget
4. Implement status()     — return (text, colour)
5. Restart the hub

Full docs: PLUGIN_TEMPLATE.md
"""
import json
import time
import threading
from pathlib import Path

import win32api
from pynput.keyboard import Listener
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton

from hub_sdk import (
    PluginBase,
    T1, T2, T3, GREEN, AMBER,
    lbl, sep, kbd,
    make_tabs, scrollable, section_header,
    ToggleSwitch, NumericStepper,
    StatusLayout,
)

# ── config ────────────────────────────────────────────────────────────────────
# Saves to cfg/myplugin_config.json in the project root.
# Rename the file to match your plugin.

_CFG = Path(__file__).parent.parent / "cfg" / "myplugin_config.json"
_DEF = {"hotkey": "f9"}   # add your own default keys/values here

def _load():
    try:    return {**_DEF, **json.loads(_CFG.read_text())}
    except: return dict(_DEF)

def _save(c):
    try:
        _CFG.parent.mkdir(parents=True, exist_ok=True)
        _CFG.write_text(json.dumps(c, indent=2))
    except: pass

cfg = _load()

# ── VK resolver (F-keys + single letters) ─────────────────────────────────────
_VK_MAP = {
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "space":0x20,"enter":0x0D,"esc":0x1B,"tab":0x09,
}
def _vk(s: str) -> int:
    s = s.lower()
    return _VK_MAP.get(s, ord(s.upper()) if len(s) == 1 else 0)

# ── bridge: worker thread → Qt main thread ────────────────────────────────────
class _Bridge(QObject):
    update = pyqtSignal()   # trigger a UI refresh from any thread
    toggle = pyqtSignal()   # safely call toggle() from a poll thread

_bridge = _Bridge()

# ── your core logic goes here ─────────────────────────────────────────────────
class _Worker:
    """Replace with whatever your plugin actually does."""

    def __init__(self):
        self.active = False

    def toggle(self):
        self.active = not self.active
        if self.active:
            threading.Thread(target=self._run, daemon=True).start()
        _bridge.update.emit()

    def stop(self):
        self.active = False
        _bridge.update.emit()

    def _run(self):
        while self.active:
            # --- do your work here ---
            time.sleep(0.05)
        _bridge.update.emit()

_worker = _Worker()
_bridge.toggle.connect(_worker.toggle)

# ── layout helper ─────────────────────────────────────────────────────────────
def _hrow(*widgets):
    """HBox: first widget left, rest right-aligned."""
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(10)
    for i, w in enumerate(widgets):
        h.addWidget(w)
        if i == 0: h.addStretch()
    return h

# ── Plugin ────────────────────────────────────────────────────────────────────

class Plugin(PluginBase):
    NAME        = "My Plugin"          # shown on the hub card
    DESCRIPTION = "Short description." # shown on the hub card
    ACCENT      = "#a78bfa"            # dot colour on the card (any hex)
    TAGS        = ["FiveM"]            # chips on the card
    VERSION     = "1.0"

    def __init__(self):
        self._hk_gen    = 0
        self._capturing = False
        self._badge     = None   # set in _build_settings

    # ── hub card live status ──────────────────────────────────────────────────

    def status(self):
        """Called every 500 ms. Return (text, colour) for the hub card dot."""
        if _worker.active:
            return "RUNNING", GREEN
        return "STOPPED", T3

    # ── page ──────────────────────────────────────────────────────────────────

    def build_page(self, nav_back):
        """
        Return the QWidget shown when the card is opened.
        nav_back() navigates back to the hub — you don't need to call it.
        """
        # Option A — single page (simple plugins)
        # return self._build_simple()

        # Option B — tabbed layout (recommended for anything with settings)
        container, stack = make_tabs(["Dashboard", "Settings"])
        stack.addWidget(self._build_dashboard())
        stack.addWidget(self._build_settings())
        self._start_poll()
        return container

    # ══════════════════════════════════════════════════════════════════════════
    # Option A — simple single-page layout
    # ══════════════════════════════════════════════════════════════════════════

    def _build_simple(self):
        page = StatusLayout(hint="press F9 to toggle")

        badge = page.set_hotkey_row(cfg.get("hotkey", "f9"),
                                    on_rebind=self._start_capture)
        self._badge = badge

        def refresh():
            hk = cfg.get("hotkey", "f9").upper()
            if _worker.active:
                page.set_status("RUNNING", GREEN)
            else:
                page.set_status(f"STOPPED — press {hk}", T3)

        _bridge.update.connect(refresh)
        t = QTimer(page); t.timeout.connect(_bridge.update.emit); t.start(500)
        self._start_poll()
        refresh()
        return page

    # ══════════════════════════════════════════════════════════════════════════
    # Option B — Dashboard tab
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

        # hotkey preview chip
        hk_row = QHBoxLayout()
        hk_row.addStretch()
        hk_row.addWidget(lbl("Toggle", sz=9, col=T3))
        hk_row.addSpacing(6)
        hk_chip = kbd(cfg.get("hotkey", "f9"))
        hk_row.addWidget(hk_chip)
        hk_row.addStretch()
        root.addLayout(hk_row)

        root.addStretch()
        root.addWidget(lbl("auto-pauses when FiveM loses focus",
                           sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter))

        def refresh():
            hk = cfg.get("hotkey", "f9").upper()
            hk_chip.setText(hk)
            if _worker.active:
                state_lbl.setText("RUNNING")
                state_lbl.setStyleSheet(
                    f"color:{GREEN}; font-size:22px; font-weight:700; background:transparent;")
            else:
                state_lbl.setText(f"STOPPED — press {hk}")
                state_lbl.setStyleSheet(
                    f"color:{T3}; font-size:22px; font-weight:700; background:transparent;")

        _bridge.update.connect(refresh)
        t = QTimer(page); t.timeout.connect(_bridge.update.emit); t.start(500)
        refresh()
        return page

    # ══════════════════════════════════════════════════════════════════════════
    # Option B — Settings tab
    # ══════════════════════════════════════════════════════════════════════════

    def _build_settings(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 8, 0, 8)
        root.setSpacing(0)

        # ── Hotkey ────────────────────────────────────────────────────────────
        root.addWidget(section_header("Hotkey"))
        root.addSpacing(8)

        badge = QPushButton(cfg.get("hotkey", "f9").upper())
        badge.setObjectName("badge")
        badge.setCursor(Qt.CursorShape.PointingHandCursor)
        badge.setToolTip("Click to rebind")
        badge.setProperty("capturing", False)
        badge.clicked.connect(self._start_capture)
        self._badge = badge
        root.addLayout(_hrow(lbl("Toggle", sz=12, col=T2), badge))
        root.addSpacing(20)

        # ── Example: numeric setting ──────────────────────────────────────────
        root.addWidget(section_header("Settings"))
        root.addSpacing(8)

        speed = NumericStepper(cfg.get("speed", 1.0),
                               step=0.1, min_val=0.1, max_val=10.0, fmt="{:.1f}")
        def _on_speed(v):
            cfg["speed"] = v
            _save(cfg)
        speed.changed.connect(_on_speed)
        root.addLayout(_hrow(lbl("Speed", sz=12, col=T2), speed))
        root.addSpacing(12)

        # ── Example: toggle setting ───────────────────────────────────────────
        sw = ToggleSwitch(cfg.get("enabled", True))
        def _on_toggle(v):
            cfg["enabled"] = v
            _save(cfg)
        sw.toggled.connect(_on_toggle)
        root.addLayout(_hrow(lbl("Some option", sz=12, col=T2), sw))

        root.addStretch()
        return page

    # ── hotkey poll ───────────────────────────────────────────────────────────

    def _start_poll(self):
        self._hk_gen += 1
        my_gen = self._hk_gen
        vk = _vk(cfg.get("hotkey", "f9"))
        if not vk:
            return

        def poll():
            last = False
            time.sleep(0.06)
            while self._hk_gen == my_gen:
                try:
                    down = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
                    if down and not last and not self._capturing:
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
        self._hk_gen += 1   # stop poll during capture

        if self._badge:
            self._badge.setText("…")
            self._badge.setProperty("capturing", True)
            self._badge.style().unpolish(self._badge)
            self._badge.style().polish(self._badge)

        def listen():
            def on_press(key):
                try:    k = key.char if hasattr(key, "char") and key.char else key.name
                except: k = str(key).replace("Key.", "")
                k = k.lower()
                cfg["hotkey"] = k
                _save(cfg)

                def apply():
                    if self._badge:
                        self._badge.setText(k.upper())
                        self._badge.setProperty("capturing", False)
                        self._badge.style().unpolish(self._badge)
                        self._badge.style().polish(self._badge)
                    self._capturing = False
                    self._start_poll()
                    _bridge.update.emit()

                QTimer.singleShot(0, apply)
                return False   # one key only

            with Listener(on_press=on_press) as lst:
                lst.join()

        threading.Thread(target=listen, daemon=True).start()
