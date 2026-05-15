# Script Hub

A plugin-based automation launcher for games. Download, run, and keep everything up to date from one place.

Originally built for FiveM — as of **v2.0** any plugin can target any game.

---

## Installation

1. Go to [Releases](https://github.com/sittingdown/script-hub/releases/latest) and download `hub.exe`
2. Create a folder anywhere and place `hub.exe` inside it
3. Download the `plugins` folder from this repo and place it next to `hub.exe`

Your folder should look like this:
```
hub.exe
plugins/
  magnet_minigame.py
  needs_test.py
  macro_recorder.py
  ...
```

4. Run `hub.exe`

---

## Plugins

| Plugin | Description |
|--------|-------------|
| **Magnet Minigame** | Auto-clicks the magnet hacking minigame icons. Region + color detection, food/drink breaks, schedules, status overlay. |
| **Macro Recorder** | Record key sequences and replay them on a hotkey. Works in any game. |
| **Key Spammer** | Spams a key sequence on repeat. Auto-pauses when the game loses focus. |

Drop any `.py` plugin into `plugins/` and click **Refresh**. Community plugins go in `plugins/community/` and are prompted for trust on first load.

---

## What's new in v2.0

- **Any game** — plugins declare which game they target; the hub isn't FiveM-only
- **Game profile dropdown** + filter bar to quickly find plugins
- **Hub panic hotkeys** — `Ctrl+Shift+S/P/R` to stop / pause / resume every plugin at once
- **Mini-dashboard overlay** — one always-on-top card showing every plugin's status
- **Per-plugin profiles** + config export / import (right-click a card)
- **Macro Recorder** plugin and a first-run setup wizard

---

## Updating

**Plugins** — click **Update Plugins** on the hub home screen. Downloads the latest plugin files from GitHub and reloads automatically. No restart needed.

**Hub exe** — when a new version is available, a banner appears on the home screen with a download link.

---

## Requirements

- Windows 10 / 11
- The game you're automating running on the same machine

No Python installation needed — everything is bundled in the exe.

---

## Writing your own plugin

Plugins are plain `.py` files dropped into `plugins/`. See **PLUGIN_TEMPLATE.md** for the full guide, or copy `plugins/_template.py` to get started.
