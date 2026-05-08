"""
Magnet Bot  —  Hub plugin (full-featured)
Tabs: Dashboard | Settings | Stats | Hotkeys

Integrates BotEngine, AdaptiveEngine, ConfigManager, and all three overlay
selectors (PointSelectorOverlay, RegionSelectorOverlay, ColorPickerOverlay).
"""
import sys
import time
import threading
from pathlib import Path

# project root holds bot.py, config.py, adaptive.py, overlays_qt.py, etc.
sys.path.insert(0, str(Path(__file__).parent.parent))

import urllib.request
import json as _json
import win32api
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QButtonGroup, QPushButton,
    QProgressBar, QTextEdit, QFrame, QLabel, QLineEdit,
)
from pynput.keyboard import Listener

from hub_sdk import (
    PluginBase, T1, T2, T3, GREEN, AMBER, lbl, sep, kbd,
    make_tabs, scrollable, section_header,
    ToggleSwitch, NumericStepper, ColorSwatch,
    is_plugin_active,
)
from config import ConfigManager, LEVEL_PRESETS
from bot import BotEngine, State, is_fivem_focused
from adaptive import AdaptiveEngine
from detector import get_click_targets
from overlays_qt import ColorPickerOverlay, RegionSelectorOverlay, PointSelectorOverlay

# ── shared config ──────────────────────────────────────────────────────────────
_cfg = ConfigManager()

# ── bridge: bot thread → Qt main thread ───────────────────────────────────────
class _Bridge(QObject):
    update        = pyqtSignal()
    cooldown      = pyqtSignal(float, float, str)   # elapsed, total, label
    log_msg       = pyqtSignal(str)
    # setup hotkey triggers — emitted from pynput thread, delivered on main thread
    hk_wait_point    = pyqtSignal()
    hk_catch_region  = pyqtSignal()
    hk_color_picker  = pyqtSignal()
    hk_emergency     = pyqtSignal()

_bridge = _Bridge()

# ── plugin reference (set in Plugin.__init__, used by bridge signal dispatch) ──
_plugin_ref: list = [None]
_bridge.hk_wait_point.connect(  lambda: _plugin_ref[0] and _plugin_ref[0]._do_select_wait_point())
_bridge.hk_catch_region.connect(lambda: _plugin_ref[0] and _plugin_ref[0]._do_select_catch_region())
_bridge.hk_color_picker.connect(lambda: _plugin_ref[0] and _plugin_ref[0]._do_open_color_picker())
_bridge.hk_emergency.connect(   lambda: _plugin_ref[0] and _plugin_ref[0]._do_emergency_stop())

# ── adaptive engine ───────────────────────────────────────────────────────────
_adaptive = AdaptiveEngine(
    _cfg,
    on_adjustment=lambda t, m: _bridge.log_msg.emit(f"[Adaptive] {t}: {m}"),
)
_cfg.add_watcher(
    lambda keys, old, new, src: _adaptive.on_user_override(keys, old, new)
    if src == "user" else None
)

# ── VK resolver ───────────────────────────────────────────────────────────────
_VK_MAP = {
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "space":0x20,"enter":0x0D,"esc":0x1B,"escape":0x1B,"tab":0x09,
}
def _vk(s: str) -> int:
    s = s.lower()
    return _VK_MAP.get(s, ord(s.upper()) if len(s) == 1 else 0)

# ── state labels ───────────────────────────────────────────────────────────────
_STATE_TEXT = {
    State.IDLE:       ("STOPPED",               T3),
    State.PRESS_E:    ("Pressing key…",         T2),
    State.WAIT_START: ("Waiting for minigame…", T2),
    State.MOVE_MOUSE: ("Positioning…",          T2),
    State.SCANNING:   ("Scanning…",             GREEN),
    State.CLICKING:   ("Clicking!",             GREEN),
    State.WAIT_END:   ("Cooldown…",             AMBER),
}

# Short labels for the in-game overlay
_OV_TEXT = {
    State.IDLE:       ("Stopped",   T3),
    State.PRESS_E:    ("Pressing",  T2),
    State.WAIT_START: ("Starting",  T2),
    State.MOVE_MOUSE: ("Moving",    T2),
    State.SCANNING:   ("Scanning",  GREEN),
    State.CLICKING:   ("Clicking!", GREEN),
    State.WAIT_END:   ("Cooldown",  AMBER),
}

# ── status overlay ─────────────────────────────────────────────────────────────

class _StatusOverlay(QWidget):
    """
    Transparent, always-on-top, click-through overlay that shows bot state
    plus optional info rows (loops, rate, runtime).
    Normally passes all mouse/keyboard events through to whatever is below.
    Becomes draggable when enter_move_mode() is called.
    Auto-hides when the Magnet Bot plugin is not the active page.
    """

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background: transparent;")
        self._drag_pos = None
        self._move_mode = False

        from PyQt6.QtWidgets import QLabel as _QLabel

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QFrame()
        self._card.setObjectName("ov_card")
        self._card.setStyleSheet(
            "QFrame#ov_card {"
            "  background: rgba(8, 8, 8, 210);"
            "  border-radius: 10px;"
            "  border: 1px solid rgba(255,255,255,18);"
            "}"
            "QLabel { background: transparent; border: none; }"
        )

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(12, 8, 14, 8)
        card_lay.setSpacing(4)

        # ── status row: dot + state text ──────────────────────────────────────
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(7)

        self._dot = QFrame()
        self._dot.setFixedSize(8, 8)
        self._dot.setStyleSheet(f"background:{T3}; border-radius:4px; border:none;")

        self._lbl = _QLabel("Stopped")
        self._lbl.setStyleSheet(
            f"color:{T3}; font-size:12px; font-weight:700;"
            " font-family:'Segoe UI',sans-serif;"
        )

        status_row.addWidget(self._dot)
        status_row.addWidget(self._lbl)
        card_lay.addLayout(status_row)

        # ── optional info labels (hidden by default) ──────────────────────────
        _info_style = (
            f"color:{T2}; font-size:10px;"
            " font-family:'Segoe UI',sans-serif;"
        )
        self._loops_lbl   = _QLabel("")
        self._rate_lbl    = _QLabel("")
        self._runtime_lbl = _QLabel("")
        for il in (self._loops_lbl, self._rate_lbl, self._runtime_lbl):
            il.setStyleSheet(_info_style)
            il.hide()
        card_lay.addWidget(self._loops_lbl)
        card_lay.addWidget(self._rate_lbl)
        card_lay.addWidget(self._runtime_lbl)

        outer.addWidget(self._card)
        self.adjustSize()

    # ── state update ──────────────────────────────────────────────────────────

    def set_state(self, text: str, color: str):
        self._lbl.setText(text)
        self._lbl.setStyleSheet(
            f"color:{color}; font-size:12px; font-weight:700;"
            " font-family:'Segoe UI',sans-serif;"
        )
        self._dot.setStyleSheet(
            f"background:{color}; border-radius:4px; border:none;"
        )
        self.adjustSize()

    def set_info(self, loops: int, rate: float, runtime: float, opts: dict):
        """Show/hide and populate the optional info rows."""
        if opts.get("show_loops", False):
            self._loops_lbl.setText(f"{loops} loops")
            self._loops_lbl.show()
        else:
            self._loops_lbl.hide()

        if opts.get("show_rate", False):
            self._rate_lbl.setText(f"{rate:.1f} / hr")
            self._rate_lbl.show()
        else:
            self._rate_lbl.hide()

        if opts.get("show_runtime", False):
            self._runtime_lbl.setText(_fmt_time(runtime))
            self._runtime_lbl.show()
        else:
            self._runtime_lbl.hide()

        self.adjustSize()

    # ── move mode ─────────────────────────────────────────────────────────────

    def enter_move_mode(self):
        """Remove click-through so the window can receive drag events."""
        self._move_mode = True
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._lbl.setText("Drag to move")
        self._lbl.setStyleSheet(
            "color:#60a5fa; font-size:12px; font-weight:700;"
            " font-family:'Segoe UI',sans-serif;"
        )
        self._dot.setStyleSheet("background:#60a5fa; border-radius:4px; border:none;")
        # Hide info rows during move so the card is compact
        for il in (self._loops_lbl, self._rate_lbl, self._runtime_lbl):
            il.hide()
        self.adjustSize()
        self.show()

    def exit_move_mode(self):
        """Re-enable click-through."""
        self._move_mode = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.show()

    # ── drag support ──────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if self._move_mode and ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._move_mode and self._drag_pos is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        self._drag_pos = None
        ev.accept()


# ── layout helper ──────────────────────────────────────────────────────────────
def _hrow(*widgets):
    """HBox: first widget left, rest right-aligned (stretch between first and rest)."""
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(10)
    for i, w in enumerate(widgets):
        h.addWidget(w)
        if i == 0:
            h.addStretch()
    return h

def _fmt_time(s):
    s = int(s); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── Plugin ─────────────────────────────────────────────────────────────────────

class Plugin(PluginBase):
    NAME        = "Magnet Bot"
    DESCRIPTION = "Automated fishing minigame bot for FiveM. Scans for colored targets, auto-clicks, and tracks session stats."
    ACCENT      = "#4ade80"
    TAGS        = ["FiveM", "Automation"]
    VERSION     = "1.2.3"

    def __init__(self):
        self._bot_state      = State.IDLE
        self._catches        = 0
        self._loops          = 0
        self._last_loop_time = 0.0
        self._last_loop_s    = None   # duration of the most recent completed loop
        self._hk_gen         = 0
        self._active_overlay = None   # prevents GC of fullscreen overlays
        self._capturing      = {}     # hotkey name → bool
        self._badges         = {}     # hotkey name → QPushButton
        self._ik_capturing   = False  # interaction key capture flag
        self._ik_badge       = None   # interaction key badge ref
        self._hk_listener      = None   # persistent pynput listener for setup hotkeys
        self._rebuild_swatches = None   # set by _build_settings; called when colors change externally
        _plugin_ref[0]         = self  # allows bridge signals to dispatch to us

        # Status overlay — created once, lives for the plugin's lifetime
        self._overlay = _StatusOverlay()
        _pos = _cfg.get("overlay_pos", default={"x": 20, "y": 60})
        self._overlay.move(_pos.get("x", 20), _pos.get("y", 60))
        # Don't show on init — _update_overlay will handle visibility
        # Periodic timer keeps the overlay in sync (handles paused state,
        # plugin-active gating, and live info rows without needing bridge events)
        self._ov_timer = QTimer()
        self._ov_timer.timeout.connect(self._update_overlay)
        self._ov_timer.start(500)

        def _on_state(s):
            self._bot_state = s
            _bridge.update.emit()

        def _on_stat(k, v):
            if k == "catches":
                self._catches = v
            elif k == "loops":
                now = time.time()
                if self._loops > 0 and self._last_loop_time > 0:
                    self._last_loop_s = now - self._last_loop_time
                self._last_loop_time = now
                self._loops = v
            _bridge.update.emit()

        self._engine = BotEngine(
            _cfg,
            on_state_change=_on_state,
            on_stat_update=_on_stat,
            on_cooldown_tick=lambda e, t, l: _bridge.cooldown.emit(e, t, l),
            on_cycle_outcome=lambda o: _adaptive.record_cycle(o),
            on_stop=lambda loops, rt: self._on_bot_stop(loops, rt),
        )

    # ── hub card status ───────────────────────────────────────────────────────

    def status(self):
        if self._engine.is_running:
            if self._engine.is_paused or not is_fivem_focused():
                return "PAUSED", AMBER
            label, _ = _STATE_TEXT.get(self._bot_state, ("RUNNING", GREEN))
            return label.split("…")[0].split("!")[0].strip(), GREEN
        return "STOPPED", T3

    # ── build_page ────────────────────────────────────────────────────────────

    def build_page(self, nav_back):
        container, stack = make_tabs(["Dashboard", "Settings", "Stats", "Hotkeys"])
        stack.addWidget(self._build_dashboard())
        stack.addWidget(self._build_settings())
        stack.addWidget(self._build_stats())
        stack.addWidget(self._build_hotkeys())
        self._start_poll()
        self._start_hk_listener()
        return container

    # ══════════════════════════════════════════════════════════════════════════
    # Dashboard tab
    # ══════════════════════════════════════════════════════════════════════════

    def _build_dashboard(self):
        inner = QWidget()
        root  = QVBoxLayout(inner)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── state indicator ───────────────────────────────────────────────────
        state_lbl = lbl("STOPPED", sz=22, col=T3, bold=True,
                        align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(state_lbl)
        root.addSpacing(6)

        # ── cooldown bar (hidden when idle) ───────────────────────────────────
        cd_bar = QProgressBar()
        cd_bar.setRange(0, 1000)
        cd_bar.setTextVisible(False)
        cd_bar.setFixedHeight(5)
        cd_bar.setStyleSheet(
            "QProgressBar { background:#1a1a1a; border:none; border-radius:2px; }"
            "QProgressBar::chunk { background:#fbbf24; border-radius:2px; }"
        )
        cd_bar.hide()
        root.addWidget(cd_bar)

        cd_lbl = lbl("", sz=10, col=AMBER, align=Qt.AlignmentFlag.AlignCenter)
        cd_lbl.hide()
        root.addWidget(cd_lbl)
        root.addSpacing(6)

        root.addWidget(sep())
        root.addSpacing(12)

        # ── live stats ────────────────────────────────────────────────────────
        def stat_pair(caption):
            lay = QVBoxLayout(); lay.setSpacing(2)
            cap_w = lbl(caption, sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter)
            val_w = lbl("—", sz=18, col=T1, bold=True,
                        align=Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(cap_w)
            lay.addWidget(val_w)
            return lay, val_w

        sr = QHBoxLayout(); sr.setSpacing(0); sr.addStretch()
        lay, loops_lbl    = stat_pair("Loops");       sr.addLayout(lay); sr.addSpacing(22)
        lay, rate_lbl     = stat_pair("Loops / hr");  sr.addLayout(lay); sr.addSpacing(22)
        lay, runtime_lbl  = stat_pair("Runtime");     sr.addLayout(lay); sr.addSpacing(22)
        lay, sessions_lbl = stat_pair("Sessions");    sr.addLayout(lay); sr.addSpacing(22)
        lay, lastloop_lbl = stat_pair("Last Loop");   sr.addLayout(lay)
        sr.addStretch()
        root.addLayout(sr)
        root.addSpacing(10)

        # ── session goal progress ─────────────────────────────────────────────
        goal_frame = QWidget()
        gf_lay = QVBoxLayout(goal_frame)
        gf_lay.setContentsMargins(0, 4, 0, 2)
        gf_lay.setSpacing(3)
        goal_txt = lbl("", sz=10, col=T2, align=Qt.AlignmentFlag.AlignCenter)
        gf_lay.addWidget(goal_txt)
        goal_bar = QProgressBar()
        goal_bar.setRange(0, 1000)
        goal_bar.setTextVisible(False)
        goal_bar.setFixedHeight(5)
        goal_bar.setStyleSheet(
            "QProgressBar { background:#1a1a1a; border:none; border-radius:2px; }"
            "QProgressBar::chunk { background:#4ade80; border-radius:2px; }"
        )
        gf_lay.addWidget(goal_bar)
        goal_frame.hide()
        root.addWidget(goal_frame)

        # ── auto-stop countdown ───────────────────────────────────────────────
        as_lbl = lbl("", sz=10, col=AMBER, align=Qt.AlignmentFlag.AlignCenter)
        as_lbl.hide()
        root.addWidget(as_lbl)
        root.addSpacing(8)

        root.addWidget(sep())
        root.addSpacing(6)

        # ── config summary ────────────────────────────────────────────────────
        cfg_lbl = lbl("", sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(cfg_lbl)
        root.addSpacing(4)

        # ── last adaptive adjustment ──────────────────────────────────────────
        adapt_lbl = lbl("", sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(adapt_lbl)
        root.addSpacing(8)

        root.addWidget(sep())
        root.addSpacing(8)

        # ── hotkey quick-reference strip ──────────────────────────────────────
        _HK_REF = [
            ("toggle_bot",          "Toggle"),
            ("pause_bot",           "Pause"),
            ("select_wait_point",   "Wait Pt"),
            ("select_catch_region", "Region"),
            ("open_color_picker",   "Colors"),
            ("emergency_stop",      "Stop"),
        ]
        hk_chips: dict = {}
        hk_strip = QHBoxLayout(); hk_strip.setSpacing(0); hk_strip.addStretch()
        for hk_name, abbr in _HK_REF:
            k, _ = _cfg.get_hotkey(hk_name)
            col_lay = QVBoxLayout(); col_lay.setSpacing(3)
            col_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_lay.addWidget(lbl(abbr, sz=9, col=T3,
                                  align=Qt.AlignmentFlag.AlignCenter))
            chip = kbd(k or "—")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hk_chips[hk_name] = chip
            col_lay.addWidget(chip)
            hk_strip.addLayout(col_lay)
            hk_strip.addSpacing(18)
        hk_strip.addStretch()
        root.addLayout(hk_strip)
        root.addSpacing(10)

        # ── colors info ───────────────────────────────────────────────────────
        colors_info = lbl("", sz=11, col=T2)
        cr_row = QHBoxLayout()
        cr_row.addWidget(lbl("Target Colors:", sz=11, col=T3))
        cr_row.addSpacing(8)
        cr_row.addWidget(colors_info)
        cr_row.addStretch()
        root.addLayout(cr_row)
        root.addStretch()

        # ── refresh callbacks ─────────────────────────────────────────────────

        def _colors_text():
            n = len(_cfg.get("target_colors", default=[]))
            return (f"{n} color{'s' if n != 1 else ''} configured"
                    if n else "No colors set — configure in Settings")

        def refresh():
            running        = self._engine.is_running
            manual_paused  = self._engine.is_paused
            fivem_ok       = is_fivem_focused()
            auto_pause_on  = _cfg.get("auto_pause_unfocused", default=True)
            unfocus_paused = auto_pause_on and not fivem_ok
            any_paused     = manual_paused or unfocus_paused

            if running and any_paused:
                if manual_paused:
                    pk, _ = _cfg.get_hotkey("pause_bot")
                    state_lbl.setText(f"PAUSED — press {pk.upper()} to resume")
                else:
                    state_lbl.setText("PAUSED — focus FiveM")
                state_lbl.setStyleSheet(
                    f"color:{AMBER}; font-size:22px; font-weight:700; background:transparent;")
            elif running:
                text, color = _STATE_TEXT.get(self._bot_state, ("RUNNING", GREEN))
                state_lbl.setText(text)
                state_lbl.setStyleSheet(
                    f"color:{color}; font-size:22px; font-weight:700; background:transparent;")
                if self._bot_state not in (State.WAIT_START, State.WAIT_END):
                    cd_bar.hide(); cd_lbl.hide()
            else:
                hk, _ = _cfg.get_hotkey("toggle_bot")
                state_lbl.setText(f"STOPPED — press {hk.upper()}")
                state_lbl.setStyleSheet(
                    f"color:{T3}; font-size:22px; font-weight:700; background:transparent;")
                cd_bar.hide(); cd_lbl.hide()

            loops_lbl.setText(str(self._loops))
            rt   = self._engine.get_session_runtime() if running else 0.0
            rate = self._engine.get_loop_rate()        if running else 0.0
            runtime_lbl.setText(_fmt_time(rt))
            rate_lbl.setText(f"{rate:.1f}")
            sessions_lbl.setText(str(_cfg.get("stats", "lifetime_sessions", default=0)))
            colors_info.setText(_colors_text())

            # Last Loop
            lastloop_lbl.setText(
                f"{self._last_loop_s:.1f}s" if self._last_loop_s is not None else "—"
            )

            # Session Goal progress
            goal = _cfg.get("session_goal", default=0)
            if goal > 0:
                done      = self._loops >= goal
                pct       = min(int(self._loops / goal * 1000), 1000)
                chunk_col = "#fbbf24" if done else "#4ade80"
                goal_bar.setValue(pct)
                goal_txt.setText(
                    f"Goal: {self._loops} / {goal} loops" + ("  ✓" if done else "")
                )
                goal_bar.setStyleSheet(
                    "QProgressBar{background:#1a1a1a;border:none;border-radius:2px;}"
                    f"QProgressBar::chunk{{background:{chunk_col};border-radius:2px;}}"
                )
                goal_frame.show()
            else:
                goal_frame.hide()

            # Auto-stop countdown
            as_loops = _cfg.get("auto_stop", "loops",   default=0)
            as_mins  = _cfg.get("auto_stop", "minutes", default=0)
            if as_loops > 0 or as_mins > 0:
                parts = []
                if as_loops > 0:
                    rem = max(0, as_loops - self._loops)
                    parts.append(f"{rem} loop{'s' if rem != 1 else ''} left")
                if as_mins > 0 and running:
                    rem_s = max(0.0, as_mins * 60 - rt)
                    m, s  = divmod(int(rem_s), 60)
                    parts.append(f"{m}:{s:02d} left")
                elif as_mins > 0:
                    parts.append(f"{as_mins} min limit")
                as_lbl.setText("Auto-stop:  " + "  ·  ".join(parts))
                as_lbl.show()
            else:
                as_lbl.hide()

            # Config summary
            preset_key  = _cfg.get("level_preset", default="1-4")
            preset_name = LEVEL_PRESETS.get(preset_key, LEVEL_PRESETS["1-4"])["label"]
            icon_n      = _cfg.get_icon_count()
            sw          = _cfg.get("timing", "start_wait",       default=1.75)
            aw          = _cfg.get("timing", "after_click_wait", default=2.80)
            cfg_lbl.setText(
                f"{preset_name}  ·  {icon_n} icon{'s' if icon_n != 1 else ''}"
                f"  ·  Cast {sw:.2f}s  ·  After {aw:.2f}s"
            )

            # Last adaptive adjustment
            alog = _adaptive.get_log()
            adapt_lbl.setText(
                f"{alog[0]['time']}  {alog[0]['title']}" if alog else ""
            )

            for hk_name, chip in hk_chips.items():
                k, _ = _cfg.get_hotkey(hk_name)
                chip.setText((k or "—").upper())

        def _on_cd(elapsed, total, label):
            pct = int(min(elapsed / max(total, 0.01), 1.0) * 1000)
            cd_bar.setValue(pct)
            cd_lbl.setText(label)
            cd_bar.show(); cd_lbl.show()

        # Debounce bridge updates
        _debounce = QTimer(inner)
        _debounce.setSingleShot(True)
        _debounce.setInterval(80)
        _debounce.timeout.connect(refresh)
        _bridge.update.connect(lambda: _debounce.start())

        _bridge.cooldown.connect(_on_cd)

        # Main poll — live counters
        timer = QTimer(inner)
        timer.timeout.connect(refresh)
        timer.start(500)

        refresh()
        return inner

    # ══════════════════════════════════════════════════════════════════════════
    # Settings tab
    # ══════════════════════════════════════════════════════════════════════════

    def _build_settings(self):
        inner = QWidget()
        root  = QVBoxLayout(inner)
        root.setContentsMargins(0, 8, 8, 16)
        root.setSpacing(0)

        # ── Level Preset ───────────────────────────────────────────────────────
        root.addWidget(section_header("Level Preset"))
        root.addSpacing(8)

        pr = QHBoxLayout(); pr.setSpacing(6)
        pg = QButtonGroup(inner); pg.setExclusive(True)
        cur = _cfg.get("level_preset", default="1-4")
        _preset_btns: dict = {}   # key → button; wired to timing steppers below
        for key, meta in LEVEL_PRESETS.items():
            b = QPushButton(meta["label"])
            b.setObjectName("preset")
            b.setCheckable(True)
            b.setChecked(key == cur)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            pg.addButton(b)
            pr.addWidget(b)
            _preset_btns[key] = b
        pr.addStretch()
        root.addLayout(pr)
        root.addSpacing(4)
        root.addWidget(lbl(
            "Selecting a preset auto-fills Cast Wait and Click Wait times.",
            sz=10, col=T3,
        ))
        root.addSpacing(8)

        ov = NumericStepper(
            _cfg.get("icon_count_override", default=0),
            step=1, min_val=0, max_val=10, fmt="{:.0f}",
        )
        ov.changed.connect(lambda v: _cfg.set("icon_count_override", int(v)))
        root.addLayout(_hrow(lbl("Icon Count Override  (0 = auto)", sz=12, col=T2), ov))
        root.addSpacing(20)

        # ── Wait Point ─────────────────────────────────────────────────────────
        root.addWidget(section_header("Wait Point"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "The point where the game places your cursor before each cast.",
            sz=10, col=T3))
        root.addSpacing(8)

        wp = _cfg.get("wait_point", default={"x": 960, "y": 540})
        wp_lbl = lbl(f"({wp['x']}, {wp['y']})", sz=12, col=T1, bold=True)
        wp_btn = QPushButton("Set Point"); wp_btn.setObjectName("action")
        wp_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _set_wp():
            def on_select(pos):
                self._active_overlay = None
                if pos:
                    x, y = pos
                    _cfg.set("wait_point", {"x": x, "y": y})
                    wp_lbl.setText(f"({x}, {y})")
            self._active_overlay = PointSelectorOverlay(None, on_select)

        wp_btn.clicked.connect(_set_wp)
        root.addLayout(_hrow(wp_lbl, wp_btn))
        root.addSpacing(20)

        # ── Catch Region ───────────────────────────────────────────────────────
        root.addWidget(section_header("Catch Region"))
        root.addSpacing(6)
        root.addWidget(lbl("Drag to select the rectangle where fish icons appear.",
                           sz=10, col=T3))
        root.addSpacing(8)

        cr = _cfg.get("catch_region", default={"x": 660, "y": 380, "width": 600, "height": 340})
        cr_lbl = lbl(f"({cr['x']}, {cr['y']})  {cr['width']}×{cr['height']}", sz=12, col=T1, bold=True)
        cr_btn = QPushButton("Select Region"); cr_btn.setObjectName("action")
        cr_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _set_cr():
            def on_select(reg):
                self._active_overlay = None
                if reg:
                    _cfg.set("catch_region", reg)
                    cr_lbl.setText(f"({reg['x']}, {reg['y']})  {reg['width']}×{reg['height']}")
            self._active_overlay = RegionSelectorOverlay(None, on_select)

        cr_btn.clicked.connect(_set_cr)
        root.addLayout(_hrow(cr_lbl, cr_btn))
        root.addSpacing(20)

        # ── Target Colors ──────────────────────────────────────────────────────
        root.addWidget(section_header("Target Colors"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "Colors the bot clicks on. Pick from the game using the overlay.",
            sz=10, col=T3))
        root.addSpacing(2)
        root.addWidget(lbl(
            "If icons appear as a glowing box outline instead of a filled icon, "
            "pick the outline color as an extra entry and lower Min Cluster Pixels to 8–12.",
            sz=10, col=T3, wrap=True))
        root.addSpacing(8)

        swatches_w = QWidget(); swatches_w.setStyleSheet("background:transparent;")
        swatches_v = QVBoxLayout(swatches_w)
        swatches_v.setContentsMargins(0, 0, 0, 0); swatches_v.setSpacing(4)

        def _rebuild_swatches():
            while swatches_v.count():
                item = swatches_v.takeAt(0)
                if item.widget(): item.widget().deleteLater()
            colors = _cfg.get("target_colors", default=[])
            for idx, c in enumerate(colors):
                sw = ColorSwatch(c["r"], c["g"], c["b"])
                def _del(i=idx):
                    cl = list(_cfg.get("target_colors", default=[]))
                    if 0 <= i < len(cl):
                        cl.pop(i)
                        _cfg.set("target_colors", cl)
                    _rebuild_swatches()
                sw.deleted.connect(_del)
                swatches_v.addWidget(sw)
            if not colors:
                swatches_v.addWidget(lbl("No colors set — click Add Color", sz=11, col=T3))

        _rebuild_swatches()
        self._rebuild_swatches = _rebuild_swatches   # expose to _do_open_color_picker
        root.addWidget(swatches_w)
        root.addSpacing(8)

        add_col_btn = QPushButton("+ Add Color"); add_col_btn.setObjectName("action")
        add_col_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_col_btn.setFixedWidth(110)

        def _add_color():
            def on_pick(r, g, b):
                self._active_overlay = None
                if r is not None:
                    cl = list(_cfg.get("target_colors", default=[]))
                    cl.append({"r": r, "g": g, "b": b})
                    _cfg.set("target_colors", cl)
                    _rebuild_swatches()
            self._active_overlay = ColorPickerOverlay(None, on_pick)

        add_col_btn.clicked.connect(_add_color)
        root.addWidget(add_col_btn)
        root.addSpacing(20)

        # ── Color Tolerance ────────────────────────────────────────────────────
        root.addWidget(section_header("Color Tolerance"))
        root.addSpacing(8)

        tol = NumericStepper(
            _cfg.get("color_tolerance", default=10),
            step=1, min_val=1, max_val=100, fmt="{:.0f}",
        )
        tol.changed.connect(lambda v: _cfg.set("color_tolerance", int(v)))
        root.addLayout(_hrow(lbl("Tolerance  (recommended: 10)", sz=12, col=T2), tol))
        root.addSpacing(20)

        # ── Timing ────────────────────────────────────────────────────────────
        root.addWidget(section_header("Timing"))
        root.addSpacing(8)

        td = _cfg.get("timing", default={})
        timing_rows = [
            ("Cast Wait (s)",           "start_wait",          td.get("start_wait",          1.75), 0.05, 0.1,  30.0),
            ("After Click Wait (s)",    "after_click_wait",    td.get("after_click_wait",    2.80), 0.05, 0.1,  30.0),
            ("Between Cycle Delay (s)", "between_cycle_delay", td.get("between_cycle_delay", 0.50), 0.05, 0.0,   5.0),
            ("Scan Interval (s)",       "scan_interval",       td.get("scan_interval",       0.05), 0.01, 0.01,  1.0),
            ("Click Delay (s)",         "click_delay",         td.get("click_delay",         0.12), 0.01, 0.01,  2.0),
            ("Min Cluster Pixels",      "min_cluster_pixels",  td.get("min_cluster_pixels",  15),   1,    1,    500),
        ]
        _preset_steppers: dict = {}   # tkey → NumericStepper; used by preset buttons
        for tname, tkey, tval, tstep, tmin, tmax in timing_rows:
            is_int = tkey == "min_cluster_pixels"
            fmt    = "{:.0f}" if is_int else "{:.2f}"
            st = NumericStepper(tval, step=tstep, min_val=tmin, max_val=tmax, fmt=fmt)
            def _t_changed(v, k=tkey, as_int=is_int):
                cur_td = dict(_cfg.get("timing", default={}))
                cur_td[k] = int(v) if as_int else v
                _cfg.set("timing", cur_td)
            st.changed.connect(_t_changed)
            root.addLayout(_hrow(lbl(tname, sz=12, col=T2), st))
            root.addSpacing(6)
            if tkey in ("start_wait", "after_click_wait"):
                _preset_steppers[tkey] = st

        # Wire preset buttons → auto-fill Cast Wait + After Click Wait
        for key, btn in _preset_btns.items():
            def _on_preset(checked, k=key):
                if not checked:
                    return
                _cfg.set("level_preset", k)
                meta = LEVEL_PRESETS[k]
                cur_td = dict(_cfg.get("timing", default={}))
                for tkey in ("start_wait", "after_click_wait"):
                    if tkey in meta and tkey in _preset_steppers:
                        cur_td[tkey] = meta[tkey]
                        _preset_steppers[tkey].set_value(meta[tkey])
                _cfg.set("timing", cur_td)
            btn.toggled.connect(_on_preset)

        root.addSpacing(14)

        # ── Options ───────────────────────────────────────────────────────────
        root.addWidget(section_header("Options"))
        root.addSpacing(8)

        # Interaction key (rebindable)
        ik_cur = _cfg.get("interaction_key", default="e")
        ik_badge = QPushButton(ik_cur.upper())
        ik_badge.setObjectName("badge")
        ik_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        ik_badge.setToolTip("Click to rebind")
        ik_badge.setProperty("capturing", False)
        self._ik_badge = ik_badge
        ik_badge.clicked.connect(self._start_ik_capture)
        root.addLayout(_hrow(lbl("Interaction Key", sz=12, col=T2), ik_badge))
        root.addSpacing(10)

        # Jitter pixels
        jp = NumericStepper(
            _cfg.get("jitter_px", default=3),
            step=1, min_val=0, max_val=20, fmt="{:.0f}",
        )
        jp.changed.connect(lambda v: _cfg.set("jitter_px", int(v)))
        root.addLayout(_hrow(lbl("Jitter Pixels", sz=12, col=T2), jp))
        root.addSpacing(10)

        for opt_label, opt_key, opt_default in [
            ("Anti-Jitter",               "anti_jitter",          True),
            ("Auto-pause when unfocused", "auto_pause_unfocused", True),
        ]:
            sw = ToggleSwitch(_cfg.get(opt_key, default=opt_default))
            sw.toggled.connect(lambda v, k=opt_key: _cfg.set(k, v))
            root.addLayout(_hrow(lbl(opt_label, sz=12, col=T2), sw))
            root.addSpacing(8)

        root.addSpacing(12)
        root.addWidget(sep())
        root.addSpacing(16)

        # ── Status Overlay ────────────────────────────────────────────────────
        root.addWidget(section_header("Status Overlay"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "Shows bot state on top of all windows — click-through, "
            "never blocks FiveM input.",
            sz=11, col=T3, wrap=True,
        ))
        root.addSpacing(12)

        ov_sw   = ToggleSwitch(_cfg.get("overlay_enabled", default=False))
        move_btn = QPushButton("Move")
        move_btn.setObjectName("action")
        move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        move_btn.setFixedWidth(60)

        _moving = [False]

        def _on_ov_toggle(v):
            _cfg.set("overlay_enabled", v)
            ov = self._overlay
            if v:
                self._update_overlay()
                ov.show()
            elif not _moving[0]:
                ov.hide()

        def _on_move():
            ov = self._overlay
            if not _moving[0]:
                _moving[0] = True
                ov.show()
                ov.enter_move_mode()
                move_btn.setText("Done")
            else:
                _moving[0] = False
                p = ov.pos()
                _cfg.set("overlay_pos", {"x": p.x(), "y": p.y()})
                ov.exit_move_mode()
                if _cfg.get("overlay_enabled", default=False):
                    self._update_overlay()
                else:
                    ov.hide()
                move_btn.setText("Move")

        ov_sw.toggled.connect(_on_ov_toggle)
        move_btn.clicked.connect(_on_move)

        ov_row = QHBoxLayout()
        ov_row.setContentsMargins(0, 0, 0, 0); ov_row.setSpacing(10)
        ov_row.addWidget(lbl("Show Overlay", sz=12, col=T2))
        ov_row.addStretch()
        ov_row.addWidget(move_btn)
        ov_row.addSpacing(4)
        ov_row.addWidget(ov_sw)
        root.addLayout(ov_row)
        root.addSpacing(12)

        # ── Overlay display options ────────────────────────────────────────────
        root.addWidget(lbl("Display on overlay", sz=10, col=T3))
        root.addSpacing(8)

        _ov_opts = _cfg.get("overlay_opts", default={})
        for opt_key, opt_label in [
            ("show_loops",   "Loop count"),
            ("show_rate",    "Loops / hr"),
            ("show_runtime", "Runtime"),
        ]:
            sw = ToggleSwitch(_ov_opts.get(opt_key, False))
            def _on_opt(v, k=opt_key):
                cur = dict(_cfg.get("overlay_opts", default={}))
                cur[k] = v
                _cfg.set("overlay_opts", cur)
                self._update_overlay()
            sw.toggled.connect(_on_opt)
            opt_row = QHBoxLayout()
            opt_row.setContentsMargins(12, 0, 0, 0)
            opt_row.addWidget(lbl(opt_label, sz=12, col=T2))
            opt_row.addStretch()
            opt_row.addWidget(sw)
            root.addLayout(opt_row)
            root.addSpacing(6)

        root.addSpacing(12)
        root.addWidget(sep())
        root.addSpacing(16)

        # ── Session Goal ──────────────────────────────────────────────────────
        root.addWidget(section_header("Session Goal"))
        root.addSpacing(8)

        sg_st = NumericStepper(
            _cfg.get("session_goal", default=0),
            step=10, min_val=0, max_val=9999, fmt="{:.0f}",
        )
        sg_st.changed.connect(lambda v: _cfg.set("session_goal", int(v)))
        root.addLayout(_hrow(lbl("Loops target  (0 = off)", sz=12, col=T2), sg_st))
        root.addSpacing(12)
        root.addWidget(sep())
        root.addSpacing(16)

        # ── Discord ───────────────────────────────────────────────────────────
        root.addWidget(section_header("Discord"))
        root.addSpacing(8)

        root.addWidget(lbl("Webhook URL", sz=12, col=T2))
        root.addSpacing(4)
        wh_edit = QLineEdit()
        wh_edit.setPlaceholderText("https://discord.com/api/webhooks/…")
        wh_edit.setText(_cfg.get("discord_webhook_url", default=""))
        wh_edit.editingFinished.connect(
            lambda: _cfg.set("discord_webhook_url", wh_edit.text().strip())
        )
        root.addWidget(wh_edit)
        root.addSpacing(12)

        root.addWidget(lbl("Milestones  (loop counts, comma-separated)", sz=12, col=T2))
        root.addSpacing(4)
        ms_edit = QLineEdit()
        ms_edit.setPlaceholderText("e.g. 10, 25, 50, 100, 250")
        _cur_ms = _cfg.get("discord_milestones", default=[10, 25, 50, 100, 250])
        ms_edit.setText(", ".join(str(x) for x in _cur_ms))

        def _save_milestones():
            raw = ms_edit.text()
            try:
                vals = sorted(set(
                    int(x.strip()) for x in raw.split(",")
                    if x.strip().isdigit() and int(x.strip()) > 0
                ))
                if vals:
                    _cfg.set("discord_milestones", vals)
                    ms_edit.setText(", ".join(str(x) for x in vals))
            except Exception:
                pass

        ms_edit.editingFinished.connect(_save_milestones)
        root.addWidget(ms_edit)
        root.addSpacing(12)

        ns_sw = ToggleSwitch(_cfg.get("discord_notify_stop", default=False))
        ns_sw.toggled.connect(lambda v: _cfg.set("discord_notify_stop", v))
        root.addLayout(_hrow(lbl("Notify on stop", sz=12, col=T2), ns_sw))
        root.addSpacing(4)
        root.addWidget(lbl(
            "Sends a session summary (loops, runtime, loops/hr) to your webhook "
            "each time the bot stops.",
            sz=10, col=T3, wrap=True,
        ))

        root.addStretch()
        return scrollable(inner)

    # ══════════════════════════════════════════════════════════════════════════
    # Stats tab
    # ══════════════════════════════════════════════════════════════════════════

    def _build_stats(self):
        inner = QWidget()
        root  = QVBoxLayout(inner)
        root.setContentsMargins(0, 8, 0, 8)
        root.setSpacing(0)

        # ── Lifetime ──────────────────────────────────────────────────────────
        root.addWidget(section_header("Lifetime Stats"))
        root.addSpacing(12)

        def stat_col(caption):
            lay = QVBoxLayout(); lay.setSpacing(2)
            cap_w = lbl(caption, sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter)
            val_w = lbl("—", sz=18, col=T1, bold=True, align=Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(cap_w); lay.addWidget(val_w)
            return lay, val_w

        sr = QHBoxLayout(); sr.setSpacing(0); sr.addStretch()
        lay, lc_lbl  = stat_col("Loops");    sr.addLayout(lay); sr.addSpacing(36)
        lay, ls_lbl  = stat_col("Sessions"); sr.addLayout(lay); sr.addSpacing(36)
        lay, lr_lbl  = stat_col("Runtime");  sr.addLayout(lay); sr.addSpacing(36)
        lay, lavg_lbl = stat_col("Avg / hr"); sr.addLayout(lay)
        sr.addStretch()
        root.addLayout(sr)
        root.addSpacing(10)

        rst_btn = QPushButton("Reset Lifetime Stats")
        rst_btn.setObjectName("action")
        rst_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rst_btn.setFixedWidth(160)
        rst_row = QHBoxLayout(); rst_row.addStretch(); rst_row.addWidget(rst_btn)
        root.addLayout(rst_row)
        root.addSpacing(16)
        root.addWidget(sep())
        root.addSpacing(16)

        # ── Adaptive Engine ────────────────────────────────────────────────────
        root.addWidget(section_header("Adaptive Engine"))
        root.addSpacing(12)

        def adapt_col(caption):
            lay = QVBoxLayout(); lay.setSpacing(2)
            cap_w = lbl(caption, sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter)
            val_w = lbl("—", sz=16, col=T1, bold=True, align=Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(cap_w); lay.addWidget(val_w)
            return lay, val_w

        ar = QHBoxLayout(); ar.setSpacing(0); ar.addStretch()
        lay, sr_lbl   = adapt_col("Success Rate"); ar.addLayout(lay); ar.addSpacing(24)
        lay, cyc_lbl  = adapt_col("Cycles");       ar.addLayout(lay); ar.addSpacing(24)
        lay, det_lbl  = adapt_col("Avg Detected"); ar.addLayout(lay); ar.addSpacing(24)
        lay, ttfd_lbl = adapt_col("Avg TTFD (s)"); ar.addLayout(lay); ar.addSpacing(24)
        lay, rec_lbl  = adapt_col("Recovery %");   ar.addLayout(lay)
        ar.addStretch()
        root.addLayout(ar)
        root.addSpacing(16)
        root.addWidget(sep())
        root.addSpacing(12)

        # ── Adjustment Log ────────────────────────────────────────────────────
        adj_log = QTextEdit()
        adj_log.setReadOnly(True)
        adj_log.setFixedHeight(90)
        adj_log.setStyleSheet(
            "QTextEdit { background:#0f0f0f; border:1px solid #1e1e1e;"
            " border-radius:6px; font-size:10px; color:#6b7280; padding:4px; }"
        )

        copy_btn = QPushButton("Copy")
        copy_btn.setObjectName("action")
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        def _copy_log():
            QApplication.clipboard().setText(adj_log.toPlainText())
            copy_btn.setText("Copied!")
            QTimer.singleShot(1800, lambda: copy_btn.setText("Copy"))
        copy_btn.clicked.connect(_copy_log)

        log_hdr = QHBoxLayout()
        log_hdr.setContentsMargins(0, 0, 0, 0); log_hdr.setSpacing(0)
        log_hdr.addWidget(section_header("Adjustment Log"))
        log_hdr.addStretch()
        log_hdr.addWidget(copy_btn)
        root.addLayout(log_hdr)
        root.addSpacing(8)
        root.addWidget(adj_log)

        root.addSpacing(16)
        root.addWidget(sep())
        root.addSpacing(12)

        # ── Session History ────────────────────────────────────────────────────
        _COL_LOOPS   = 52
        _COL_RUNTIME = 56

        def _hist_hrow(date_w, loops_w, rt_w, is_header=False):
            row = QWidget(); row.setStyleSheet("background:transparent;")
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 1 if is_header else 2, 4, 1 if is_header else 2)
            h.setSpacing(0)
            h.addWidget(date_w)
            h.addStretch()
            loops_w.setFixedWidth(_COL_LOOPS)
            loops_w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            h.addWidget(loops_w)
            h.addSpacing(16)
            rt_w.setFixedWidth(_COL_RUNTIME)
            rt_w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            h.addWidget(rt_w)
            return row

        clr_btn = QPushButton("Clear")
        clr_btn.setObjectName("action")
        clr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clr_btn.setFixedWidth(56)

        sh_hdr = QHBoxLayout()
        sh_hdr.setContentsMargins(0, 0, 0, 0)
        sh_hdr.addWidget(section_header("Session History"))
        sh_hdr.addStretch()
        sh_hdr.addWidget(clr_btn)
        root.addLayout(sh_hdr)
        root.addSpacing(6)

        root.addWidget(_hist_hrow(
            lbl("Date",    sz=10, col=T3),
            lbl("Loops",   sz=10, col=T3),
            lbl("Runtime", sz=10, col=T3),
            is_header=True,
        ))
        root.addSpacing(4)
        root.addWidget(sep())
        root.addSpacing(4)

        hist_w = QWidget(); hist_w.setStyleSheet("background:transparent;")
        hist_v = QVBoxLayout(hist_w)
        hist_v.setContentsMargins(0, 0, 0, 0); hist_v.setSpacing(1)
        root.addWidget(hist_w)
        root.addStretch()

        # ── refresh ────────────────────────────────────────────────────────────

        def _refresh_stats():
            ll_loops   = _cfg.get("stats", "lifetime_loops",      default=0)
            ll_runtime = _cfg.get("stats", "lifetime_runtime_s",  default=0)
            lc_lbl.setText(str(ll_loops))
            ls_lbl.setText(str(_cfg.get("stats", "lifetime_sessions", default=0)))
            lr_lbl.setText(_fmt_time(ll_runtime))
            avg_hr = (ll_loops / (ll_runtime / 3600)) if ll_runtime > 0 else 0.0
            lavg_lbl.setText(f"{avg_hr:.1f}")

            stats = _adaptive.get_stats()
            sr_lbl.setText(f"{stats['success_rate']:.0%}")
            cyc_lbl.setText(str(_adaptive.total_cycles))
            det_lbl.setText(str(stats["avg_detected"]))
            ttfd_lbl.setText(f"{stats['avg_ttfd']:.2f}")
            rec_lbl.setText(f"{stats['recovery_rate']:.0%}")

            adj_log.clear()
            for entry in reversed(_adaptive.get_log()):
                adj_log.append(f"[{entry['time']}] {entry['title']}: {entry['msg']}")
            sb = adj_log.verticalScrollBar(); sb.setValue(sb.maximum())

            # Session history — rebuild rows newest first
            while hist_v.count():
                item = hist_v.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            history = list(_cfg.get("stats", "session_history", default=[]))
            if not history:
                hist_v.addWidget(lbl("No sessions recorded yet.", sz=11, col=T3))
            else:
                for entry in reversed(history):
                    hist_v.addWidget(_hist_hrow(
                        lbl(entry.get("date", ""),                sz=11, col=T2),
                        lbl(str(entry.get("loops", 0)),           sz=11, col=T1, bold=True),
                        lbl(_fmt_time(entry.get("runtime_s", 0)), sz=11, col=T2),
                    ))

        def _reset_stats():
            _cfg.set("stats", "lifetime_catches",   0)
            _cfg.set("stats", "lifetime_loops",     0)
            _cfg.set("stats", "lifetime_sessions",  0)
            _cfg.set("stats", "lifetime_runtime_s", 0)
            _refresh_stats()

        def _clear_history():
            _cfg.set("stats", "session_history", [])
            _refresh_stats()

        rst_btn.clicked.connect(_reset_stats)
        clr_btn.clicked.connect(_clear_history)

        timer = QTimer(inner); timer.timeout.connect(_refresh_stats); timer.start(2000)
        _refresh_stats()
        return scrollable(inner)

    # ══════════════════════════════════════════════════════════════════════════
    # Hotkeys tab
    # ══════════════════════════════════════════════════════════════════════════

    def _build_hotkeys(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 4, 0, 8)
        root.setSpacing(0)

        HOTKEYS = [
            ("toggle_bot",          "Toggle Bot"),
            ("pause_bot",           "Pause / Resume"),
            ("select_wait_point",   "Select Wait Point"),
            ("select_catch_region", "Select Catch Region"),
            ("open_color_picker",   "Open Color Picker"),
            ("emergency_stop",      "Emergency Stop"),
        ]

        for name, label in HOTKEYS:
            key_str, enabled = _cfg.get_hotkey(name)
            root.addWidget(sep())
            root.addSpacing(10)

            row = QHBoxLayout(); row.setSpacing(10)
            row.addWidget(lbl(label, sz=12, col=T2))
            row.addStretch()

            badge = QPushButton(key_str.upper() or "—")
            badge.setObjectName("badge")
            badge.setCursor(Qt.CursorShape.PointingHandCursor)
            badge.setToolTip("Click to rebind")
            badge.setProperty("capturing", False)
            self._badges[name]    = badge
            self._capturing[name] = False
            badge.clicked.connect(lambda _, n=name: self._start_hk_capture(n))
            row.addWidget(badge)
            row.addSpacing(8)

            sw = ToggleSwitch(enabled)
            def _on_enabled(v, n=name):
                _cfg.set_hotkey(n, enabled=v)
                if n in ("toggle_bot", "pause_bot"):
                    self._start_poll()
                else:
                    self._start_hk_listener()
            sw.toggled.connect(_on_enabled)
            row.addWidget(sw)

            root.addLayout(row)
            root.addSpacing(10)

        root.addWidget(sep())
        root.addStretch()
        return page

    # ── hotkey poll (toggle + pause — win32api, works while FiveM focused) ──────

    def _start_poll(self):
        self._hk_gen += 1
        my_gen = self._hk_gen

        toggle_str, toggle_en = _cfg.get_hotkey("toggle_bot")
        toggle_vk = _vk(toggle_str) if toggle_en else 0

        pause_str, pause_en = _cfg.get_hotkey("pause_bot")
        pause_vk = _vk(pause_str) if pause_en else 0

        if not toggle_vk and not pause_vk:
            return

        def poll():
            last_toggle = False
            last_pause  = False
            time.sleep(0.06)
            while self._hk_gen == my_gen:
                try:
                    if toggle_vk:
                        down = bool(win32api.GetAsyncKeyState(toggle_vk) & 0x8000)
                        if down and not last_toggle and is_plugin_active("Magnet Bot"):
                            if self._engine.is_running:
                                self._engine.stop()
                            else:
                                self._engine.start()
                            _bridge.update.emit()
                        last_toggle = down

                    if pause_vk:
                        down = bool(win32api.GetAsyncKeyState(pause_vk) & 0x8000)
                        if down and not last_pause and is_plugin_active("Magnet Bot"):
                            if self._engine.is_running:
                                self._engine.toggle_pause()
                                _bridge.update.emit()
                        last_pause = down
                except Exception:
                    pass
                time.sleep(0.05)

        threading.Thread(target=poll, daemon=True).start()

    # ── setup hotkey listener (all non-toggle hotkeys) ────────────────────────

    def _start_hk_listener(self):
        """Persistent pynput Listener for the four setup hotkeys.
        Emits Qt signals (thread-safe) instead of QTimer.singleShot so the
        overlay actions always run on the main thread.
        Uses string-based key matching — robust across pynput versions.
        Restarts whenever a key is rebound or enabled/disabled.
        """
        # stop any previous listener
        if self._hk_listener is not None:
            try: self._hk_listener.stop()
            except Exception: pass
            self._hk_listener = None

        # Map "f4" / "e" etc. → the signal to emit
        str_to_sig: dict = {}
        for hk_id, sig in [
            ("emergency_stop",      _bridge.hk_emergency),
            ("select_wait_point",   _bridge.hk_wait_point),
            ("select_catch_region", _bridge.hk_catch_region),
            ("open_color_picker",   _bridge.hk_color_picker),
        ]:
            ks, enabled = _cfg.get_hotkey(hk_id)
            if enabled and ks:
                str_to_sig[ks.lower()] = sig

        if not str_to_sig:
            return   # all hotkeys disabled

        def _key_str(key) -> str:
            """Normalise a pynput key to a lowercase string like 'f4' or 'e'."""
            try:
                # Special keys (Key.f4, Key.space, …) have a .name attribute
                if hasattr(key, "name") and key.name:
                    return key.name.lower()
                # Character keys (KeyCode) have a .char attribute
                if hasattr(key, "char") and key.char:
                    return key.char.lower()
            except Exception:
                pass
            return ""

        def on_press(key):
            if not is_plugin_active("Magnet Bot"):
                return
            ks = _key_str(key)
            if not ks:
                return
            sig = str_to_sig.get(ks)
            if sig and not any(self._capturing.values()) and not self._ik_capturing:
                sig.emit()   # ← thread-safe: Qt queues to main thread

        self._hk_listener = Listener(on_press=on_press)
        self._hk_listener.daemon = True
        self._hk_listener.start()

    # ── setup hotkey actions ──────────────────────────────────────────────────

    def _do_emergency_stop(self):
        self._engine.stop()
        _bridge.log_msg.emit("⚠ Emergency stop triggered.")
        _bridge.update.emit()

    def _do_select_wait_point(self):
        def on_select(pos):
            self._active_overlay = None
            if pos:
                x, y = pos
                _cfg.set("wait_point", {"x": x, "y": y})
                _bridge.log_msg.emit(f"Wait point set: ({x}, {y})")
        self._active_overlay = PointSelectorOverlay(None, on_select)

    def _do_select_catch_region(self):
        def on_select(reg):
            self._active_overlay = None
            if reg:
                _cfg.set("catch_region", reg)
                _bridge.log_msg.emit(
                    f"Catch region set: ({reg['x']}, {reg['y']})  "
                    f"{reg['width']}×{reg['height']}"
                )
        self._active_overlay = RegionSelectorOverlay(None, on_select)

    def _do_open_color_picker(self):
        def on_pick(r, g, b):
            self._active_overlay = None
            if r is not None:
                cl = list(_cfg.get("target_colors", default=[]))
                cl.append({"r": r, "g": g, "b": b})
                _cfg.set("target_colors", cl)
                _bridge.log_msg.emit(f"Color added: RGB({r}, {g}, {b})")
                # Refresh the Settings tab swatches if the page is open
                if self._rebuild_swatches:
                    self._rebuild_swatches()
        self._active_overlay = ColorPickerOverlay(None, on_pick)

    # ── Discord stop notification ─────────────────────────────────────────────

    def _on_bot_stop(self, loops: int, runtime: float):
        """
        Called by BotEngine when a session ends (via on_stop callback).
        Posts a session summary to the Discord webhook if the toggle is on.
        Runs the HTTP request in a daemon thread so it never blocks the UI.
        """
        url = _cfg.get("discord_webhook_url", default="")
        if not url or not _cfg.get("discord_notify_stop", default=False):
            return

        def _post():
            try:
                h, rem = divmod(int(runtime), 3600)
                m, s   = divmod(rem, 60)
                rt_str = f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"
                rate   = (loops / (runtime / 3600)) if runtime > 0 else 0.0
                content = (
                    f"🎣 **Magnet Bot** — Session ended\n"
                    f"> **{loops}** loops  ·  {rt_str} runtime  ·  {rate:.1f} loops/hr"
                )
                data = _json.dumps({"content": content}).encode()
                req  = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=8)
            except Exception:
                pass

        threading.Thread(target=_post, daemon=True).start()

    # ── interaction key capture ───────────────────────────────────────────────

    def _start_ik_capture(self):
        if self._ik_capturing:
            return
        self._ik_capturing = True
        badge = self._ik_badge
        if badge:
            badge.setText("…")
            badge.setProperty("capturing", True)
            badge.style().unpolish(badge); badge.style().polish(badge)

        def listen():
            def on_press(key):
                try:
                    k = key.char if hasattr(key, "char") and key.char else key.name
                except Exception:
                    k = str(key).replace("Key.", "")
                k = k.lower()
                _cfg.set("interaction_key", k)

                def apply():
                    if badge:
                        badge.setText(k.upper())
                        badge.setProperty("capturing", False)
                        badge.style().unpolish(badge); badge.style().polish(badge)
                    self._ik_capturing = False

                QTimer.singleShot(0, apply)
                return False

            with Listener(on_press=on_press) as lst:
                lst.join()

        threading.Thread(target=listen, daemon=True).start()

    # ── hotkey rebind capture ─────────────────────────────────────────────────

    def _start_hk_capture(self, name: str):
        if self._capturing.get(name):
            return
        self._capturing[name] = True

        # Stop the persistent pynput listener before capture — on Windows,
        # two simultaneous WH_KEYBOARD_LL hooks from the same process compete
        # and the capture listener may never see the key press.
        if self._hk_listener is not None:
            try: self._hk_listener.stop()
            except Exception: pass
            self._hk_listener = None

        badge = self._badges.get(name)
        if badge:
            badge.setText("…")
            badge.setProperty("capturing", True)
            badge.style().unpolish(badge); badge.style().polish(badge)

        def listen():
            def on_press(key):
                try:
                    k = key.char if hasattr(key, "char") and key.char else key.name
                except Exception:
                    k = str(key).replace("Key.", "")
                k = k.lower()
                _cfg.set_hotkey(name, key=k)

                def apply():
                    if badge:
                        badge.setText(k.upper())
                        badge.setProperty("capturing", False)
                        badge.style().unpolish(badge); badge.style().polish(badge)
                    self._capturing[name] = False
                    # Always restart the persistent listener (was stopped before capture)
                    # and the poll for the two win32api-based keys
                    if name in ("toggle_bot", "pause_bot"):
                        self._start_poll()
                    self._start_hk_listener()

                QTimer.singleShot(0, apply)
                return False

            with Listener(on_press=on_press) as lst:
                lst.join()

        threading.Thread(target=listen, daemon=True).start()

    # ── on_unload ─────────────────────────────────────────────────────────────

    # ── overlay state sync ────────────────────────────────────────────────────

    def _update_overlay(self):
        ov      = self._overlay
        enabled = _cfg.get("overlay_enabled", default=False)

        # Hide when disabled or a different plugin is active
        if not enabled or not is_plugin_active("Magnet Bot"):
            if not ov._move_mode:
                ov.hide()
            return

        # Ensure visible (may have been hidden when navigating away)
        if not ov._move_mode:
            if not ov.isVisible():
                ov.show()
        else:
            return   # move mode handles its own display

        running       = self._engine.is_running
        manual_paused = self._engine.is_paused
        auto_pause_on = _cfg.get("auto_pause_unfocused", default=True)
        any_paused    = manual_paused or (auto_pause_on and not is_fivem_focused())

        if running and any_paused:
            ov.set_state("Paused", AMBER)
        elif running:
            text, color = _OV_TEXT.get(self._bot_state, ("Running", GREEN))
            ov.set_state(text, color)
        else:
            ov.set_state("Stopped", T3)

        # Update optional info rows
        opts    = _cfg.get("overlay_opts", default={})
        loops   = self._loops
        rate    = self._engine.get_loop_rate()        if running else 0.0
        runtime = self._engine.get_session_runtime() if running else 0.0
        ov.set_info(loops, rate, runtime, opts)

    def on_unload(self):
        """Stop all background threads before plugin is destroyed on hot-reload."""
        # Stop the bot engine (sets is_running = False, stops scan thread)
        try:
            self._engine.stop()
        except Exception:
            pass

        # Stop the setup-hotkey pynput listener
        if self._hk_listener is not None:
            try:
                self._hk_listener.stop()
            except Exception:
                pass
            self._hk_listener = None

        # Kill the toggle-bot poll thread by advancing the generation counter
        self._hk_gen += 1

        # Stop the overlay refresh timer and destroy the overlay window
        try:
            self._ov_timer.stop()
        except Exception:
            pass
        try:
            self._overlay.hide()
            self._overlay.deleteLater()
        except Exception:
            pass

        # Clear the module-level plugin reference so bridge signals are no-ops
        if _plugin_ref[0] is self:
            _plugin_ref[0] = None
