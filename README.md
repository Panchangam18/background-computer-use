# background-computer-use

An **MCP server + skill library** that lets any MCP-capable LLM client
(Claude Code, Claude Desktop, Cursor, Codex CLI, Goose, Continue, Zed,
etc.) drive other macOS apps **in the background** — without activating
them, bringing them to the front, or moving your cursor.

Two things ship here:

1. **MCP server** (`server/cua_server.py`) — exposes nine tools
   (`list_apps`, `get_app_state`, `click`, `drag`,
   `perform_secondary_action`, `press_key`, `scroll`, `set_value`,
   `type_text`) wrapping the macOS Accessibility, CGEvent, and
   `CGWindowList` APIs. Pure stdio MCP — any compliant client works.
2. **Skill library** (`skills/`) — markdown files with YAML
   frontmatter that teach the model the one-snapshot-per-turn loop
   and app-specific heuristics for Safari, Chrome, Clock, Numbers,
   Apple Music, Spotify, and Notion. Any skill-aware client auto-
   loads them; for clients without native skill support, you can
   read the files directly into a system prompt.

It started as a parity port of the Codex desktop's built-in computer-
use tool surface, with a few capabilities the original didn't have:
animated scroll, Chrome scrolling via AppleScript JS, Electron
accessibility auto-enable, and strict refusal to silently steal focus.

## Status

Works on macOS 15, should work on 13+. The tool surface is stable; the
per-app hint files grow as new quirks are found. See
[`docs/APPS.md`](docs/APPS.md) for the app-by-app coverage matrix.

## Requirements

- macOS 13 or newer (15+ recommended).
- Python 3.10+ available as `python3`.
- **Accessibility** and **Screen Recording** permissions granted to
  the process that runs your MCP client (your terminal if you're
  using a CLI client, the desktop app's bundle if you're using
  Claude Desktop / Cursor, etc.). System Settings → Privacy &
  Security.

## Install

### Any MCP client (generic)

Clone this repo, then point your client's MCP configuration at
`bin/cua-server`. The launcher provisions a virtualenv on first run
and execs the stdio server.

Example MCP config snippet (same shape used by Claude Desktop,
Cursor, Claude Code, Goose, etc.):

```json
{
  "mcpServers": {
    "background-computer-use": {
      "command": "/absolute/path/to/background-computer-use/bin/cua-server",
      "env": {
        "CUA_PLUGIN_ROOT": "/absolute/path/to/background-computer-use"
      }
    }
  }
}
```

Your client's specific config file locations:

| Client        | Config path                                       |
|---------------|---------------------------------------------------|
| Claude Code   | project `.mcp.json` or `~/.claude.json`           |
| Claude Desktop| `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor        | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (user) |
| Goose         | `~/.config/goose/config.yaml` (different syntax)  |

After restart, the nine tools should appear in your client's tool
list. Loading the skills is client-specific — see
[`docs/SKILLS.md`](docs/SKILLS.md).

### As a Claude Code plugin

This repo also ships a Claude Code plugin manifest so you can install
it via Claude Code's marketplace system:

```text
/plugin marketplace add Panchangam18/background-computer-use
/plugin install background-computer-use@background-computer-use
```

The `.claude-plugin/` directory exists only to enable this path; if
you're using a different MCP client, you can ignore it.

### Dev mode (Claude Code)

```bash
git clone https://github.com/Panchangam18/background-computer-use.git
cd background-computer-use
claude --plugin-dir .
```

The first run takes ~15 seconds while the launcher provisions a
virtualenv under `.venv/` with PyObjC, Pillow, and the MCP SDK.
Subsequent runs reuse it.

See [`docs/DEVELOPING.md`](docs/DEVELOPING.md) for environment
variables, the ghost-cursor playground, and troubleshooting.

## Quickstart

With the server loaded in your MCP client:

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
| `click(app, element_index=… \| x/y=…)` | Click an AX element or raw coordinate. Uses `AXPress` when advertised; falls back to AppleScript, then `CGEventPostToPid`. |
| `drag(app, from_x, from_y, to_x, to_y)` | Smooth ~20-step drag via `CGEventPostToPid`. |
| `perform_secondary_action(app, element_index, action)` | Invoke a named AX action (`Pick`, `Increment`, `ScrollToVisible`, etc.). Refuses focus-stealing actions (`ShowMenu`, `ShowAlternateUI`, `Raise`) unless `allow_foreground_activation=True`. |
| `press_key(app, key, element_index=…)` | xdotool-style keys (`Return`, `cmd+l`, `Page_Down`, `shift+command+t`). Can pre-focus a specific AX element. |
| `scroll(app, element_index, direction, pages=1, smooth=True)` | Smooth, multi-page scroll. Tries AX `AXScroll*ByPage`, then scroll-bar `AXValue`, then Chromium AppleScript JS, then pixel wheel events. Refuses to silently steal focus. |
| `set_value(app, element_index, value, submit=True)` | Set an AX value. Auto-falls-back to a `cmd+a` → `type_text` → `Return` typing sequence for Safari's URL bar and other sticky text fields. |
| `type_text(app, text, element_index=…)` | Freeform Unicode typing via synthetic keyboard events. |

## Principles

Four things this library is opinionated about:

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
   renderer opt-in) are documented in per-app markdown files that
   ship with the plugin and get injected at tool-call time.

## Repository layout

```text
.
├── .claude-plugin/          # Claude Code plugin shim (optional)
│   ├── plugin.json
│   └── marketplace.json
├── .mcp.json                # Claude Code per-project MCP config
├── bin/
│   └── cua-server           # Bash launcher: provisions .venv/ and execs the server
├── server/
│   ├── cua_server.py        # Main FastMCP stdio server (~2,200 LoC)
│   ├── cursor_ghost.py      # Ghost-cursor overlay daemon
│   ├── cursor_paths.py      # Bezier path generator for the ghost
│   ├── cursor_playground.py # Dev playground for the ghost
│   ├── requirements.txt
│   └── app-hints/           # Per-app <app_hints> payloads (server-private)
├── skills/                  # Skill library (any skill-aware MCP client)
│   ├── computer-use/        # Main operating-loop skill
│   ├── safari/
│   ├── chrome/
│   ├── clock/
│   ├── numbers/
│   ├── apple-music/
│   ├── spotify/
│   └── notion/
└── docs/
    ├── DESIGN.md            # Reverse-engineering notes + design rationale
    ├── APPS.md              # App-by-app coverage matrix and limitations
    ├── SKILLS.md            # How to use the skill library with each MCP client
    └── DEVELOPING.md        # Local-dev loop, env vars, troubleshooting
```

## License

MIT. See [`LICENSE`](LICENSE).
