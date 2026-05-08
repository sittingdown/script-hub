"""
Magnet Minigame  —  press E, wait for the minigame, click each icon once,
wait for the minigame to clear, then loop.

Reuses overlays_qt (preview pickers), detector (color matching), bot
(FiveM focus check), and hub_sdk (UI). Saves to cfg/magnet_minigame_config.json.
"""
import json
import sys
import time
import threading
from pathlib import Path

import win32api
import win32con
from pynput.keyboard import Listener, Controller, Key, KeyCode
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QProgressBar,
    QFrame, QLabel,
)

# Ensure project root is on sys.path so we can import sibling modules.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hub_sdk import (
    PluginBase,
    T1, T2, T3, GREEN, AMBER, RED, BLUE,
    lbl, sep, kbd, section_header,
    make_tabs, scrollable,
    ToggleSwitch, NumericStepper, ColorSwatch,
    is_plugin_active,
)
from overlays_qt import (
    RegionSelectorOverlay, PointSelectorOverlay, ColorPickerOverlay,
)
from detector import capture_region, build_color_mask
from bot import is_fivem_focused
import numpy as np

# Optional scipy fast-path for pixel-level connected components.
# Falls back to a pure-numpy BFS if scipy isn't available.
try:
    from scipy.ndimage import label as _ndi_label   # type: ignore
    _HAS_SCIPY_LABEL = True
except Exception:
    _HAS_SCIPY_LABEL = False

# ── config ────────────────────────────────────────────────────────────────────

_CFG = _ROOT / "cfg" / "magnet_minigame_config.json"
_DEF = {
    "hotkeys": {
        "toggle":         {"key": "f3", "enabled": True},
        "pause":          {"key": "f9", "enabled": True},
        "emergency_stop": {"key": "f7", "enabled": True},
        "select_wait":    {"key": "f4", "enabled": True},
        "select_region":  {"key": "f5", "enabled": True},
        "select_color":   {"key": "f6", "enabled": True},
    },
    "wait_point":      {"x": 0, "y": 0},
    "region":          {"x": 0, "y": 0, "width": 0, "height": 0},
    "target_colors":   [],
    "color_tolerance": 10,

    "interaction_key":   "e",
    "post_e_delay":      1.00,
    "click_delay":       0.11,
    "scan_interval":     0.03,
    "min_cluster_px":    30,
    "dedupe_radius":     35,
    "max_targets":       10,
    "minigame_gone_for": 0.8,
    "post_round_delay":  3.40,
    "max_round_wait":    12.0,

    "auto_pause_unfocused": True,

    "stats": {
        "lifetime_rounds":    0,
        "lifetime_aborts":    0,
        "lifetime_clicks":    0,
        "lifetime_sessions":  0,
        "lifetime_runtime_s": 0.0,
        "best_rate_per_hr":   0.0,
        "fastest_round_s":    0.0,
        "session_history":    [],   # newest at the end, capped to 50
    },

    "overlay_enabled": False,
    "overlay_pos":     {"x": 20, "y": 60},
    "overlay_opts":    {
        "show_rounds":     True,
        "show_rate":       False,
        "show_runtime":    True,
        "show_clicks":     False,
        "show_last_round": False,
        "show_clusters":   False,
    },
}

def _migrate(loaded: dict) -> dict:
    """Bring old configs forward — supports the legacy string-only hotkey form."""
    hk = loaded.get("hotkeys")
    if isinstance(hk, dict):
        for name, val in list(hk.items()):
            if isinstance(val, str):
                hk[name] = {"key": val, "enabled": True}
    return loaded

def _merge_defaults(loaded: dict) -> dict:
    out = json.loads(json.dumps(_DEF))
    loaded = _migrate(loaded)
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            # one-level deep merge (good enough for hotkeys + nested dicts)
            for sk, sv in v.items():
                if isinstance(sv, dict) and isinstance(out[k].get(sk), dict):
                    out[k][sk].update(sv)
                else:
                    out[k][sk] = sv
        else:
            out[k] = v
    return out

def _load() -> dict:
    try:
        return _merge_defaults(json.loads(_CFG.read_text()))
    except Exception:
        return json.loads(json.dumps(_DEF))

def _save(c: dict):
    try:
        _CFG.parent.mkdir(parents=True, exist_ok=True)
        _CFG.write_text(json.dumps(c, indent=2))
    except Exception:
        pass

cfg = _load()

def _get_hk(name: str) -> tuple[str, bool]:
    h = cfg.get("hotkeys", {}).get(name) or {}
    if isinstance(h, str):
        return h, True
    return h.get("key", ""), bool(h.get("enabled", True))

def _set_hk(name: str, *, key: str | None = None, enabled: bool | None = None):
    cfg.setdefault("hotkeys", {}).setdefault(name, {"key": "", "enabled": True})
    if key is not None:
        cfg["hotkeys"][name]["key"] = key
    if enabled is not None:
        cfg["hotkeys"][name]["enabled"] = bool(enabled)
    _save(cfg)

# ── input helpers ─────────────────────────────────────────────────────────────

_kbd = Controller()

def _press_key(key_str: str, hold: float = 0.05):
    s = (key_str or "").lower()
    try:
        k = Key[s] if s in Key.__members__ else KeyCode.from_char(s)
    except Exception:
        return
    try:
        _kbd.press(k)
        time.sleep(hold)
        _kbd.release(k)
    except Exception:
        try: _kbd.release(k)
        except Exception: pass

def _click_at(x: int, y: int, hold: float = 0.10):
    win32api.SetCursorPos((int(x), int(y)))
    time.sleep(0.015)   # let FiveM register the cursor move before pressing
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

def _move_to(x: int, y: int):
    try:
        win32api.SetCursorPos((int(x), int(y)))
    except Exception:
        pass

# ── pixel-level connected components ──────────────────────────────────────────

def _find_components(mask, min_pixels: int):
    """
    True 4-connected components on a boolean mask. Returns [(cx, cy), ...]
    centroids in mask coords. Strictly better separation than the cell-grid
    flood-fill in detector.py — close-but-not-touching icons stay distinct.
    Uses scipy.ndimage.label when available for a C-speed pass; otherwise
    falls back to a pure-Python BFS that's still fast enough for typical
    region sizes (~5–10 ms per scan).
    """
    if not mask.any():
        return []
    h, w = mask.shape

    # Fast path — scipy connected components + bincount centroids
    if _HAS_SCIPY_LABEL:
        labels, n = _ndi_label(mask)
        if n == 0:
            return []
        flat = labels.ravel()
        ys, xs = np.indices((h, w), dtype=np.int32)
        counts = np.bincount(flat, minlength=n + 1)
        ysum   = np.bincount(flat, weights=ys.ravel(), minlength=n + 1)
        xsum   = np.bincount(flat, weights=xs.ravel(), minlength=n + 1)
        centers = []
        for lid in range(1, n + 1):
            c = int(counts[lid])
            if c >= min_pixels:
                centers.append((int(xsum[lid] / c), int(ysum[lid] / c)))
        return centers

    # Fallback — pure Python BFS, iterating only over True pixels
    visited = np.zeros((h, w), dtype=bool)
    centers = []
    ys_idx, xs_idx = np.where(mask)
    for sy, sx in zip(ys_idx.tolist(), xs_idx.tolist()):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        cy_sum = 0; cx_sum = 0; n = 0
        while stack:
            y, x = stack.pop()
            if y < 0 or y >= h or x < 0 or x >= w:
                continue
            if visited[y, x] or not mask[y, x]:
                continue
            visited[y, x] = True
            cy_sum += y; cx_sum += x; n += 1
            stack.append((y - 1, x))
            stack.append((y + 1, x))
            stack.append((y, x - 1))
            stack.append((y, x + 1))
        if n >= min_pixels:
            centers.append((cx_sum // n, cy_sum // n))
    return centers

# ── bridge: worker → Qt main thread ───────────────────────────────────────────

class _Bridge(QObject):
    update           = pyqtSignal()
    cooldown         = pyqtSignal(float, float, str)  # elapsed, total, label
    hk_toggle        = pyqtSignal()
    hk_pause         = pyqtSignal()
    hk_estop         = pyqtSignal()
    hk_select_wait   = pyqtSignal()
    hk_select_region = pyqtSignal()
    hk_select_color  = pyqtSignal()

_bridge = _Bridge()

# ── shared runtime state ──────────────────────────────────────────────────────

class _State:
    enabled       = False
    paused        = False
    focused       = True
    phase         = "STOPPED"
    clicks_round  = 0
    rounds_done   = 0
    last_clusters = 0
    session_start       = None    # monotonic seconds, or None when stopped
    session_started_at  = None    # wall-clock unix time at session start
    session_aborts      = 0       # rounds aborted in this session
    session_clicks      = 0       # total icon clicks in this session
    fastest_round_s     = None    # fastest single round in this session
    last_round_s        = None    # seconds the last round took
    round_start         = None    # monotonic seconds, current round start

state = _State()

# ── worker (state machine) ────────────────────────────────────────────────────

class _Worker:
    def __init__(self):
        self._running = False
        self._thread  = None
        self._clicked = []

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=0.5)
        self._thread = None

    def reset_round(self):
        self._clicked = []
        state.clicks_round = 0

    def _is_new_target(self, x: int, y: int, radius: int) -> bool:
        r2 = radius * radius
        for cx, cy in self._clicked:
            if (cx - x) * (cx - x) + (cy - y) * (cy - y) < r2:
                return False
        return True

    def _continue(self) -> bool:
        return self._running and state.enabled and not state.paused and (
            (not cfg.get("auto_pause_unfocused", True)) or state.focused
        )

    def _cd_sleep(self, secs: float, label: str):
        """Sleep emitting cooldown progress. Aborts if stopped/paused."""
        total = max(0.0, float(secs))
        if total <= 0:
            return
        start = time.monotonic()
        while True:
            if not self._continue():
                return
            elapsed = time.monotonic() - start
            if elapsed >= total:
                _bridge.cooldown.emit(total, total, "")
                return
            _bridge.cooldown.emit(elapsed, total, label)
            time.sleep(0.05)

    def _run(self):
        while self._running:
            try:
                if not state.enabled:
                    state.phase = "STOPPED"
                    self.reset_round()
                    _bridge.update.emit()
                    time.sleep(0.05)
                    continue

                if state.paused:
                    state.phase = "PAUSED"
                    _bridge.update.emit()
                    time.sleep(0.05)
                    continue

                if cfg.get("auto_pause_unfocused", True) and not state.focused:
                    state.phase = "UNFOCUSED"
                    _bridge.update.emit()
                    time.sleep(0.1)
                    continue

                # ── PRESS E ───────────────────────────────────────────────────
                state.phase = "PRESS_E"
                state.round_start = time.monotonic()
                _bridge.update.emit()
                _press_key(cfg.get("interaction_key", "e"), hold=0.05)
                self._cd_sleep(cfg.get("post_e_delay", 0.6), "post-E delay")
                if not self._continue(): continue

                # ── PARK MOUSE ────────────────────────────────────────────────
                state.phase = "PARK"
                _bridge.update.emit()
                wp = cfg.get("wait_point") or {}
                if wp.get("x") or wp.get("y"):
                    _move_to(wp["x"], wp["y"])
                time.sleep(0.05)
                if not self._continue(): continue

                # ── SCAN ──────────────────────────────────────────────────────
                self.reset_round()
                state.phase = "SCAN"
                _bridge.update.emit()
                gone_for    = float(cfg.get("minigame_gone_for", 0.8))
                max_wait    = float(cfg.get("max_round_wait", 12.0))
                tolerance   = int(cfg.get("color_tolerance", 10))
                min_px      = int(cfg.get("min_cluster_px", 30))
                dedupe_r    = int(cfg.get("dedupe_radius", 35))
                max_targets = int(cfg.get("max_targets", 10))
                colors      = cfg.get("target_colors", [])
                region      = cfg.get("region", {})
                click_h     = float(cfg.get("click_delay", 0.10))
                scan_iv     = float(cfg.get("scan_interval", 0.05))
                wp          = cfg.get("wait_point") or {}
                rgx, rgy    = region["x"], region["y"]

                if not colors or not region.get("width") or not region.get("height"):
                    state.phase = "NEEDS_SETUP"
                    _bridge.update.emit()
                    state.enabled = False
                    continue

                scan_start    = time.monotonic()
                last_seen     = scan_start
                seen_anything = False
                aborted       = False

                while self._continue() and state.phase == "SCAN":
                    now = time.monotonic()
                    img = capture_region(region)
                    mask = build_color_mask(img, colors, tolerance)
                    centers = _find_components(mask, min_px)
                    state.last_clusters = len(centers)

                    if centers:
                        seen_anything = True
                        last_seen = now
                        # cap defensively in case false positives appear
                        for cx, cy in centers[:max_targets]:
                            if not self._continue():
                                break
                            ax = rgx + cx
                            ay = rgy + cy
                            if self._is_new_target(ax, ay, dedupe_r):
                                _click_at(ax, ay, hold=click_h)
                                self._clicked.append((ax, ay))
                                state.clicks_round  += 1
                                state.session_clicks += 1
                                _bridge.update.emit()
                                # park cursor between clicks so it doesn't
                                # cover the next icon's colour pixels
                                if wp.get("x") or wp.get("y"):
                                    _move_to(wp["x"], wp["y"])
                    else:
                        if seen_anything:
                            if (now - last_seen) >= gone_for:
                                break
                        elif max_wait > 0 and (now - scan_start) >= max_wait:
                            # icons never appeared — bail and try again
                            aborted = True
                            break
                    time.sleep(scan_iv)

                if not self._continue(): continue

                # ── ROUND DONE ────────────────────────────────────────────────
                if not aborted:
                    state.rounds_done += 1
                    if state.round_start is not None:
                        dur = time.monotonic() - state.round_start
                        state.last_round_s = dur
                        if (state.fastest_round_s is None
                                or dur < state.fastest_round_s):
                            state.fastest_round_s = dur
                else:
                    state.session_aborts += 1
                self.reset_round()
                state.phase = "COOLDOWN"
                _bridge.update.emit()
                self._cd_sleep(cfg.get("post_round_delay", 0.5),
                               "retrying — no icons" if aborted else "cooldown")

            except Exception:
                time.sleep(0.2)

_worker = _Worker()

# ── UI helpers ────────────────────────────────────────────────────────────────

def _hrow(*widgets):
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(10)
    for i, w in enumerate(widgets):
        if isinstance(w, QWidget):
            h.addWidget(w)
        else:
            h.addLayout(w)
        if i == 0:
            h.addStretch()
    return h

_PHASE_TEXT = {
    "PRESS_E":     ("PRESSING E",  GREEN),
    "PARK":        ("PARKING",     GREEN),
    "SCAN":        ("SCANNING",    GREEN),
    "COOLDOWN":    ("COOLDOWN",    BLUE),
    "NEEDS_SETUP": ("NEEDS SETUP", RED),
    "UNFOCUSED":   ("UNFOCUSED",   AMBER),
    "PAUSED":      ("PAUSED",      AMBER),
    "STOPPED":     ("STOPPED",     T3),
}

# Friendlier title-case phrasing for the overlay
_OV_PHASE = {
    "PRESS_E":     ("Casting",      GREEN),
    "PARK":        ("Parking",      GREEN),
    "SCAN":        ("Scanning",     GREEN),
    "COOLDOWN":    ("Cooldown",     BLUE),
    "NEEDS_SETUP": ("Needs setup",  RED),
}

# Display-row keys → label format. Order = display order in the overlay.
_OV_ROWS = (
    ("rounds",     "Rounds"),
    ("rate",       "Rate"),
    ("runtime",    "Runtime"),
    ("clicks",     "Clicks"),
    ("last_round", "Last round"),
    ("clusters",   "Clusters"),
)

def _fmt_time(secs: float) -> str:
    if secs <= 0:
        return "0:00"
    h, rem = divmod(int(secs), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ── Status Overlay ────────────────────────────────────────────────────────────

class _StatusOverlay(QWidget):
    """
    Always-on-top, click-through, slightly translucent status card.
    Optional rows — caller toggles each via ``set_row``.
    Drag to move when ``enter_move_mode`` is active; otherwise mouse and
    keyboard events pass straight through to whatever's beneath.
    Updates are change-detected so unchanged ticks do zero paint work.
    """

    _BASE_FLAGS = (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )
    _PASS_THROUGH = _BASE_FLAGS | Qt.WindowType.WindowTransparentForInput

    def __init__(self):
        super().__init__(None, self._PASS_THROUGH)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background: transparent;")
        self._drag_pos  = None
        self._move_mode = False
        self._cache     = {}        # change-detection for repaints

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QFrame()
        self._card.setObjectName("ov_card")
        self._card.setStyleSheet(
            "QFrame#ov_card {"
            "  background: rgba(8, 8, 8, 215);"
            "  border-radius: 10px;"
            "  border: 1px solid rgba(255, 255, 255, 22);"
            "}"
            "QLabel { background: transparent; border: none; }"
        )
        cl = QVBoxLayout(self._card)
        cl.setContentsMargins(12, 8, 14, 8)
        cl.setSpacing(3)

        # ── status row (dot + state) ──────────────────────────────────────────
        sr = QHBoxLayout()
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(7)
        self._dot = QFrame()
        self._dot.setFixedSize(8, 8)
        self._dot.setStyleSheet(f"background:{T3}; border-radius:4px; border:none;")
        self._lbl = QLabel("Stopped")
        self._lbl.setStyleSheet(
            f"color:{T3}; font-size:12px; font-weight:700;"
            " font-family:'Segoe UI',sans-serif;"
        )
        sr.addWidget(self._dot)
        sr.addWidget(self._lbl)
        sr.addStretch()
        cl.addLayout(sr)

        # ── info rows (one per key, hidden by default) ────────────────────────
        info_style = (
            f"color:{T2}; font-size:10px;"
            " font-family:'Segoe UI',sans-serif;"
        )
        self._rows: dict[str, QLabel] = {}
        for key, _label in _OV_ROWS:
            row = QLabel("")
            row.setStyleSheet(info_style)
            row.hide()
            self._rows[key] = row
            cl.addWidget(row)

        outer.addWidget(self._card)
        self.adjustSize()

    # ── public API ────────────────────────────────────────────────────────────

    def set_state(self, text: str, color: str):
        if self._cache.get("state") == (text, color):
            return
        self._cache["state"] = (text, color)
        self._lbl.setText(text)
        self._lbl.setStyleSheet(
            f"color:{color}; font-size:12px; font-weight:700;"
            " font-family:'Segoe UI',sans-serif;"
        )
        self._dot.setStyleSheet(
            f"background:{color}; border-radius:4px; border:none;"
        )

    def set_row(self, key: str, text: str, visible: bool):
        widget = self._rows.get(key)
        if not widget:
            return
        ckey = f"row_{key}"
        cur  = (text, visible)
        if self._cache.get(ckey) == cur:
            return
        prev_visible = self._cache.get(ckey, (None, None))[1]
        self._cache[ckey] = cur
        if visible:
            widget.setText(text)
            if not prev_visible:
                widget.show()
                self.adjustSize()
        else:
            if prev_visible:
                widget.hide()
                self.adjustSize()

    # ── move mode ─────────────────────────────────────────────────────────────

    def enter_move_mode(self):
        self._move_mode = True
        # Re-show with input enabled, then signal "drag me"
        self.setWindowFlags(self._BASE_FLAGS)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._lbl.setText("Drag to move")
        self._lbl.setStyleSheet(
            "color:#60a5fa; font-size:12px; font-weight:700;"
            " font-family:'Segoe UI',sans-serif;"
        )
        self._dot.setStyleSheet(
            "background:#60a5fa; border-radius:4px; border:none;"
        )
        for w in self._rows.values():
            w.hide()
        self._cache.clear()   # force redraw on exit
        self.adjustSize()
        self.show()

    def exit_move_mode(self):
        self._move_mode = False
        self.setWindowFlags(self._PASS_THROUGH)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.show()

    # ── drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if self._move_mode and ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (ev.globalPosition().toPoint()
                              - self.frameGeometry().topLeft())
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._move_mode and self._drag_pos is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        self._drag_pos = None
        ev.accept()

# ── Plugin ────────────────────────────────────────────────────────────────────

class Plugin(PluginBase):
    NAME        = "Magnet Minigame"
    DESCRIPTION = "Auto-clicks the magnet hacking minigame icons."
    ACCENT      = "#60a5fa"
    TAGS        = ["FiveM", "Minigame"]
    VERSION     = "1.0"

    _HOTKEYS = [
        ("toggle",         "Toggle Bot",          "Toggle"),
        ("pause",           "Pause / Resume",     "Pause"),
        ("select_wait",     "Select Wait Point",  "Wait Pt"),
        ("select_region",   "Select Region",      "Region"),
        ("select_color",    "Pick Color",         "Color"),
        ("emergency_stop",  "Emergency Stop",     "Stop"),
    ]

    def __init__(self):
        self._listener        = None
        self._capturing_for   = None
        self._active_overlay  = None   # holds picker overlay so PyQt doesn't GC it
        self._badges          = {}    # name → rebind QPushButton (Hotkeys tab)
        self._hk_chips        = {}    # name → chip QLabel (dashboard strip)
        self._color_list_w    = None
        self._wait_lbl        = None
        self._region_lbl      = None
        self._ik_badge        = None
        self._dash            = {}    # field name → QWidget for refresh

        _bridge.hk_toggle.connect(self._do_toggle)
        _bridge.hk_pause.connect(self._do_pause)
        _bridge.hk_estop.connect(self._do_emergency_stop)
        _bridge.hk_select_wait.connect(self._do_pick_wait)
        _bridge.hk_select_region.connect(self._do_pick_region)
        _bridge.hk_select_color.connect(self._do_pick_color)

        # ── Status overlay — lives for the plugin's lifetime ───────────────────
        self._overlay = _StatusOverlay()
        op = cfg.get("overlay_pos", {}) or {}
        self._overlay.move(int(op.get("x", 20)), int(op.get("y", 60)))
        self._overlay_moving = False
        self._overlay_timer  = QTimer()
        self._overlay_timer.timeout.connect(self._update_overlay)
        self._overlay_timer.start(500)

        _worker.start()
        self._start_listener()

    # ── card live status ──────────────────────────────────────────────────────

    def status(self):
        if not state.enabled:
            return "STOPPED", T3
        if state.paused:
            return "PAUSED", AMBER
        if cfg.get("auto_pause_unfocused", True) and not state.focused:
            return "UNFOCUSED", AMBER
        text, color = _PHASE_TEXT.get(state.phase, ("RUNNING", GREEN))
        return text, color

    # ── build_page ────────────────────────────────────────────────────────────

    def build_page(self, nav_back):
        container, stack = make_tabs(["Dashboard", "Settings", "Stats", "Hotkeys"])
        stack.addWidget(self._build_dashboard())
        stack.addWidget(scrollable(self._build_settings()))
        stack.addWidget(scrollable(self._build_stats()))
        stack.addWidget(scrollable(self._build_hotkeys()))
        # focus tick
        t = QTimer(container)
        t.timeout.connect(self._tick)
        t.start(250)
        self._refresh_dashboard()
        return container

    # ══════════════════════════════════════════════════════════════════════════
    # Dashboard tab
    # ══════════════════════════════════════════════════════════════════════════

    def _build_dashboard(self):
        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # state indicator
        state_lbl = lbl("STOPPED", sz=22, col=T3, bold=True,
                        align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(state_lbl)
        root.addSpacing(6)

        # cooldown bar (hidden when idle)
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
        lay, rounds_lbl   = stat_pair("Rounds");        sr.addLayout(lay); sr.addSpacing(22)
        lay, rate_lbl     = stat_pair("Rounds / hr");   sr.addLayout(lay); sr.addSpacing(22)
        lay, runtime_lbl  = stat_pair("Runtime");       sr.addLayout(lay); sr.addSpacing(22)
        lay, clicks_lbl   = stat_pair("Clicks");        sr.addLayout(lay); sr.addSpacing(22)
        lay, last_lbl     = stat_pair("Last Round");    sr.addLayout(lay)
        sr.addStretch()
        root.addLayout(sr)
        root.addSpacing(14)

        root.addWidget(sep())
        root.addSpacing(8)

        # config summary
        cfg_lbl = lbl("", sz=10, col=T3, align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(cfg_lbl)
        root.addSpacing(8)

        root.addWidget(sep())
        root.addSpacing(8)

        # hotkey quick-reference strip
        self._hk_chips = {}
        hk_strip = QHBoxLayout(); hk_strip.setSpacing(0); hk_strip.addStretch()
        for name, _label, abbr in self._HOTKEYS:
            k, en = _get_hk(name)
            col_lay = QVBoxLayout(); col_lay.setSpacing(3)
            col_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_lay.addWidget(lbl(abbr, sz=9, col=T3,
                                  align=Qt.AlignmentFlag.AlignCenter))
            chip = kbd((k or "—") if en else "off")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._hk_chips[name] = chip
            col_lay.addWidget(chip)
            hk_strip.addLayout(col_lay)
            hk_strip.addSpacing(18)
        hk_strip.addStretch()
        root.addLayout(hk_strip)
        root.addSpacing(10)

        # colors info
        colors_info = lbl("", sz=11, col=T2)
        cr_row = QHBoxLayout()
        cr_row.addWidget(lbl("Target Colors:", sz=11, col=T3))
        cr_row.addSpacing(8)
        cr_row.addWidget(colors_info)
        cr_row.addStretch()
        root.addLayout(cr_row)
        root.addStretch()

        # store refresh widgets
        self._dash = {
            "state":   state_lbl,
            "cd_bar":  cd_bar,
            "cd_lbl":  cd_lbl,
            "rounds":  rounds_lbl,
            "rate":    rate_lbl,
            "runtime": runtime_lbl,
            "clicks":  clicks_lbl,
            "last":    last_lbl,
            "cfg":     cfg_lbl,
            "colors":  colors_info,
        }

        # cooldown signal updates the bar
        def _on_cd(elapsed, total, label):
            if total <= 0 or not label:
                cd_bar.hide(); cd_lbl.hide()
                return
            pct = int(min(elapsed / max(total, 0.01), 1.0) * 1000)
            cd_bar.setValue(pct)
            cd_lbl.setText(label)
            cd_bar.show(); cd_lbl.show()
        _bridge.cooldown.connect(_on_cd)

        # debounced refresh on update
        _debounce = QTimer(inner)
        _debounce.setSingleShot(True)
        _debounce.setInterval(80)
        _debounce.timeout.connect(self._refresh_dashboard)
        _bridge.update.connect(_debounce.start)

        # main 500ms tick — keeps runtime / rate live
        timer = QTimer(inner)
        timer.timeout.connect(self._refresh_dashboard)
        timer.start(500)

        return inner

    def _refresh_dashboard(self):
        d = self._dash
        if not d:
            return
        running = state.enabled
        manual_paused = state.paused
        unfocus_paused = (cfg.get("auto_pause_unfocused", True)
                          and not state.focused)

        if running and (manual_paused or unfocus_paused):
            if manual_paused:
                pk, _ = _get_hk("pause")
                d["state"].setText(f"PAUSED — press {pk.upper()} to resume")
            else:
                d["state"].setText("PAUSED — focus FiveM")
            d["state"].setStyleSheet(
                f"color:{AMBER}; font-size:22px; font-weight:700; background:transparent;")
        elif running:
            text, color = _PHASE_TEXT.get(state.phase, ("RUNNING", GREEN))
            d["state"].setText(text)
            d["state"].setStyleSheet(
                f"color:{color}; font-size:22px; font-weight:700; background:transparent;")
            if state.phase != "COOLDOWN":
                d["cd_bar"].hide(); d["cd_lbl"].hide()
        else:
            tk, _ = _get_hk("toggle")
            d["state"].setText(f"STOPPED — press {tk.upper()}")
            d["state"].setStyleSheet(
                f"color:{T3}; font-size:22px; font-weight:700; background:transparent;")
            d["cd_bar"].hide(); d["cd_lbl"].hide()

        # stats
        rt = (time.monotonic() - state.session_start) if state.session_start else 0.0
        rate = (state.rounds_done / (rt / 3600)) if rt > 0 else 0.0
        d["rounds"].setText(str(state.rounds_done))
        d["rate"].setText(f"{rate:.1f}")
        d["runtime"].setText(_fmt_time(rt))
        d["clicks"].setText(str(state.clicks_round))
        d["last"].setText(
            f"{state.last_round_s:.1f}s" if state.last_round_s is not None else "—"
        )

        # config summary
        rg = cfg.get("region", {}) or {}
        wp = cfg.get("wait_point", {}) or {}
        n_col = len(cfg.get("target_colors", []))
        rg_str = (f"{rg['width']}×{rg['height']}"
                  if rg.get("width") and rg.get("height") else "no region")
        wp_str = (f"({wp['x']}, {wp['y']})"
                  if wp.get("x") or wp.get("y") else "no wait pt")
        tol = cfg.get("color_tolerance", 18)
        d["cfg"].setText(
            f"Region {rg_str}  ·  Wait {wp_str}  ·  "
            f"{n_col} color{'s' if n_col != 1 else ''}  ·  Tol {tol}"
        )

        # colors summary line
        d["colors"].setText(
            f"{n_col} color{'s' if n_col != 1 else ''} configured"
            if n_col else "No colors set — configure in Settings"
        )

        # hotkey strip
        for name, chip in self._hk_chips.items():
            k, en = _get_hk(name)
            chip.setText((k.upper() if k else "—") if en else "OFF")

    # ══════════════════════════════════════════════════════════════════════════
    # Settings tab — targets + tuning (no hotkeys; those live in Hotkeys tab)
    # ══════════════════════════════════════════════════════════════════════════

    def _build_settings(self):
        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setContentsMargins(0, 8, 8, 16)
        root.setSpacing(0)

        # ── Wait Point ────────────────────────────────────────────────────────
        root.addWidget(section_header("Wait Point"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "The point where the cursor parks while the minigame is open "
            "so it doesn't sit on top of an icon.",
            sz=10, col=T3, wrap=True))
        root.addSpacing(8)

        wp = cfg.get("wait_point", {}) or {}
        self._wait_lbl = lbl(self._fmt_wait(wp), sz=12, col=T1, bold=True)
        root.addLayout(_hrow(self._wait_lbl, self._hk_hint("select_wait")))
        root.addSpacing(20)

        # ── Region ────────────────────────────────────────────────────────────
        root.addWidget(section_header("Region"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "Drag to select the rectangle where the minigame icons appear.",
            sz=10, col=T3, wrap=True))
        root.addSpacing(8)

        rg = cfg.get("region", {}) or {}
        self._region_lbl = lbl(self._fmt_region(rg), sz=12, col=T1, bold=True)
        root.addLayout(_hrow(self._region_lbl, self._hk_hint("select_region")))
        root.addSpacing(20)

        # ── Target Colors ─────────────────────────────────────────────────────
        root.addWidget(section_header("Target Colors"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "Pick the PURPLE icon body — that's the same color on every "
            "icon (anchor, bicycle, blade, etc.) at every level.",
            sz=10, col=T3, wrap=True))
        root.addSpacing(2)
        root.addWidget(lbl(
            "Do NOT pick the blue ring around the icons — that's a water "
            "ripple animation, not a clickable target. Picking it will "
            "cause false clicks.",
            sz=10, col=AMBER, wrap=True))
        root.addSpacing(8)

        self._color_list_w = QWidget()
        self._color_list_w.setStyleSheet("background:transparent;")
        cv = QVBoxLayout(self._color_list_w)
        cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(4)
        root.addWidget(self._color_list_w)
        self._refresh_color_list()
        root.addSpacing(8)
        col_hint = QHBoxLayout(); col_hint.setContentsMargins(0, 0, 0, 0)
        col_hint.addStretch()
        col_hint.addLayout(self._hk_hint("select_color"))
        root.addLayout(col_hint)
        root.addSpacing(20)

        # ── Color Tolerance ───────────────────────────────────────────────────
        root.addWidget(section_header("Color Tolerance"))
        root.addSpacing(8)

        tol = NumericStepper(cfg.get("color_tolerance", 10),
                             step=1, min_val=1, max_val=100, fmt="{:.0f}")
        tol.changed.connect(lambda v: (cfg.update({"color_tolerance": int(v)}),
                                       _save(cfg)))
        root.addLayout(_hrow(lbl("Tolerance  (recommended: 10)", sz=12, col=T2), tol))
        root.addSpacing(20)

        # ── Detection ─────────────────────────────────────────────────────────
        root.addWidget(section_header("Detection"))
        root.addSpacing(8)

        for label_text, key, lo, hi, step, fmt in [
            ("Min Cluster Pixels",     "min_cluster_px",     5,  200,   1,  "{:.0f}"),
            ("Dedupe Radius (px)",     "dedupe_radius",     10,  100,   5,  "{:.0f}"),
            ("Max Targets / Scan",     "max_targets",        1,   25,   1,  "{:.0f}"),
            ("Round-end Idle (s)",     "minigame_gone_for", 0.2, 3.0, 0.1,  "{:.1f}"),
        ]:
            is_int = step >= 1 and fmt == "{:.0f}"
            st = NumericStepper(cfg.get(key, _DEF[key]),
                                step=step, min_val=lo, max_val=hi, fmt=fmt)
            def _changed(v, k=key, ai=is_int):
                cfg[k] = int(v) if ai else v
                _save(cfg)
            st.changed.connect(_changed)
            root.addLayout(_hrow(lbl(label_text, sz=12, col=T2), st))
            root.addSpacing(6)
        root.addSpacing(14)

        # ── Timing ────────────────────────────────────────────────────────────
        root.addWidget(section_header("Timing"))
        root.addSpacing(8)

        for label_text, key, lo, hi, step, fmt in [
            ("Post-E Delay (s)",       "post_e_delay",     0.0, 3.0, 0.05, "{:.2f}"),
            ("Cooldown (s)",           "post_round_delay", 0.0, 5.0, 0.05, "{:.2f}"),
            ("Click Delay (s)",        "click_delay",      0.01, 1.0, 0.01, "{:.2f}"),
            ("Scan Interval (s)",      "scan_interval",   0.02, 0.5, 0.01, "{:.2f}"),
            ("Max Round Wait (s)",     "max_round_wait",  0.0, 60.0, 1.0, "{:.0f}"),
        ]:
            st = NumericStepper(cfg.get(key, _DEF[key]),
                                step=step, min_val=lo, max_val=hi, fmt=fmt)
            st.changed.connect(lambda v, k=key: (cfg.update({k: v}), _save(cfg)))
            root.addLayout(_hrow(lbl(label_text, sz=12, col=T2), st))
            root.addSpacing(6)
        root.addSpacing(14)

        # ── Options ───────────────────────────────────────────────────────────
        root.addWidget(section_header("Options"))
        root.addSpacing(8)

        # Interaction key (rebindable badge)
        ik_cur = cfg.get("interaction_key", "e")
        ik_badge = QPushButton(ik_cur.upper())
        ik_badge.setObjectName("badge")
        ik_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        ik_badge.setToolTip("Click to rebind")
        ik_badge.setProperty("capturing", False)
        self._ik_badge = ik_badge
        ik_badge.clicked.connect(self._start_ik_capture)
        root.addLayout(_hrow(lbl("Interaction Key", sz=12, col=T2), ik_badge))
        root.addSpacing(10)

        sw = ToggleSwitch(cfg.get("auto_pause_unfocused", True))
        sw.toggled.connect(lambda v: (cfg.update({"auto_pause_unfocused": v}),
                                      _save(cfg)))
        root.addLayout(_hrow(lbl("Auto-pause when unfocused", sz=12, col=T2), sw))
        root.addSpacing(20)

        # ── Status Overlay ────────────────────────────────────────────────────
        root.addWidget(section_header("Status Overlay"))
        root.addSpacing(6)
        root.addWidget(lbl(
            "Always-on-top status card. Click-through, slightly translucent, "
            "positioned anywhere on screen. Doesn't block FiveM input.",
            sz=10, col=T3, wrap=True))
        root.addSpacing(12)

        ov_sw    = ToggleSwitch(cfg.get("overlay_enabled", False))
        move_btn = QPushButton("Move")
        move_btn.setObjectName("badge")
        move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        move_btn.setFixedWidth(64)

        def _on_ov_toggle(v):
            cfg["overlay_enabled"] = v
            _save(cfg)
            self._update_overlay()
            if v and not self._overlay.isVisible() and not self._overlay._move_mode:
                self._overlay.show()
            elif not v and not self._overlay_moving:
                self._overlay.hide()

        def _on_move():
            ov = self._overlay
            if not self._overlay_moving:
                self._overlay_moving = True
                ov.show()
                ov.enter_move_mode()
                move_btn.setText("Done")
            else:
                self._overlay_moving = False
                p = ov.pos()
                cfg["overlay_pos"] = {"x": p.x(), "y": p.y()}
                _save(cfg)
                ov.exit_move_mode()
                if cfg.get("overlay_enabled", False):
                    self._update_overlay()
                else:
                    ov.hide()
                move_btn.setText("Move")

        ov_sw.toggled.connect(_on_ov_toggle)
        move_btn.clicked.connect(_on_move)

        ov_row = QHBoxLayout()
        ov_row.setContentsMargins(0, 0, 0, 0); ov_row.setSpacing(10)
        ov_row.addWidget(lbl("Show overlay", sz=12, col=T2))
        ov_row.addStretch()
        ov_row.addWidget(move_btn)
        ov_row.addSpacing(4)
        ov_row.addWidget(ov_sw)
        root.addLayout(ov_row)
        root.addSpacing(12)

        # per-row visibility toggles
        root.addWidget(lbl("Display on overlay", sz=10, col=T3))
        root.addSpacing(8)

        opts = cfg.get("overlay_opts", {}) or {}
        for key, label in _OV_ROWS:
            ck = f"show_{key}"
            sw_row = ToggleSwitch(bool(opts.get(ck, False)))
            def _on_opt(v, k=ck):
                cur = dict(cfg.get("overlay_opts", {}) or {})
                cur[k] = v
                cfg["overlay_opts"] = cur
                _save(cfg)
                self._update_overlay()
            sw_row.toggled.connect(_on_opt)
            opt_row = QHBoxLayout()
            opt_row.setContentsMargins(12, 0, 0, 0); opt_row.setSpacing(8)
            opt_row.addWidget(lbl(label, sz=12, col=T2))
            opt_row.addStretch()
            opt_row.addWidget(sw_row)
            root.addLayout(opt_row)
            root.addSpacing(4)

        root.addStretch()
        return inner

    def _hk_hint(self, action: str) -> QHBoxLayout:
        """Right-aligned 'press <KEY> to set' hint chip."""
        k, en = _get_hk(action)
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
        h.addWidget(lbl("press", sz=10, col=T3))
        h.addWidget(kbd((k or "—").upper() if en else "off"))
        h.addWidget(lbl("to set", sz=10, col=T3))
        return h

    def _fmt_wait(self, wp: dict) -> str:
        if wp.get("x") or wp.get("y"):
            return f"({wp['x']}, {wp['y']})"
        return "— not set —"

    def _fmt_region(self, r: dict) -> str:
        if r.get("width") and r.get("height"):
            return f"{r['width']}×{r['height']} @ ({r['x']}, {r['y']})"
        return "— not set —"

    def _refresh_color_list(self):
        if not self._color_list_w:
            return
        layout = self._color_list_w.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, c in enumerate(list(cfg.get("target_colors", []))):
            r, g, b = c["r"], c["g"], c["b"]
            sw = ColorSwatch(r, g, b)
            sw.deleted.connect(lambda _=None, idx=i: self._delete_color(idx))
            layout.addWidget(sw)

    def _delete_color(self, idx: int):
        try:
            cfg["target_colors"].pop(idx)
            _save(cfg)
            self._refresh_color_list()
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # Stats tab — lifetime totals, last session, session history
    # ══════════════════════════════════════════════════════════════════════════

    def _build_stats(self):
        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setContentsMargins(0, 8, 0, 8)
        root.setSpacing(0)

        self._stats_widgets: dict = {}   # field name → QLabel

        def stat_col(caption, font_sz=18):
            lay = QVBoxLayout(); lay.setSpacing(2)
            cap_w = lbl(caption, sz=10, col=T3,
                        align=Qt.AlignmentFlag.AlignCenter)
            val_w = lbl("—", sz=font_sz, col=T1, bold=True,
                        align=Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(cap_w); lay.addWidget(val_w)
            return lay, val_w

        def kv_row(label_text, key):
            row = QHBoxLayout(); row.setSpacing(0)
            row.addWidget(lbl(label_text, sz=12, col=T2))
            row.addStretch()
            v = lbl("—", sz=12, col=T1, bold=True)
            self._stats_widgets[key] = v
            row.addWidget(v)
            root.addLayout(row)
            root.addSpacing(4)

        # ── LIFETIME ─────────────────────────────────────────────────────────
        root.addWidget(section_header("Lifetime"))
        root.addSpacing(12)

        sr1 = QHBoxLayout(); sr1.setSpacing(0); sr1.addStretch()
        for caption, key in (("Rounds",   "lt_rounds"),
                             ("Clicks",   "lt_clicks"),
                             ("Sessions", "lt_sessions"),
                             ("Runtime",  "lt_runtime")):
            lay, w = stat_col(caption)
            self._stats_widgets[key] = w
            sr1.addLayout(lay); sr1.addSpacing(28)
        sr1.addStretch()
        root.addLayout(sr1)
        root.addSpacing(14)

        sr2 = QHBoxLayout(); sr2.setSpacing(0); sr2.addStretch()
        for caption, key in (("Avg Rate / hr", "lt_rate"),
                             ("Avg / Round",   "lt_avg_per_round"),
                             ("Best Rate",     "lt_best_rate"),
                             ("Fastest Round", "lt_fastest")):
            lay, w = stat_col(caption, font_sz=16)
            self._stats_widgets[key] = w
            sr2.addLayout(lay); sr2.addSpacing(28)
        sr2.addStretch()
        root.addLayout(sr2)
        root.addSpacing(14)

        # success rate (centred, large)
        sr3 = QHBoxLayout(); sr3.addStretch()
        lay, w = stat_col("Success Rate", font_sz=22)
        self._stats_widgets["lt_success"] = w
        sr3.addLayout(lay); sr3.addStretch()
        root.addLayout(sr3)
        root.addSpacing(12)

        rst_btn = QPushButton("Reset Lifetime Stats")
        rst_btn.setObjectName("badge")
        rst_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rst_btn.setFixedWidth(180)
        rst_row = QHBoxLayout(); rst_row.addStretch(); rst_row.addWidget(rst_btn)
        root.addLayout(rst_row)
        root.addSpacing(16)
        root.addWidget(sep())
        root.addSpacing(14)

        # ── LAST SESSION ─────────────────────────────────────────────────────
        root.addWidget(section_header("Last Session"))
        root.addSpacing(8)

        kv_row("Date",          "ls_date")
        kv_row("Rounds",        "ls_rounds")
        kv_row("Aborts",        "ls_aborts")
        kv_row("Clicks",        "ls_clicks")
        kv_row("Runtime",       "ls_runtime")
        kv_row("Rate / hr",     "ls_rate")
        kv_row("Fastest Round", "ls_fastest")
        kv_row("Success Rate",  "ls_success")

        root.addSpacing(14)
        root.addWidget(sep())
        root.addSpacing(14)

        # ── HISTORY ──────────────────────────────────────────────────────────
        clr_btn = QPushButton("Clear")
        clr_btn.setObjectName("badge")
        clr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clr_btn.setFixedWidth(64)
        sh_hdr = QHBoxLayout()
        sh_hdr.setContentsMargins(0, 0, 0, 0)
        sh_hdr.addWidget(section_header("Session History"))
        sh_hdr.addStretch()
        sh_hdr.addWidget(clr_btn)
        root.addLayout(sh_hdr)
        root.addSpacing(6)

        # column header strip
        _W_DATE, _W_ROUNDS, _W_RUN, _W_RATE, _W_SUCC = 110, 56, 64, 64, 56
        def hist_row(d, r, rt, ra, su, header=False):
            row = QWidget(); row.setStyleSheet("background:transparent;")
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 1 if header else 2, 4, 1 if header else 2)
            h.setSpacing(0)
            d.setFixedWidth(_W_DATE); h.addWidget(d)
            h.addStretch()
            for w, width in ((r, _W_ROUNDS), (rt, _W_RUN), (ra, _W_RATE), (su, _W_SUCC)):
                w.setFixedWidth(width)
                w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                h.addWidget(w); h.addSpacing(10)
            return row

        root.addWidget(hist_row(
            lbl("Date",    sz=10, col=T3),
            lbl("Rounds",  sz=10, col=T3),
            lbl("Runtime", sz=10, col=T3),
            lbl("Rate",    sz=10, col=T3),
            lbl("Succ%",   sz=10, col=T3),
            header=True,
        ))
        root.addSpacing(2)
        root.addWidget(sep())
        root.addSpacing(2)

        hist_w = QWidget(); hist_w.setStyleSheet("background:transparent;")
        hist_v = QVBoxLayout(hist_w)
        hist_v.setContentsMargins(0, 0, 0, 0); hist_v.setSpacing(1)
        root.addWidget(hist_w)
        root.addStretch()

        # ── refresh + reset handlers ─────────────────────────────────────────

        def _rebuild_history():
            while hist_v.count():
                item = hist_v.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            hist = list(cfg.get("stats", {}).get("session_history", []))
            if not hist:
                hist_v.addWidget(lbl("No sessions yet.", sz=11, col=T3))
                return
            for entry in reversed(hist):
                rounds = int(entry.get("rounds", 0))
                aborts = int(entry.get("aborts", 0))
                attempts = rounds + aborts
                succ = (rounds / attempts * 100) if attempts else 0.0
                hist_v.addWidget(hist_row(
                    lbl(entry.get("date", ""),                        sz=11, col=T2),
                    lbl(str(rounds),                                  sz=11, col=T1, bold=True),
                    lbl(_fmt_time(entry.get("runtime_s", 0)),         sz=11, col=T2),
                    lbl(f"{entry.get('rate_per_hr', 0.0):.1f}",       sz=11, col=T2),
                    lbl(f"{succ:.0f}%",                               sz=11, col=T2),
                ))

        def _refresh_stats():
            s = cfg.get("stats", {}) or {}
            lt_rounds   = int(s.get("lifetime_rounds", 0))
            lt_aborts   = int(s.get("lifetime_aborts", 0))
            lt_clicks   = int(s.get("lifetime_clicks", 0))
            lt_sessions = int(s.get("lifetime_sessions", 0))
            lt_runtime  = float(s.get("lifetime_runtime_s", 0.0))
            best_rate   = float(s.get("best_rate_per_hr", 0.0))
            fastest     = float(s.get("fastest_round_s", 0.0))

            avg_rate     = (lt_rounds / (lt_runtime / 3600)) if lt_runtime > 0 else 0.0
            avg_per_round = (lt_clicks / lt_rounds) if lt_rounds > 0 else 0.0
            attempts     = lt_rounds + lt_aborts
            success      = (lt_rounds / attempts * 100) if attempts else 0.0

            w = self._stats_widgets
            w["lt_rounds"].setText(f"{lt_rounds}")
            w["lt_clicks"].setText(f"{lt_clicks}")
            w["lt_sessions"].setText(f"{lt_sessions}")
            w["lt_runtime"].setText(_fmt_time(lt_runtime))
            w["lt_rate"].setText(f"{avg_rate:.1f}")
            w["lt_avg_per_round"].setText(f"{avg_per_round:.1f}")
            w["lt_best_rate"].setText(f"{best_rate:.1f}")
            w["lt_fastest"].setText(f"{fastest:.1f}s" if fastest > 0 else "—")
            success_color = GREEN if success >= 80 else (AMBER if success >= 50 else RED)
            w["lt_success"].setText(f"{success:.0f}%" if attempts else "—")
            w["lt_success"].setStyleSheet(
                f"color:{success_color}; font-size:22px; font-weight:700; "
                "background:transparent;"
            )

            # Last session
            hist = list(s.get("session_history", []))
            if hist:
                last = hist[-1]
                rounds = int(last.get("rounds", 0))
                aborts = int(last.get("aborts", 0))
                clicks = int(last.get("clicks", 0))
                attempts = rounds + aborts
                succ = (rounds / attempts * 100) if attempts else 0.0
                fr = float(last.get("fastest_round_s", 0.0))
                w["ls_date"].setText(str(last.get("date", "")))
                w["ls_rounds"].setText(str(rounds))
                w["ls_aborts"].setText(str(aborts))
                w["ls_clicks"].setText(str(clicks))
                w["ls_runtime"].setText(_fmt_time(last.get("runtime_s", 0)))
                w["ls_rate"].setText(f"{last.get('rate_per_hr', 0.0):.1f}")
                w["ls_fastest"].setText(f"{fr:.1f}s" if fr > 0 else "—")
                w["ls_success"].setText(f"{succ:.0f}%" if attempts else "—")
            else:
                for k in ("ls_date", "ls_rounds", "ls_aborts", "ls_clicks",
                          "ls_runtime", "ls_rate", "ls_fastest", "ls_success"):
                    w[k].setText("—")

            _rebuild_history()

        def _reset_lifetime():
            s = cfg.setdefault("stats", {})
            s["lifetime_rounds"]    = 0
            s["lifetime_aborts"]    = 0
            s["lifetime_clicks"]    = 0
            s["lifetime_sessions"]  = 0
            s["lifetime_runtime_s"] = 0.0
            s["best_rate_per_hr"]   = 0.0
            s["fastest_round_s"]    = 0.0
            _save(cfg)
            _refresh_stats()

        def _clear_history():
            cfg.setdefault("stats", {})["session_history"] = []
            _save(cfg)
            _refresh_stats()

        rst_btn.clicked.connect(_reset_lifetime)
        clr_btn.clicked.connect(_clear_history)

        timer = QTimer(inner)
        timer.timeout.connect(_refresh_stats)
        timer.start(2000)
        _refresh_stats()
        return inner

    # ══════════════════════════════════════════════════════════════════════════
    # Hotkeys tab — rebind + per-hotkey enable toggle
    # ══════════════════════════════════════════════════════════════════════════

    def _build_hotkeys(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 4, 0, 8)
        root.setSpacing(0)

        for name, label, _abbr in self._HOTKEYS:
            key_str, enabled = _get_hk(name)
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
            badge.clicked.connect(lambda _=None, n=name: self._start_hk_capture(n))
            self._badges[name] = badge
            row.addWidget(badge)
            row.addSpacing(8)

            sw = ToggleSwitch(enabled)
            def _on_enable(v, n=name):
                _set_hk(n, enabled=v)
                self._start_listener()    # rebuild listener mapping
                self._refresh_dashboard()
            sw.toggled.connect(_on_enable)
            row.addWidget(sw)

            root.addLayout(row)
            root.addSpacing(10)

        root.addWidget(sep())
        root.addStretch()
        return page

    # ── pickers ───────────────────────────────────────────────────────────────

    def _do_pick_wait(self):
        if self._active_overlay is not None:
            return
        def cb(point):
            self._active_overlay = None
            if point is None: return
            x, y = point
            cfg["wait_point"] = {"x": int(x), "y": int(y)}
            _save(cfg)
            if self._wait_lbl:
                self._wait_lbl.setText(self._fmt_wait(cfg["wait_point"]))
            self._refresh_dashboard()
        # KEEP a reference on self — the overlay window auto-shows in its
        # __init__, but PyQt will GC it the moment this method returns
        # if nothing is holding the Python proxy.
        self._active_overlay = PointSelectorOverlay(None, cb)

    def _do_pick_region(self):
        if self._active_overlay is not None:
            return
        def cb(rect):
            self._active_overlay = None
            if rect is None: return
            cfg["region"] = {
                "x": int(rect["x"]), "y": int(rect["y"]),
                "width": int(rect["width"]), "height": int(rect["height"]),
            }
            _save(cfg)
            if self._region_lbl:
                self._region_lbl.setText(self._fmt_region(cfg["region"]))
            self._refresh_dashboard()
        self._active_overlay = RegionSelectorOverlay(None, cb)

    def _do_pick_color(self):
        if self._active_overlay is not None:
            return
        def cb(r, g, b):
            self._active_overlay = None
            if r is None: return
            cfg.setdefault("target_colors", []).append(
                {"r": int(r), "g": int(g), "b": int(b)}
            )
            _save(cfg)
            self._refresh_color_list()
            self._refresh_dashboard()
        self._active_overlay = ColorPickerOverlay(None, cb)

    # ── hotkey actions ────────────────────────────────────────────────────────

    def _do_toggle(self):
        going_on = not state.enabled
        state.paused = False
        if going_on:
            self._begin_session()
        else:
            self._end_session()
        _worker.reset_round()
        _bridge.update.emit()

    def _do_pause(self):
        if state.enabled:
            state.paused = not state.paused
            _bridge.update.emit()

    def _do_emergency_stop(self):
        if state.enabled:
            self._end_session()
        else:
            state.enabled = False
            state.paused  = False
        try:
            _kbd.release(KeyCode.from_char(cfg.get("interaction_key", "e")))
        except Exception:
            pass
        _worker.reset_round()
        _bridge.update.emit()

    # ── session lifecycle ─────────────────────────────────────────────────────

    def _begin_session(self):
        state.enabled            = True
        state.session_start      = time.monotonic()
        state.session_started_at = time.time()
        state.rounds_done        = 0
        state.session_aborts     = 0
        state.session_clicks     = 0
        state.fastest_round_s    = None
        state.last_round_s       = None

    def _end_session(self):
        self._commit_session()
        state.enabled            = False
        state.paused             = False
        state.session_start      = None
        state.session_started_at = None

    def _commit_session(self):
        """Persist the current session into stats. No-op for empty sessions."""
        if state.session_start is None:
            return
        runtime = time.monotonic() - state.session_start
        rounds  = int(state.rounds_done)
        aborts  = int(state.session_aborts)
        clicks  = int(state.session_clicks)

        # ignore zero-content sessions (toggled on then off immediately)
        if runtime < 1.0 and rounds == 0 and aborts == 0:
            return

        rate = (rounds / (runtime / 3600)) if runtime > 0 else 0.0
        s = cfg.setdefault("stats", {})
        s["lifetime_rounds"]    = int(s.get("lifetime_rounds",   0)) + rounds
        s["lifetime_aborts"]    = int(s.get("lifetime_aborts",   0)) + aborts
        s["lifetime_clicks"]    = int(s.get("lifetime_clicks",   0)) + clicks
        s["lifetime_sessions"]  = int(s.get("lifetime_sessions", 0)) + 1
        s["lifetime_runtime_s"] = float(s.get("lifetime_runtime_s", 0.0)) + runtime
        if rate > float(s.get("best_rate_per_hr", 0.0)):
            s["best_rate_per_hr"] = rate
        if state.fastest_round_s is not None:
            cur_fastest = float(s.get("fastest_round_s", 0.0))
            if cur_fastest <= 0 or state.fastest_round_s < cur_fastest:
                s["fastest_round_s"] = float(state.fastest_round_s)

        history = list(s.get("session_history", []))
        history.append({
            "date":            time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(state.session_started_at or time.time())),
            "rounds":          rounds,
            "aborts":          aborts,
            "clicks":          clicks,
            "runtime_s":       runtime,
            "rate_per_hr":     rate,
            "fastest_round_s": float(state.fastest_round_s)
                               if state.fastest_round_s is not None else 0.0,
        })
        if len(history) > 50:
            history = history[-50:]
        s["session_history"] = history
        _save(cfg)

    # ── persistent hotkey listener ────────────────────────────────────────────

    def _start_listener(self):
        """(Re)build the pynput listener with the current key→signal mapping."""
        if self._listener is not None:
            try: self._listener.stop()
            except Exception: pass
            self._listener = None

        sig_map = {}
        for name, signal in (
            ("toggle",         _bridge.hk_toggle),
            ("pause",          _bridge.hk_pause),
            ("emergency_stop", _bridge.hk_estop),
            ("select_wait",    _bridge.hk_select_wait),
            ("select_region",  _bridge.hk_select_region),
            ("select_color",   _bridge.hk_select_color),
        ):
            k, en = _get_hk(name)
            if en and k:
                sig_map[k.lower()] = signal

        def _key_str(key) -> str:
            try:
                if hasattr(key, "name") and key.name:
                    return key.name.lower()
                if hasattr(key, "char") and key.char:
                    return key.char.lower()
            except Exception:
                pass
            return ""

        def on_press(key):
            ks = _key_str(key)
            if not ks:
                return
            if self._capturing_for:
                self._apply_rebind(ks)
                return
            sig = sig_map.get(ks)
            if sig:
                sig.emit()

        try:
            self._listener = Listener(on_press=on_press)
            self._listener.daemon = True
            self._listener.start()
        except Exception:
            self._listener = None

    # ── rebind capture ────────────────────────────────────────────────────────

    def _start_hk_capture(self, action: str):
        if self._capturing_for:
            return
        self._capturing_for = action
        badge = self._badges.get(action)
        if badge:
            badge.setText("…")
            badge.setProperty("capturing", True)
            badge.style().unpolish(badge); badge.style().polish(badge)

    def _apply_rebind(self, k: str):
        action = self._capturing_for
        self._capturing_for = None
        if not action:
            return

        # interaction-key rebind (the key the bot itself presses)
        if action == "_interaction_key":
            cfg["interaction_key"] = k
            _save(cfg)
            def apply_ik():
                if self._ik_badge:
                    self._ik_badge.setText(k.upper())
                    self._ik_badge.setProperty("capturing", False)
                    self._ik_badge.style().unpolish(self._ik_badge)
                    self._ik_badge.style().polish(self._ik_badge)
            QTimer.singleShot(0, apply_ik)
            return

        _set_hk(action, key=k)

        def apply():
            badge = self._badges.get(action)
            if badge:
                badge.setText(k.upper())
                badge.setProperty("capturing", False)
                badge.style().unpolish(badge); badge.style().polish(badge)
            self._start_listener()
            self._refresh_dashboard()

        QTimer.singleShot(0, apply)

    def _start_ik_capture(self):
        if self._capturing_for:
            return
        self._capturing_for = "_interaction_key"
        if self._ik_badge:
            self._ik_badge.setText("…")
            self._ik_badge.setProperty("capturing", True)
            self._ik_badge.style().unpolish(self._ik_badge)
            self._ik_badge.style().polish(self._ik_badge)

    # ── tick (focus check + dashboard refresh) ────────────────────────────────

    def _tick(self):
        try:
            state.focused = is_fivem_focused()
        except Exception:
            state.focused = True

    # ── overlay sync ──────────────────────────────────────────────────────────

    def _update_overlay(self):
        ov = getattr(self, "_overlay", None)
        if ov is None:
            return
        enabled = bool(cfg.get("overlay_enabled", False))
        active  = is_plugin_active(self.NAME)

        # Hide when disabled or another plugin's page is showing.
        # Move mode bypasses these checks so the user can drag freely.
        if not ov._move_mode and (not enabled or not active):
            if ov.isVisible():
                ov.hide()
            return

        if not ov._move_mode and not ov.isVisible():
            ov.show()

        if ov._move_mode:
            return  # move mode owns its own display

        # ── status text + colour ──────────────────────────────────────────────
        if not state.enabled:
            ov.set_state("Stopped", T3)
        elif state.paused:
            ov.set_state("Paused", AMBER)
        elif cfg.get("auto_pause_unfocused", True) and not state.focused:
            ov.set_state("Unfocused", AMBER)
        else:
            text, color = _OV_PHASE.get(state.phase, ("Running", GREEN))
            ov.set_state(text, color)

        # ── info rows ─────────────────────────────────────────────────────────
        opts = cfg.get("overlay_opts", {}) or {}
        rt = (time.monotonic() - state.session_start) if state.session_start else 0.0
        rate = (state.rounds_done / (rt / 3600)) if rt > 0 else 0.0

        values = {
            "rounds":     str(state.rounds_done),
            "rate":       f"{rate:.1f} / hr",
            "runtime":    _fmt_time(rt),
            "clicks":     f"{state.clicks_round} this round",
            "last_round": (f"{state.last_round_s:.1f}s"
                           if state.last_round_s is not None else "—"),
            "clusters":   str(state.last_clusters),
        }
        for key, label in _OV_ROWS:
            visible = bool(opts.get(f"show_{key}", False))
            text = f"{label}:  {values[key]}"
            ov.set_row(key, text, visible)

    # ── unload ────────────────────────────────────────────────────────────────

    def on_unload(self):
        if state.enabled:
            self._commit_session()
        state.enabled = False
        state.paused  = False
        state.session_start = None
        state.session_started_at = None
        _worker.stop()
        if self._listener:
            try: self._listener.stop()
            except Exception: pass
            self._listener = None
        try:
            self._overlay_timer.stop()
        except Exception:
            pass
        try:
            self._overlay.hide()
            self._overlay.deleteLater()
        except Exception:
            pass
        for sig in (_bridge.hk_toggle, _bridge.hk_pause, _bridge.hk_estop,
                    _bridge.hk_select_wait, _bridge.hk_select_region,
                    _bridge.hk_select_color, _bridge.update, _bridge.cooldown):
            try: sig.disconnect()
            except Exception: pass
        try:
            _kbd.release(KeyCode.from_char(cfg.get("interaction_key", "e")))
        except Exception:
            pass
