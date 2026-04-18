# Developing locally

This repo is both a working Claude Code plugin and a small native macOS
service. This doc covers the local-development loop, permissions setup,
and troubleshooting. User-facing install instructions are in the
[top-level README](../README.md).

## Requirements

- macOS 13+ (tested target is 15+; older may work but is unsupported).
- Python 3.10+ on your `$PATH` as `python3`, or set `CUA_PYTHON` to a full path.
- Claude Code installed and authenticated.
- Two macOS permissions for the **process that runs Claude Code**
  (usually your terminal, Claude Code app, or Cursor if you run it
  through Cursor's plugin support):
  - **Accessibility** — System Settings → Privacy & Security → Accessibility
  - **Screen Recording** — System Settings → Privacy & Security → Screen Recording

Without both permissions the MCP server will start, but `get_app_state`
will return an empty/error-like tree and screenshots will be blank.

## Develop against a working copy

From the repo root:

```bash
claude --plugin-dir .
```

On first run the launcher (`bin/cua-server`) creates a venv under
`.venv/` and installs `server/requirements.txt` (MCP SDK, PyObjC
bindings, Pillow). Subsequent runs reuse the venv.

Inside Claude Code, try:

```text
> list_apps
> get_app_state app="Finder"
> click app="Finder" element_index=0
```

Run `/reload-plugins` after editing `SKILL.md` or `plugin.json` files.

If you edit the Python server itself, you need to restart the MCP
server for Claude Code to pick up the change. The easiest way is to
exit Claude Code and relaunch, or kill the server process and let
Claude Code respawn it.

## Environment variables

The launcher and server read:

- `CUA_PYTHON` — path to the Python interpreter to use (default `python3`).
- `CUA_PLUGIN_ROOT` — overrides the plugin root; normally set for you by
  Claude Code's `${CLAUDE_PLUGIN_ROOT}` expansion in `.mcp.json`.
- `CUA_LOG_LEVEL` — `DEBUG`/`INFO`/`WARNING` (default `INFO`). Server logs
  go to stderr and land in Claude Code's MCP log buffer.
- `CUA_MAX_ELEMENTS` — hard cap on snapshot elements (default `1200`).
- `CUA_MAX_DEPTH` — accessibility tree depth cap (default `30`).
- `CUA_SCREENSHOT_MAX_EDGE` — downscale screenshots so their longest edge is
  at most this many pixels (default `1600`).
- `CUA_CLICK_OVERLAY` — set to `0`/`false` to disable the ghost-cursor
  visualization that runs during tool calls (default: enabled).

## Ghost cursor playground

There's a small dev harness for tuning the ghost-cursor animation that
appears during tool calls (bezier path, duration, easing):

```bash
.venv/bin/python server/cursor_playground.py
```

A full-screen translucent window opens; every click triggers a ghost
animation from the previous click position to the new one. Click the
red `X` in the top-left to exit.

## Troubleshooting

- **"No captured app state for this app."** — call `get_app_state(app)`
  first. Element indexes are scoped to the most recent snapshot.
- **Empty accessibility tree / `AXIsProcessTrusted` warning** —
  Accessibility permission is not granted to the process running Claude
  Code. Toggle it off and back on after granting, then fully restart
  the process.
- **Clicks seem to go nowhere** — many apps silently drop
  `CGEventPostToPid` events when they lack focus and the target element
  is inside a heavy WebView. Fall back to `AXPress` via
  `click(element_index=N)` (the server uses `AXPress` automatically when
  available), or use `perform_secondary_action`.
- **`CGWindowListCreateImage` returns nothing** — Screen Recording
  permission is not granted. Screenshots will be `None` but the
  accessibility tree still works.
- **"element X has no position/size"** — the element is either
  off-screen or the app declines to report geometry. Call
  `perform_secondary_action` with `ScrollToVisible` first, then re-read
  state.
- **Slack/Cursor/Notion tree has only ~10 elements** — first
  `get_app_state` of an Electron app flips on `AXManualAccessibility`
  and then retries briefly for the renderer to build its tree (up to
  ~1s). If the retry ceiling isn't enough for your app, the server
  falls through with a truncated tree; call `get_app_state` again and
  it should be populated.
- **Chrome scroll errors with "could not scroll in a background-safe
  way"** — this is expected if the server can't find a scrollable
  ancestor via AppleScript JavaScript. See `server/app-hints/Chrome.md`
  for workarounds.
- **`perform_secondary_action` refuses `ShowMenu` / `ShowAlternateUI` /
  `Raise`** — by design. Those actions almost always raise the target
  app to the foreground. Pass `allow_foreground_activation=True` if
  you explicitly want that behavior.

## Code layout

- `bin/cua-server` — bash launcher. Provisions `.venv/` and execs the
  Python server.
- `server/cua_server.py` — FastMCP server with all nine tools.
- `server/cursor_ghost.py` — AppKit daemon for the animated ghost
  cursor that appears during tool calls.
- `server/cursor_paths.py` — bezier-path sampling for the ghost.
- `server/cursor_playground.py` — stand-alone dev playground.
- `server/app-hints/<App>.md` — app-specific behavioral notes that get
  injected as `<app_hints>` in the `get_app_state` response for that
  app. Picked by bundle id or display name via the `_HINT_ALIASES`
  table in the server.
- `skills/<name>/SKILL.md` — operator-facing guidance the model loads
  when the user's intent matches that app/skill. Skills are auto-
  discovered by Claude Code.

## Testing the server in isolation

The server runs as a stdio MCP server. You can smoke-test without
Claude Code by running:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'server'); import cua_server; print('ok')"
```

For interactive testing, use `@modelcontextprotocol/inspector`:

```bash
npx @modelcontextprotocol/inspector ./bin/cua-server
```
