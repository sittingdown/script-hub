"""
Macro Recorder  —  game-agnostic plugin

Records keyboard key sequences (with timing) and plays them back on a hotkey.
Lets you build named macros, save / delete / rename them, and trigger them
without touching the mouse.

Hotkeys (rebindable in Hotkeys tab):
    F2 — start / stop recording the currently-selected macro
    F3 — play the currently-selected macro
    F8 — toggle this plugin (panic switch — disables all macro hotkeys)

Storage:  cfg/macro_recorder_config.json
"""
import json
import sys
import time
import threading
from pathlib import Path

from pynput.keyboard import Listener, Controller, Key, KeyCode
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QListWidgetItem, QLineEdit,
)

# project root on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hub_sdk import (
    PluginBase,
    T1, T2, T3, GREEN, AMBER, RED, BLUE,
    lbl, sep, kbd, section_header,
    make_tabs, scrollable,
    NumericStepper, ToggleSwitch,
    pubsub,
    is_plugin_active,
)

# Gate hotkeys + playback to this plugin's page
_PLUGIN_NAME = "Macro Recorder"

# ── config ────────────────────────────────────────────────────────────────────

_CFG = _ROOT / "cfg" / "macro_recorder_config.json"
_DEF = {
    "selected":     "",       # name of the active macro
    "macros":       {},       # name → {events: [...], created: iso}
    "playback_speed": 1.0,
    "hotkeys": {
        "toggle":  {"key": "f8", "enabled": True},
        "record":  {"key": "f2", "enabled": True},
        "play":    {"key": "f3", "enabled": True},
    },
}

def _migrate(loaded: dict) -> dict:
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
            for sk, sv in v.items():
                out[k][sk] = sv
        else:
            out[k] = v
    return out

def _load() -> dict:
    try:    return _merge_defaults(json.loads(_CFG.read_text()))
    except: return json.loads(json.dumps(_DEF))

def _save(c: dict):
    try:
        _CFG.parent.mkdir(parents=True, exist_ok=True)
        _CFG.write_text(json.dumps(c, indent=2))
    except Exception:
        pass

cfg = _load()

def _get_hk(name: str) -> tuple[str, bool]:
    h = cfg.get("hotkeys", {}).get(name) or {}
    if isinstance(h, str): return h, True
    return h.get("key", ""), bool(h.get("enabled", True))

def _set_hk(name: str, *, key=None, enabled=None):
    cfg.setdefault("hotkeys", {}).setdefault(name, {"key": "", "enabled": True})
    if key is not None: cfg["hotkeys"][name]["key"] = key
    if enabled is not None: cfg["hotkeys"][name]["enabled"] = bool(enabled)
    _save(cfg)

# ── input / serialisation helpers ─────────────────────────────────────────────

_kbd = Controller()

def _key_token(key) -> str:
    """Stable string for a pynput key: 'a', 'f3', 'space', ..."""
    try:
        if hasattr(key, "name") and key.name:
            return key.name.lower()
        if hasattr(key, "char") and key.char:
            return key.char.lower()
    except Exception:
        pass
    return ""

def _token_to_key(tok: str):
    """Reverse: token → pynput Key | KeyCode for playback."""
    if not tok:
        return None
    if tok in Key.__members__:
        return Key[tok]
    if len(tok) == 1:
        return KeyCode.from_char(tok)
    # Some special-case names that pynput exposes via Key
    aliases = {
        "ctrl_l": Key.ctrl_l, "ctrl_r": Key.ctrl_r,
        "shift_l": Key.shift_l, "shift_r": Key.shift_r,
        "alt_l": Key.alt_l, "alt_r": Key.alt_r,
    }
    if tok in aliases:
        return aliases[tok]
    return None

# ── bridge / state ────────────────────────────────────────────────────────────

class _Bridge(QObject):
    update      = pyqtSignal()
    hk_toggle   = pyqtSignal()
    hk_record   = pyqtSignal()
    hk_play     = pyqtSignal()

_bridge = _Bridge()

class _State:
    enabled    = True       # plugin master (panic) toggle
    recording  = False
    playing    = False
    play_started_at = None

state = _State()

# ── recorder thread ───────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self._listener  = None
        self._events: list = []
        self._t0       = 0.0

    def start(self):
        if self._listener is not None:
            return
        self._events = []
        self._t0    = time.monotonic()
        def on_press(key):
            tok = _key_token(key)
            if not tok: return
            self._events.append({"t": time.monotonic() - self._t0,
                                 "type": "down", "key": tok})
        def on_release(key):
            tok = _key_token(key)
            if not tok: return
            self._events.append({"t": time.monotonic() - self._t0,
                                 "type": "up", "key": tok})
        self._listener = Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> list:
        if self._listener is None:
            return []
        try: self._listener.stop()
        except Exception: pass
        try: self._listener.join(timeout=0.5)
        except Exception: pass
        self._listener = None
        return list(self._events)

_recorder = _Recorder()

# ── playback ──────────────────────────────────────────────────────────────────

def _play_events(events: list, speed: float, abort_flag: list):
    """Replay recorded events on a worker thread. ``abort_flag[0]`` flips
    True to cancel playback mid-flight; navigating away from the plugin
    page also aborts (so a half-played macro doesn't keep typing into
    whatever the user has focused now)."""
    if not events:
        return
    speed = max(0.1, float(speed))
    t0 = time.monotonic()
    base_t = events[0]["t"]
    held: set = set()
    try:
        for ev in events:
            if abort_flag[0] or not is_plugin_active(_PLUGIN_NAME):
                break
            tgt = (ev["t"] - base_t) / speed
            while not abort_flag[0] and is_plugin_active(_PLUGIN_NAME):
                elapsed = time.monotonic() - t0
                if elapsed >= tgt:
                    break
                time.sleep(min(0.01, tgt - elapsed))
            k = _token_to_key(ev["key"])
            if k is None:
                continue
            try:
                if ev["type"] == "down":
                    _kbd.press(k);   held.add(ev["key"])
                else:
                    _kbd.release(k); held.discard(ev["key"])
            except Exception:
                pass
    finally:
        # Release any keys still held (safety)
        for tok in list(held):
            try:
                k = _token_to_key(tok)
                if k is not None: _kbd.release(k)
            except Exception: pass

# ── UI helpers ────────────────────────────────────────────────────────────────

def _hrow(*widgets):
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0); h.setSpacing(10)
    for i, w in enumerate(widgets):
        if isinstance(w, QWidget): h.addWidget(w)
        else: h.addLayout(w)
        if i == 0: h.addStretch()
    return h

# ── Plugin ────────────────────────────────────────────────────────────────────

class Plugin(PluginBase):
    NAME        = "Macro Recorder"
    DESCRIPTION = "Record key sequences and play them back on a hotkey."
    ACCENT      = "#a78bfa"
    TAGS        = ["Macros", "Input"]
    VERSION     = "1.0"

    GAME_NAME      = ""              # game-agnostic — no targeting
    GAME_PROCESSES = []
    CONFIG_PATH    = "macro_recorder_config.json"

    _HOTKEYS = [
        ("toggle", "Toggle plugin"),
        ("record", "Record start / stop"),
        ("play",   "Play selected"),
    ]

    def __init__(self):
        self._listener        = None
        self._capturing_for   = None
        self._badges          = {}
        self._play_abort      = [False]
        self._play_thread     = None
        # widgets
        self._lst             = None
        self._name_edit       = None
        self._status_lbl      = None
        self._meta_lbl        = None

        # bridge wiring (guarded by enable check)
        _bridge.hk_toggle.connect(self._guarded("toggle", self._do_toggle))
        _bridge.hk_record.connect(self._guarded("record", self._do_record))
        _bridge.hk_play.connect  (self._guarded("play",   self._do_play))
        _bridge.update.connect(self._refresh)

        # cross-plugin panic
        self._pubsub_unsubs = [
            pubsub.subscribe("hub.stop_all",   lambda _p: QTimer.singleShot(
                0, self._do_stop_everything)),
        ]
        self._start_listener()

    # ── card status ──────────────────────────────────────────────────────────
    def status(self):
        if not state.enabled:
            return "DISABLED", T3
        if state.recording:
            return "RECORDING", RED
        if state.playing:
            return "PLAYING", BLUE
        n = len(cfg.get("macros", {}) or {})
        return f"{n} macro{'s' if n != 1 else ''}", GREEN if n else T3

    # ── page ─────────────────────────────────────────────────────────────────
    def build_page(self, nav_back):
        container, stack = make_tabs(["Macros", "Hotkeys"])
        stack.addWidget(self._build_macros())
        stack.addWidget(scrollable(self._build_hotkeys()))
        self._refresh()
        return container

    # ── macros page ──────────────────────────────────────────────────────────
    def _build_macros(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(16, 16, 16, 16); root.setSpacing(0)

        # status bar
        self._status_lbl = lbl("Idle", sz=14, col=T1, bold=True,
                               align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_lbl)
        self._meta_lbl = lbl("", sz=10, col=T3,
                             align=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._meta_lbl)
        root.addSpacing(14)

        # action row
        action_row = QHBoxLayout(); action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        rec_btn  = QPushButton("Record")
        play_btn = QPushButton("Play")
        del_btn  = QPushButton("Delete")
        for b in (rec_btn, play_btn, del_btn):
            b.setObjectName("action")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        rec_btn.clicked.connect(self._do_record)
        play_btn.clicked.connect(self._do_play)
        del_btn.clicked.connect(self._do_delete)
        action_row.addWidget(rec_btn)
        action_row.addWidget(play_btn)
        action_row.addStretch()
        action_row.addWidget(del_btn)
        root.addLayout(action_row)
        root.addSpacing(12)

        # name + save row
        self._name_edit = QLineEdit(cfg.get("selected", ""))
        self._name_edit.setPlaceholderText("Macro name (e.g. open-inventory)")
        self._name_edit.setStyleSheet(
            "QLineEdit { background:#0f0f0f; border:1px solid #2a2a2a;"
            " border-radius:5px; padding:6px 8px; color:#e5e7eb; }"
            "QLineEdit:focus { border-color:#3a3a3a; }"
        )
        root.addWidget(self._name_edit)
        root.addSpacing(10)

        # macro list
        self._lst = QListWidget()
        self._lst.setStyleSheet(
            "QListWidget { background:#0f0f0f; border:1px solid #1e1e1e;"
            " border-radius:6px; color:#e5e7eb; padding:4px; }"
            "QListWidget::item { padding:6px 8px; border-radius:4px; }"
            "QListWidget::item:selected { background:#2a2a2a; }"
        )
        self._lst.itemSelectionChanged.connect(self._on_select)
        root.addWidget(self._lst, 1)
        root.addSpacing(10)

        # playback speed
        sp = NumericStepper(float(cfg.get("playback_speed", 1.0)),
                            step=0.1, min_val=0.1, max_val=10.0, fmt="{:.1f}")
        sp.setToolTip("Playback speed multiplier (1.0 = real-time)")
        sp.changed.connect(
            lambda v: (cfg.update({"playback_speed": float(v)}), _save(cfg)))
        root.addLayout(_hrow(lbl("Playback speed ×", sz=12, col=T2), sp))

        return page

    def _on_select(self):
        items = self._lst.selectedItems() if self._lst else []
        if items:
            n = items[0].text()
            cfg["selected"] = n
            _save(cfg)
            if self._name_edit:
                self._name_edit.setText(n)
            self._refresh()

    # ── hotkeys page ─────────────────────────────────────────────────────────
    def _build_hotkeys(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 4, 16, 8); root.setSpacing(0)

        for name, label in self._HOTKEYS:
            key_str, enabled = _get_hk(name)
            root.addWidget(sep()); root.addSpacing(10)
            row = QHBoxLayout(); row.setSpacing(10)
            row.addWidget(lbl(label, sz=12, col=T2))
            row.addStretch()
            badge = QPushButton(key_str.upper() or "—")
            badge.setObjectName("badge")
            badge.setCursor(Qt.CursorShape.PointingHandCursor)
            badge.setToolTip("Click to rebind")
            badge.setProperty("capturing", False)
            badge.clicked.connect(lambda _=None, n=name: self._start_capture(n))
            self._badges[name] = badge
            row.addWidget(badge); row.addSpacing(8)

            sw = ToggleSwitch(enabled)
            def _on_en(v, n=name):
                _set_hk(n, enabled=v)
                self._start_listener()
            sw.toggled.connect(_on_en)
            row.addWidget(sw)
            root.addLayout(row); root.addSpacing(10)
        root.addWidget(sep())
        root.addStretch()
        return page

    # ── hotkey actions ───────────────────────────────────────────────────────
    def _do_toggle(self):
        state.enabled = not state.enabled
        if not state.enabled:
            self._do_stop_everything()
        _bridge.update.emit()

    def _do_stop_everything(self):
        # Cancel any in-flight recording or playback
        if state.recording:
            _recorder.stop()
            state.recording = False
        if state.playing:
            self._play_abort[0] = True
            state.playing = False
        _bridge.update.emit()

    def _do_record(self):
        if state.playing:
            return
        if state.recording:
            events = _recorder.stop()
            state.recording = False
            name = (self._name_edit.text().strip() if self._name_edit
                    else cfg.get("selected", "")).strip()
            if not name:
                name = time.strftime("macro-%H%M%S")
            cfg.setdefault("macros", {})[name] = {
                "events": events,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            cfg["selected"] = name
            _save(cfg)
        else:
            state.recording = True
            _recorder.start()
        _bridge.update.emit()

    def _do_play(self):
        if state.recording or state.playing:
            return
        name = (self._name_edit.text().strip() if self._name_edit
                else cfg.get("selected", "")).strip()
        macro = (cfg.get("macros", {}) or {}).get(name)
        if not macro or not macro.get("events"):
            return
        speed = float(cfg.get("playback_speed", 1.0))
        self._play_abort = [False]
        state.playing = True
        state.play_started_at = time.monotonic()
        _bridge.update.emit()

        def _runner(evs=macro["events"], sp=speed, ab=self._play_abort):
            try:
                _play_events(evs, sp, ab)
            finally:
                state.playing = False
                _bridge.update.emit()

        self._play_thread = threading.Thread(target=_runner, daemon=True)
        self._play_thread.start()

    def _do_delete(self):
        name = (self._name_edit.text().strip() if self._name_edit
                else cfg.get("selected", "")).strip()
        if not name:
            return
        macros = cfg.get("macros", {}) or {}
        if name in macros:
            del macros[name]
            cfg["macros"] = macros
            if cfg.get("selected") == name:
                cfg["selected"] = ""
            _save(cfg)
            _bridge.update.emit()

    # ── refresh ──────────────────────────────────────────────────────────────
    def _refresh(self):
        if self._lst is None:
            return
        # Rebuild list keeping selection if possible
        selected = (cfg.get("selected", "") or
                    (self._name_edit.text().strip() if self._name_edit else ""))
        self._lst.blockSignals(True)
        self._lst.clear()
        for name in sorted((cfg.get("macros", {}) or {}).keys()):
            item = QListWidgetItem(name)
            self._lst.addItem(item)
            if name == selected:
                self._lst.setCurrentItem(item)
        self._lst.blockSignals(False)

        if self._status_lbl:
            if not state.enabled:
                self._status_lbl.setText("Disabled")
                self._status_lbl.setStyleSheet(
                    f"color:{T3}; font-size:14px; font-weight:700; background:transparent;")
            elif state.recording:
                self._status_lbl.setText("Recording…")
                self._status_lbl.setStyleSheet(
                    f"color:{RED}; font-size:14px; font-weight:700; background:transparent;")
            elif state.playing:
                self._status_lbl.setText("Playing…")
                self._status_lbl.setStyleSheet(
                    f"color:{BLUE}; font-size:14px; font-weight:700; background:transparent;")
            else:
                self._status_lbl.setText("Idle")
                self._status_lbl.setStyleSheet(
                    f"color:{T1}; font-size:14px; font-weight:700; background:transparent;")

        if self._meta_lbl:
            macro = (cfg.get("macros", {}) or {}).get(selected) or {}
            n = len(macro.get("events", []))
            self._meta_lbl.setText(
                f"{selected or '(unselected)'}  ·  {n} event(s)")

    # ── enable-state guard wrapper ───────────────────────────────────────────
    def _guarded(self, hk_name: str, action):
        def runner(*_a, **_kw):
            if not state.enabled:
                return
            if not is_plugin_active(_PLUGIN_NAME):
                return
            _, en = _get_hk(hk_name)
            if en:
                action()
        return runner

    # ── persistent listener ──────────────────────────────────────────────────
    def _start_listener(self):
        if self._listener is not None:
            old = self._listener
            self._listener = None
            try: old.stop()
            except Exception: pass
            try: old.join(timeout=1.0)
            except Exception: pass

        sig_map = {}
        for name, signal in (
            ("toggle", _bridge.hk_toggle),
            ("record", _bridge.hk_record),
            ("play",   _bridge.hk_play),
        ):
            k, en = _get_hk(name)
            if en and k:
                sig_map[k.lower()] = signal

        def on_press(key):
            tok = _key_token(key)
            if not tok:
                return
            # Rebind capture wins regardless of page
            if self._capturing_for:
                self._apply_rebind(tok)
                return
            # IMPORTANT: while recording, we DO want to receive every key —
            # the recorder thread captures them. We still suppress the
            # plugin-hotkey fire during recording so e.g. F3 (play) inside
            # a macro doesn't accidentally start a playback. So the active-
            # page guard is only checked on the hotkey-fire path.
            sig = sig_map.get(tok)
            if sig is None:
                return
            if state.recording:
                return
            if not is_plugin_active(_PLUGIN_NAME):
                return
            sig.emit()

        try:
            self._listener = Listener(on_press=on_press)
            self._listener.daemon = True
            self._listener.start()
        except Exception:
            self._listener = None

    # ── rebind capture ───────────────────────────────────────────────────────
    def _start_capture(self, action: str):
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
        _set_hk(action, key=k)
        def apply():
            badge = self._badges.get(action)
            if badge:
                badge.setText(k.upper())
                badge.setProperty("capturing", False)
                badge.style().unpolish(badge); badge.style().polish(badge)
            self._start_listener()
        QTimer.singleShot(0, apply)

    # ── unload ───────────────────────────────────────────────────────────────
    def on_unload(self):
        # May run on a half-constructed instance — tolerate missing attrs.
        try: self._do_stop_everything()
        except Exception: pass
        listener = getattr(self, "_listener", None)
        if listener is not None:
            self._listener = None
            try: listener.stop()
            except Exception: pass
            try: listener.join(timeout=1.0)
            except Exception: pass
        for unsub in getattr(self, "_pubsub_unsubs", []) or []:
            try: unsub()
            except Exception: pass
        self._pubsub_unsubs = []
        for sig in (_bridge.hk_toggle, _bridge.hk_record,
                    _bridge.hk_play, _bridge.update):
            try: sig.disconnect()
            except Exception: pass
