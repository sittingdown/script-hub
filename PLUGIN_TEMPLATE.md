# Plugin Development Guide

Plugins are plain `.py` files dropped into the `plugins/` folder. The hub discovers them automatically — no registration needed. This guide covers everything from a minimal plugin to a full tabbed layout with hotkeys, settings, and background threads.

---

## How the hub loads plugins

- Every `.py` file in `plugins/` that does **not** start with `_` is loaded
- The file must contain a class named exactly `Plugin` that inherits from `PluginBase`
- The hub calls `Plugin()` once, then calls `build_page()` when the card is opened
- Files starting with `_` (like `_template.py`) are ignored — safe to keep as reference

---

## Minimal plugin

```python
from hub_sdk import PluginBase, T3

class Plugin(PluginBase):
    NAME        = "My Tool"
    DESCRIPTION = "Does something useful."
    ACCENT      = "#a78bfa"
    TAGS        = ["FiveM"]

    def build_page(self, nav_back):
        from hub_sdk import StatusLayout
        return StatusLayout(hint="press F9 to toggle")
```

Save as `plugins/my_tool.py`, click **Refresh** in the hub, and the card appears.

---

## PluginBase — full interface

```python
class Plugin(PluginBase):
    # ── class attributes (shown on the hub card) ──────────────────────────────
    NAME        = "My Tool"          # card title
    DESCRIPTION = "Short summary."   # card body text
    ACCENT      = "#a78bfa"          # coloured dot (any hex colour)
    TAGS        = ["FiveM", "Input"] # small chips on the card
    VERSION     = "1.0"              # informational only

    # ── game targeting (any game — not just FiveM) ────────────────────────────
    # Used by focus/auto-pause logic and the "game not running" indicator.
    #   GAME_NAME      → friendly display name shown in the UI
    #   GAME_PROCESSES → list of substrings matched (case-insensitive) against
    #                    the focused window's process name. First hit wins.
    GAME_NAME      = "FiveM"
    GAME_PROCESSES = ["fivem", "gta"]
    # Examples:
    #   Minecraft        → GAME_PROCESSES = ["javaw", "minecraft"]
    #   Rust             → GAME_PROCESSES = ["rustclient"]
    #   Cyberpunk 2077   → GAME_PROCESSES = ["cyberpunk2077"]
    #
    # In your worker / focus code, call:
    #     from bot import is_process_focused, is_process_running
    #     focused = is_process_focused(self.GAME_PROCESSES)
    #     running = is_process_running(self.GAME_PROCESSES)

    # ── optional: hub-card extras ─────────────────────────────────────────────
    # GAME_ICON_PATH  — small image shown next to the plugin name on the hub
    #                   card. Relative paths resolve against plugins/ first,
    #                   then the hub exe directory. Any PIL-readable format.
    # GAME_LAUNCH_CMD — fires when the user clicks the ▶ button on the card.
    #                   String (shell-parsed) OR list (no shell) OR a single
    #                   executable path.
    GAME_ICON_PATH  = "icons/fivem.png"
    GAME_LAUNCH_CMD = "C:/Games/FiveM/FiveM.exe"

    # ── optional: richer GAME_PROCESSES entries ───────────────────────────────
    # In addition to plain substring matchers, each entry can be a dict with
    # ``process`` and/or ``window`` substring fields. Both fields (if set)
    # must match for the entry to be a hit. Useful for distinguishing apps
    # that share a process name (e.g. javaw.exe across many Java games).
    #     GAME_PROCESSES = [
    #         {"process": "javaw", "window": "Minecraft 1."},
    #         "rust",   # plain strings still supported alongside dicts
    #     ]

    # ── optional: required-game gating ────────────────────────────────────────
    # When True, the hub card is dimmed and unclickable while the target game
    # is not running. Useful for plugins that genuinely can't function
    # without the game open.
    GAME_REQUIRED = False

    # ── required for hub Export/Import ────────────────────────────────────────
    # Path to the plugin's single config file, relative to cfg/ (or absolute).
    # Setting this enables the right-click "Export config…" / "Import config…"
    # entries on the plugin's hub card.
    CONFIG_PATH = "myplugin_config.json"

    # ── optional: cross-plugin pubsub ─────────────────────────────────────────
    # Plugins can talk to each other without importing each other directly via
    # the ``pubsub`` singleton in hub_sdk. Topic format is a free string;
    # convention is ``<source>.<event>``.
    #     from hub_sdk import pubsub
    #     pubsub.publish("needs.food_low", {"value": 25})
    #     unsub = pubsub.subscribe("needs.food_low",
    #                              lambda p: print(p["value"]))
    # Callbacks fire on the publisher's thread — route through a pyqtSignal or
    # QTimer.singleShot if you need to touch widgets.

    # ── hub-level panic topics ────────────────────────────────────────────────
    # The hub installs panic hotkeys (default ctrl+shift+s/p/r) that publish:
    #     hub.stop_all   — every plugin should stop
    #     hub.pause_all  — every plugin should pause
    #     hub.resume_all — every plugin should resume

    # ── schedule helper ───────────────────────────────────────────────────────
    # Gate your worker to a daily/weekly window with one call:
    #     from hub_sdk import is_in_window
    #     if not is_in_window("22:00", "04:00", ["fri", "sat"]):
    #         time.sleep(1.0); continue   # overnight Fri/Sat only

    # ── vision plugin SDK (detector.py) ───────────────────────────────────────
    # New backend-pluggable detection:
    #     from detector import Detector, ColorClusterDetector, TemplateMatchDetector
    # ColorClusterDetector wraps the existing build_color_mask + connected
    # components pipeline. TemplateMatchDetector is a slot for an OpenCV /
    # ONNX matcher you supply via match_fn. Subclass Detector to write your
    # own backend.

    # ── community plugin marker ───────────────────────────────────────────────
    # Plugins loaded from plugins/community/ are auto-tagged "Community" and
    # require a one-time trust prompt on first sight. The hub stores approval
    # in cfg/hub_settings.json under "approved_community".

    def build_page(self, nav_back) -> QWidget:
        """
        Required. Return the QWidget shown when the card is opened.
        nav_back() navigates back to the hub — the ← Hub button calls it
        automatically, you don't need to wire it yourself.
        """
        ...

    def status(self) -> tuple[str, str]:
        """
        Optional. Called every 500 ms to update the live dot on the hub card.
        Return (label_text, colour_hex).  Default: ("READY", T3)
        """
        return "READY", T3

    def on_unload(self):
        """
        Optional. Called before the plugin is destroyed on hub Refresh or
        per-plugin Reload. Stop background threads, listeners, and timers here.
        Default: no-op.

        IMPORTANT — on_unload may run on a HALF-CONSTRUCTED instance.
        If your __init__ raises partway through, the hub still calls
        on_unload() so it can tear down whatever __init__ already started
        (threads, listeners, timers). That means every attribute you touch
        here might not exist yet. Always use defensive access:

            listener = getattr(self, "_listener", None)
            if listener is not None:
                listener.stop()

        Wrap each teardown step in its own try/except so one failure
        doesn't skip the rest. If on_unload isn't crash-safe, an errored
        plugin leaks orphan threads that can hard-crash the next refresh.
        """
        pass
```

---

## Page layout options

### Option A — single page (simple plugins)

`StatusLayout` gives you a centred status label and an optional hotkey chip row with zero boilerplate.

```python
def build_page(self, nav_back):
    from hub_sdk import StatusLayout, GREEN, T3
    page = StatusLayout(hint="press F9 to toggle")
    page.set_status("STOPPED", T3)
    return page
```

### Option B — tabbed layout (recommended)

```python
def build_page(self, nav_back):
    from hub_sdk import make_tabs
    container, stack = make_tabs(["Dashboard", "Settings"])
    stack.addWidget(self._build_dashboard())
    stack.addWidget(self._build_settings())
    return container
```

Add as many tabs as you need — just match the label list to the order you add widgets.

---

## Background threads and UI updates

**Never update Qt widgets from a background thread.** Use a bridge object with `pyqtSignal` — Qt automatically delivers emitted signals to the main thread.

```python
from PyQt6.QtCore import pyqtSignal, QObject

class _Bridge(QObject):
    update = pyqtSignal()   # tells the UI to redraw
    toggle = pyqtSignal()   # safely calls toggle() from a poll thread

_bridge = _Bridge()
_bridge.toggle.connect(_worker.toggle)
```

In your UI:
```python
_bridge.update.connect(refresh)   # refresh() reads state and updates labels
```

From any background thread:
```python
_bridge.update.emit()   # safe — Qt queues it to the main thread
_bridge.toggle.emit()   # safe
```

---

## Config pattern

```python
import json
from pathlib import Path

_CFG = Path(__file__).parent.parent / "cfg" / "myplugin_config.json"
_DEF = {"hotkey": "f9", "speed": 1.0}

def _load():
    try:    return {**_DEF, **json.loads(_CFG.read_text())}
    except: return dict(_DEF)

def _save(c):
    try:
        _CFG.parent.mkdir(parents=True, exist_ok=True)
        _CFG.write_text(json.dumps(c, indent=2))
    except: pass

cfg = _load()
```

Read: `cfg.get("speed", 1.0)`  
Write: `cfg["speed"] = 2.0; _save(cfg)`

The `cfg/` folder is created automatically. Name the file after your plugin to avoid collisions with other plugins.

---

## Hotkey poll pattern

Poll `GetAsyncKeyState` in a daemon thread. A generation counter (`_hk_gen`) stops the old thread cleanly when the key is rebound.

```python
import win32api, time, threading

_VK_MAP = {
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "space":0x20,"enter":0x0D,"esc":0x1B,"tab":0x09,
}
def _vk(s):
    s = s.lower()
    return _VK_MAP.get(s, ord(s.upper()) if len(s) == 1 else 0)

# inside Plugin:
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
```

---

## on_unload — clean up on reload

Stop everything here so a hot-reload doesn't leave orphaned threads running in the background.

**`on_unload` must be crash-safe on a half-constructed instance.** If your
`__init__` throws partway through, the hub still calls `on_unload()` to tear
down whatever already started — so attributes set later in `__init__` may not
exist. Use `getattr(...)` and wrap each step in its own `try/except`. A plugin
whose `on_unload` is *not* defensive can leak orphan threads/listeners that
hard-crash the next hub refresh.

```python
def on_unload(self):
    # Bounded, defensive teardown — tolerates missing attributes.
    try: _worker.stop()                       # module-level worker
    except Exception: pass

    listener = getattr(self, "_listener", None)
    if listener is not None:
        self._listener = None
        try: listener.stop()
        except Exception: pass
        try: listener.join(timeout=1.0)       # wait for the Win32 hook to die
        except Exception: pass

    for attr in ("_overlay_timer", "_poll_timer"):
        t = getattr(self, attr, None)
        if t is not None:
            try: t.stop()
            except Exception: pass

    # Detach pubsub subscriptions
    for unsub in getattr(self, "_pubsub_unsubs", []) or []:
        try: unsub()
        except Exception: pass

    # Disconnect bridge signals so a lingering worker can't fire into
    # deleted widgets
    for sig in (_bridge.update, _bridge.toggle):
        try: sig.disconnect()
        except Exception: pass
```

Why the `join(timeout=...)`: `Listener.stop()` only *requests* the Win32 hook
thread to exit. Without joining, the new plugin instance after a refresh
installs its hook alongside the old one — both fire, and disabled hotkeys can
still trigger. Always `stop()` then `join()`.

---

## SDK reference

```python
from hub_sdk import (
    PluginBase,
    # palette
    T1, T2, T3, GREEN, AMBER, BLUE, PURPLE, RED,
    # widget helpers
    lbl, sep, dot, tag, kbd, section_header,
    # layout helpers
    make_tabs, scrollable,
    # interactive widgets
    ToggleSwitch, NumericStepper, ColorSwatch,
    # preset layouts
    StatusLayout, StatBox, DashboardLayout,
    # utilities
    send_discord_notification,
)
```

### Palette

| Name | Hex | Use |
|------|-----|-----|
| `T1` | `#f3f4f6` | Primary text |
| `T2` | `#9ca3af` | Secondary text |
| `T3` | `#4b5563` | Muted / hint text |
| `GREEN` | `#4ade80` | Running / success |
| `AMBER` | `#fbbf24` | Paused / warning |
| `RED` | `#f87171` | Error / stopped |
| `BLUE` | `#60a5fa` | Info |
| `PURPLE` | `#a78bfa` | Accent |

### Widget helpers

| Helper | Returns | Description |
|--------|---------|-------------|
| `lbl(text, sz, col, bold, align, wrap)` | `QLabel` | Styled label |
| `sep()` | `QFrame` | 1 px horizontal divider |
| `dot(color, r)` | `QLabel` | Filled circle |
| `tag(text)` | `QLabel` | Small rounded chip |
| `kbd(key)` | `QLabel` | Keyboard key visual (non-interactive) |
| `section_header(text)` | `QLabel` | Uppercase grey section title |

### Layout helpers

| Helper | Returns | Description |
|--------|---------|-------------|
| `make_tabs(labels)` | `(container, QStackedWidget)` | Tab bar + stacked pages |
| `scrollable(widget)` | `QScrollArea` | Vertical scroll wrapper |

### Interactive widgets

**`ToggleSwitch(checked)`**
```python
sw = ToggleSwitch(cfg.get("enabled", True))
sw.toggled.connect(lambda v: cfg.update({"enabled": v}) or _save(cfg))
```

**`NumericStepper(value, step, min_val, max_val, fmt)`**
```python
st = NumericStepper(1.5, step=0.1, min_val=0.1, max_val=10.0, fmt="{:.1f}")
st.changed.connect(lambda v: ...)
st.set_value(2.0)   # update programmatically
```

**`ColorSwatch(r, g, b)`**
```python
sw = ColorSwatch(255, 100, 50)
sw.deleted.connect(lambda: ...)
```

### Preset layouts

**`StatusLayout(hint)`** — centred status label + optional hotkey row
```python
page = StatusLayout(hint="press F9 to toggle")
page.set_status("RUNNING", GREEN)
badge = page.set_hotkey_row("f9", on_rebind=self._start_capture)
```

**`StatBox(title, rows)`** — dark box with key/value rows
```python
box = StatBox("Session", [("Catches", 42), ("Runtime", "1:23")])
```

**`DashboardLayout()`** — fluent builder for static dashboard pages
```python
page = (DashboardLayout()
    .quick_stats([("Catches", 42), ("Runtime", "1:23")])
    .divider()
    .boxes(StatBox("A", [...]), StatBox("B", [...]))
    .stretch())
```

### Discord notifications

Sends a message to the webhook configured in Hub Settings. Works from any plugin, fire-and-forget.

```python
from hub_sdk import send_discord_notification

send_discord_notification("Bot started!")
send_discord_notification("Caught 50 fish!", title="Milestone")
```

---

## Getting started

1. Copy `plugins/_template.py` → `plugins/my_tool.py`
2. Fill in `NAME`, `DESCRIPTION`, `ACCENT`, `TAGS`
3. Edit `_Worker._run()` with your actual logic
4. Click **Refresh** in the hub — no restart needed

The template has the config pattern, bridge, worker thread, poll thread, hotkey rebind capture, dashboard tab, and settings tab all wired up and ready to customise.
