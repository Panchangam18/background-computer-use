# Developing locally

This repo ships an MCP server and a skill library. This doc covers the
local-development loop, permissions setup, and troubleshooting.
User-facing install instructions for every MCP client are in the
[top-level README](../README.md).

## Requirements

- macOS 13+ (tested target is 15+; older may work but is unsupported).
- Python 3.10+ on your `$PATH` as `python3`, or set `CUA_PYTHON` to a
  full path.
- An MCP-capable LLM client (any of Claude Code, Claude Desktop,
  Cursor, Codex CLI, Goose, etc.).
- Two macOS permissions for the **process that runs your MCP
  client**:
  - **Accessibility** — System Settings → Privacy & Security → Accessibility
  - **Screen Recording** — System Settings → Privacy & Security → Screen Recording

Without both permissions the server will start, but `get_app_state`
will return an empty/error-like tree and screenshots will be blank.

## Iterate against a working copy

Fastest feedback loop: point your MCP client at the local path and
restart the client to pick up server-code changes.

**Claude Code**

```bash
claude --plugin-dir .
```

Run `/reload-plugins` after editing skill or manifest files. For
Python server changes, exit and relaunch Claude Code (or kill the MCP
server process and let Claude Code respawn it).

**Other clients**: edit your client's MCP config to point its
`command` entry at `/absolute/path/to/this/repo/bin/cua-server`, then
restart the client.

On first run the launcher (`bin/cua-server`) creates a venv under
`.venv/` and installs `server/requirements.txt` (MCP SDK, PyObjC
bindings, Pillow). Subsequent runs reuse the venv.

## Smoke-test inside the client

```text
> list_apps
> get_app_state app="Finder"
> click app="Finder" element_index=0
```

## Smoke-test outside any client (MCP Inspector)

```bash
npx @modelcontextprotocol/inspector ./bin/cua-server
```

A local web UI opens and you can drive the tools interactively
without attaching a model.

## Environment variables

The launcher and server read:

- `CUA_PYTHON` — path to the Python interpreter (default `python3`).
- `CUA_PLUGIN_ROOT` — overrides the plugin root; normally set for you
  by `bin/cua-server` (which falls back to the directory containing
  the launcher if neither `CUA_PLUGIN_ROOT` nor `CLAUDE_PLUGIN_ROOT`
  is set).
- `CUA_LOG_LEVEL` — `DEBUG`/`INFO`/`WARNING` (default `INFO`). Server
  logs go to stderr and land in the client's MCP log buffer.
- `CUA_MAX_ELEMENTS` — hard cap on snapshot elements (default `1200`).
- `CUA_MAX_DEPTH` — accessibility tree depth cap (default `30`).
- `CUA_SCREENSHOT_MAX_EDGE` — downscale screenshots so their longest
  edge is at most this many pixels (default `1600`).
- `CUA_CLICK_OVERLAY` — set to `0`/`false` to disable the ghost-cursor
  visualization that runs during tool calls (default: enabled).
- `CUA_CURSOR` — which cursor image the ghost uses. Accepts a built-in
  name (`default` — bold black click-pointer with sparkle marks, shown
  by default; `claude` — the Claude logo) or an absolute path to your
  own SVG/PNG. Default `default`.
- `CUA_CURSOR_HOTSPOT` — `"fx,fy"` fractions (0..1) that locate the
  pointer tip inside the cursor image. Only needed for custom cursors
  whose tip isn't at the top-left (the default). Example: `0.5,0.5`
  for a centered hotspot.
- `CUA_CLICK_PRESS_SCALE` — how far the cursor shrinks on click
  (default `0.7`; i.e. 70% size). Set to `1.0` to disable the press
  animation.
- `CUA_CLICK_RING` — set to `0`/`false` to suppress the blue ring
  flash on click and rely only on the cursor-shrink animation
  (default: enabled).
- `CUA_GHOST_PARK_IDLE_S` — seconds of no tool-call activity before the
  ghost drifts from its last click location to the interior of its
  target app's window. Default `1.5`. The "park" behavior makes each
  agent's cursor visibly "belong to" its app when multiple agents are
  sharing the Mac. Set to a very large number to disable.
- `CUA_GHOST_Z_TRACK_S` — how often the ghost re-asserts its z-order
  relative to its target app's window. Default `0.15` (150 ms), so
  when the user Cmd-Tabs to the target app the ghost rises with it
  (and drops behind when another app is raised above the target).
- `CUA_GHOST_HARD_IDLE_S` — hard-idle backstop that self-exits the
  ghost after this many seconds of no commands, in case the
  parent-process-watch path somehow fails. Default `1800` (30 min);
  normally you never hit this because the ghost exits as soon as its
  parent server process dies.
- `CUA_AGENT_LABEL` — human-readable name for this server instance
  shown to other agents in "desktop busy: held by 'agent-X'" errors.
  Defaults to `MCP_CLIENT_NAME`, then `CURSOR_AGENT_ID`, then
  `pid-<n>`. Recommended when running multiple MCP clients against
  the same Mac.
- `CUA_LEASE_DIR` — directory for the desktop-lease lock file and
  holder metadata. Default `/tmp`. Only relevant if multiple Mac
  users (separate logins) are each running the server and you want
  them to contend on the same lock.
- `CUA_LEASE_DEFAULT_TTL_S` — how long an implicit per-call lease is
  considered live before another process can reclaim it if the
  holding process appears dead. Default `30` seconds.
- `CUA_LEASE_DEFAULT_WAIT_S` — how long `click`/`type_text`/etc.
  wait for a contended lease before failing with `ToolError`.
  Default `8` seconds.

## Ghost cursor playground

There's a small dev harness for tuning the ghost-cursor animation
that appears during tool calls (bezier path, duration, easing):

```bash
.venv/bin/python server/cursor_playground.py
```

A full-screen translucent window opens; every click triggers a ghost
animation from the previous click position to the new one. Click the
red `X` in the top-left to exit.

## Troubleshooting

- **"No captured app state for this app."** — call `get_app_state(app)`
  first. Element indexes are scoped to the most recent snapshot.
- **Empty accessibility tree across every app / every tool call fails
  with "macOS Accessibility permission is NOT granted"** —
  Accessibility permission is not granted to the GUI app that
  launched your MCP client (Cursor, Claude Desktop, iTerm, etc.). Call
  the `check_accessibility_permission` tool — it identifies the exact
  bundle id that needs to be toggled. Then either run
  `open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"`
  or call the `open_accessibility_settings` tool to jump to the pane.
  Enable the app, then **fully quit and relaunch it** (not just close
  the window) so child processes inherit the new permission. On first
  startup the server also asks macOS to show the standard "would like
  to control your computer" prompt; dismissing it without granting
  access lands you in this state.
- **Clicks seem to go nowhere** — many apps silently drop
  `CGEventPostToPid` events when they lack focus and the target
  element is inside a heavy WebView. Fall back to `AXPress` via
  `click(element_index=N)` (the server uses `AXPress` automatically
  when available), or use `perform_secondary_action`.
- **`CGWindowListCreateImage` returns nothing** — Screen Recording
  permission is not granted. Screenshots will be `None` but the
  accessibility tree still works.
- **"element X has no position/size"** — the element is either
  off-screen or the app declines to report geometry. Call
  `perform_secondary_action` with `ScrollToVisible` first, then
  re-read state.
- **Slack/Cursor/Notion tree has only ~10 elements** — first
  `get_app_state` of an Electron app flips on `AXManualAccessibility`
  and then retries briefly for the renderer to build its tree (up to
  ~1s). If the retry ceiling isn't enough for your app, call
  `get_app_state` again.
- **Chrome scroll errors with "could not scroll in a background-safe
  way"** — see `server/app-hints/Chrome.md` for workarounds.
- **`perform_secondary_action` refuses `ShowMenu` / `ShowAlternateUI`
  / `Raise`** — by design. Those actions almost always raise the
  target app to the foreground. Pass
  `allow_foreground_activation=True` if you explicitly want that.

## Code layout

- `bin/cua-server` — bash launcher. Provisions `.venv/` and execs the
  Python server.
- `server/cua_server.py` — FastMCP server with all nine tools.
- `server/cursor_ghost.py` — AppKit daemon for the animated ghost
  cursor that appears during tool calls.
- `server/cursor_paths.py` — bezier-path sampling for the ghost.
- `server/cursor_playground.py` — stand-alone dev playground.
- `server/desktop_lease.py` — cross-process `flock`-based mutex that
  serializes mutating tool calls from concurrent MCP clients so they
  don't stomp on each other's keystrokes. Exposes the
  `acquire_desktop`/`release_desktop`/`desktop_status` MCP tools.
- `server/app-hints/<App>.md` — app-specific behavioral notes that
  get injected as `<app_hints>` in the `get_app_state` response for
  that app. Picked by bundle id or display name via the
  `_HINT_ALIASES` table in the server.
- `skills/<name>/SKILL.md` — operator-facing guidance the model
  loads when the user's intent matches that app/skill.
