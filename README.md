# background-computer-use

A Claude Code plugin that lets Claude drive other macOS apps **in the
background** — without activating them, bringing them to the front, or
moving your cursor.

Under the hood it's:

- **An MCP server** (`server/cua_server.py`) that exposes nine tools
  (`list_apps`, `get_app_state`, `click`, `drag`,
  `perform_secondary_action`, `press_key`, `scroll`, `set_value`,
  `type_text`) wrapping the macOS Accessibility, CGEvent, and
  `CGWindowList` APIs.
- **Skills** under `skills/` that teach Claude the one-snapshot-per-turn
  loop and give per-app heuristics for Safari, Chrome, Clock, Numbers,
  Apple Music, Spotify, and Notion.

It's a parity port of the Codex desktop's built-in computer-use tools,
with a few extra improvements the original doesn't have (smooth
animated scroll, Chrome scrolling via AppleScript JS, Electron
accessibility auto-enable, and an explicit refusal to silently steal
focus for menu-opening actions).

## Status

Works on macOS 15, should work on 13+. The tool surface is stable; the
per-app hint files grow as I hit new quirks. See
[`docs/APPS.md`](docs/APPS.md) for the app-by-app coverage matrix.

## Install

### Requirements

- macOS 13 or newer (15+ recommended).
- Python 3.10+ available as `python3`.
- Claude Code installed and authenticated.
- **Accessibility** and **Screen Recording** permissions granted to the
  process that runs Claude Code (your terminal, the Claude Code app,
  Cursor, etc.). System Settings → Privacy & Security.

### As a plugin marketplace

```text
/plugin marketplace add madhavan/background-computer-use
/plugin install background-computer-use@background-computer-use
```

Or, if you've cloned the repo locally:

```text
/plugin marketplace add /path/to/background-computer-use
/plugin install background-computer-use@background-computer-use
```

### As a dev-mode plugin

Skip the marketplace and point Claude Code at this directory directly:

```bash
git clone https://github.com/madhavan/background-computer-use.git
cd background-computer-use
claude --plugin-dir .
```

The first run takes ~15 seconds while the launcher provisions a
virtualenv under `.venv/` with PyObjC, Pillow, and the MCP SDK.
Subsequent runs reuse it.

See [`docs/DEVELOPING.md`](docs/DEVELOPING.md) for environment
variables, the ghost-cursor playground, and troubleshooting.

## Quickstart

Inside a Claude Code session:

```text
> What's on my Safari start page?
> Open https://en.wikipedia.org/wiki/Octopus in Safari.
> Scroll down to "Locomotion" in that article.
> Add 2+2 in Calculator and tell me what it says.
> Switch to the Dolphin Wikipedia tab.
```

All of the above happen without activating the target app. Your
cursor stays where it is.

## Tool surface

| Tool | Purpose |
|------|---------|
| `list_apps` | Discover running apps (display name + bundle id). |
| `get_app_state(app)` | Return the key window's accessibility tree, a window-scoped screenshot, and per-app `<app_hints>` (if any). Call this once per turn before interacting with an app. |
| `click(app, element_index=... \| x/y=...)` | Click an AX element or raw coordinate. Uses `AXPress` when advertised; falls back to AppleScript, then `CGEventPostToPid`. |
| `drag(app, from_x, from_y, to_x, to_y)` | Smooth ~20-step drag via `CGEventPostToPid`. |
| `perform_secondary_action(app, element_index, action)` | Invoke a named AX action (`Pick`, `Increment`, `ScrollToVisible`, etc.). Refuses focus-stealing actions (`ShowMenu`, `ShowAlternateUI`, `Raise`) unless `allow_foreground_activation=True`. |
| `press_key(app, key, element_index=...)` | xdotool-style keys (`Return`, `cmd+l`, `Page_Down`, `shift+command+t`). Can pre-focus a specific AX element. |
| `scroll(app, element_index, direction, pages=1, smooth=True)` | Smooth, multi-page scroll. Tries AX `AXScroll*ByPage`, then scroll-bar `AXValue`, then pixel wheel events, then Chromium AppleScript JS. Refuses to silently steal focus. |
| `set_value(app, element_index, value, submit=True)` | Set an AX value. Auto-falls-back to a `cmd+a` → `type_text` → `Return` typing sequence for Safari's URL bar and other sticky text fields. |
| `type_text(app, text, element_index=...)` | Freeform Unicode typing via synthetic keyboard events. |

## Principles

This plugin is opinionated about four things:

1. **Background-safe by default.** No tool silently brings the target
   app to the foreground. When that isn't possible, the tool errors
   out and makes the caller opt in explicitly.
2. **Accessibility first.** The tool surface prefers AX primitives
   (AXPress, AXValue, AXScroll*ByPage) over coordinate-based event
   synthesis. Coordinate fallbacks exist, but they're the second
   choice.
3. **One snapshot per turn.** Element indexes are snapshot-local.
   `get_app_state` is cheap; call it after any UI-changing action.
4. **Per-app knowledge belongs in skills and `<app_hints>`, not in the
   tool implementation.** The server is a thin wrapper around the OS;
   app-specific quirks (Safari's URL bar, Chrome's scroll, Electron's
   renderer opt-in) are documented in per-app markdown files that ship
   with the plugin.

## Repository layout

```text
.
├── .claude-plugin/
│   ├── plugin.json         # Plugin manifest
│   └── marketplace.json    # Lets `/plugin marketplace add` work on this repo
├── .mcp.json               # Registers the MCP server
├── bin/
│   └── cua-server          # Bash launcher that provisions .venv/ and execs the server
├── server/
│   ├── cua_server.py       # Main FastMCP server (2,200-ish lines)
│   ├── cursor_ghost.py     # Ghost-cursor animation daemon
│   ├── cursor_paths.py     # Bezier path generator
│   ├── cursor_playground.py
│   ├── requirements.txt
│   └── app-hints/          # Per-app <app_hints> payloads
├── skills/
│   ├── computer-use/       # Main operating-loop skill
│   ├── safari/
│   ├── chrome/
│   ├── clock/
│   ├── numbers/
│   ├── apple-music/
│   ├── spotify/
│   └── notion/
└── docs/
    ├── DESIGN.md           # Reverse-engineering notes + design rationale
    ├── APPS.md             # App-by-app coverage matrix and limitations
    └── DEVELOPING.md       # Local-dev loop, env vars, troubleshooting
```

## License

MIT. See [`LICENSE`](LICENSE).
