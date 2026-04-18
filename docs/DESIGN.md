# Background Computer Use Notes

This README captures everything I can reliably glean from the computer-use capability available in this Codex desktop environment, plus the parts of Anthropic's current Claude Code docs that matter if the goal is to build a Claude plugin with the same background computer-use behavior.

The goal here is not to guess at hidden implementation details and present them as facts. Wherever possible, this document separates:

- `Observed`: directly visible from the tool interface, live tool responses, or installed local bundle files
- `Documented`: stated in official Anthropic docs
- `Inferred`: likely true, but not directly proven from the available evidence

## Executive Summary

If you want Claude Code to have the closest possible equivalent to this Codex computer-use capability, a Claude plugin should almost certainly ship:

- an MCP server that exposes GUI-control tools
- one or more skills that teach Claude how to use those tools
- optional monitors for background notifications or status watching
- optional app-specific instruction files for brittle UIs

The plugin system alone is not the computer-use engine. The real work is a local native service or daemon that can:

- inspect the active UI tree
- capture screenshots
- synthesize mouse and keyboard input
- manage permissions
- manage locking and safety boundaries

## Sources Used

### Directly observed in this Codex environment

- The `Computer Use` plugin is enabled in this session.
- The exposed tools are:
  - `list_apps`
  - `get_app_state`
  - `click`
  - `drag`
  - `perform_secondary_action`
  - `press_key`
  - `scroll`
  - `set_value`
  - `type_text`
- A live `list_apps` call
- A live `get_app_state` call against `Finder`
- The installed local bundle under:
  - `~/.codex/plugins/cache/openai-bundled/computer-use/1.0.750/`
- Local bundle metadata from the plugin's `Info.plist`
- Bundled app-specific instruction files for:
  - Clock
  - Numbers
  - Apple Music
  - Spotify
  - Notion

### Official Anthropic docs fetched from the docs index you pointed me to

- Docs index: [llms.txt](https://code.claude.com/docs/llms.txt)
- Computer use docs: [computer-use](https://code.claude.com/docs/en/computer-use)
- Plugin docs: [plugins](https://code.claude.com/docs/en/plugins)
- Plugin reference: [plugins-reference](https://code.claude.com/docs/en/plugins-reference)
- MCP docs: [mcp](https://code.claude.com/docs/en/mcp)
- Tools reference: [tools-reference](https://code.claude.com/docs/en/tools-reference)

## What Codex Exposes Today

### Tool surface

Observed tool API:

| Tool | What it does | Key inputs |
| --- | --- | --- |
| `list_apps` | Lists apps available on the machine, including running apps and recently used apps | none |
| `get_app_state` | Starts an app-use session if needed, then returns the app's key-window accessibility tree and a screenshot | `app` |
| `click` | Clicks an accessibility element or raw coordinates | `app`, `element_index` or `x`/`y`, optional `click_count`, `mouse_button` |
| `drag` | Drags from one screen coordinate to another | `app`, `from_x`, `from_y`, `to_x`, `to_y` |
| `perform_secondary_action` | Invokes a named secondary accessibility action on an element | `app`, `element_index`, `action` |
| `press_key` | Sends a key or key combo | `app`, `key` |
| `scroll` | Scrolls an element in a direction by pages | `app`, `element_index`, `direction`, optional `pages` |
| `set_value` | Sets the value of a settable accessibility element | `app`, `element_index`, `value` |
| `type_text` | Types literal text as keyboard input | `app`, `text` |

### App identifiers

Observed:

- `app` accepts either a human app name or a bundle identifier.
- Example names and bundle IDs were returned together by `list_apps`, such as:
  - `Safari — com.apple.Safari`
  - `Finder — com.apple.finder`
  - `Claude — com.anthropic.claudefordesktop`

### `list_apps` behavior

Observed:

- It returns both currently running apps and apps used recently.
- The response includes:
  - display name
  - bundle identifier
  - running state
  - last-used date
  - usage count
- The tool description says the history window is the last 14 days.

Example observed shape:

```text
Safari — com.apple.Safari [running, last-used=2026-04-16, uses=4060]
Finder — com.apple.finder [running]
Claude — com.anthropic.claudefordesktop [running, last-used=2026-04-16, uses=4]
```

### `get_app_state` behavior

Observed:

- The tool description says it must be called once per assistant turn before interacting with the app.
- It returns:
  - app name and PID
  - key window title
  - an indexed accessibility tree
  - element roles
  - element labels and values
  - element IDs when available
  - settable/disabled/selected state when available
  - secondary actions when available
  - a note about selected content
  - a screenshot
- In a live call, the output began with `Computer Use state (CUA App Version: 750)`.

Example observed structure:

```text
<app_state>
App=com.apple.finder (pid 86668)
Window: "Astropad Workbench", App: Finder.
    0 standard window Astropad Workbench, ID: FinderWindow, Secondary Actions: Raise
        1 split group
            2 scroll area
                3 collection Description: icon view, ID: IconView
                    5 image (selected) Astropad Workbench, Secondary Actions: open
                    6 image Applications, Secondary Actions: open
...
Selected:
7 container Astropad Workbench
...
</app_state>
```

This strongly suggests the state object is designed to be both machine-readable by the model and human-debuggable.

### Element model

Observed from the live tree:

- Each node has a numeric index.
- Nodes may include:
  - role, like `standard window`, `scroll area`, `collection`, `image`, `text`, `slider`
  - visible label text
  - description
  - ID
  - value
  - flags like `selected`, `disabled`, `settable`
  - secondary actions
  - help text

Inferred:

- Element indexes are probably snapshot-local, not durable IDs across turns.
- The intended loop is likely:
  1. get fresh state
  2. choose element indexes from that state
  3. act
  4. refresh state

### Mouse and keyboard model

Observed from tool definitions:

- `click` supports either:
  - accessibility targeting by `element_index`
  - raw pixel targeting by `x` and `y`
- `click_count` defaults to 1.
- `mouse_button` supports `left`, `right`, or `middle`.
- `drag` is coordinate-based only.
- `press_key` explicitly says it supports `xdotool` key syntax, with examples like:
  - `a`
  - `Return`
  - `Tab`
  - `super+c`
  - `Up`
  - `KP_0`
- `type_text` is literal text input, distinct from `press_key`.

This split matters. A faithful port for Claude should preserve the distinction between:

- semantic key presses
- raw text entry
- element-based mouse actions
- coordinate-based mouse actions

### Scroll and secondary actions

Observed:

- `scroll` requires an `element_index`, not raw coordinates.
- Supported directions are `up`, `down`, `left`, `right`.
- It scrolls by pages, not pixels.
- `perform_secondary_action` exposes named actions discovered from accessibility, such as `Raise`, `open`, or app-specific actions.

That secondary-action surface is especially important. It means the engine is not limited to generic click/type behavior; it can sometimes invoke richer native actions exposed by the OS accessibility layer.

## Interaction Model

Based on the tool contract and live output, the intended usage pattern looks like this:

1. Call `list_apps` if the model needs to discover what app to use.
2. Call `get_app_state(app)` before interacting in the current turn.
3. Parse the accessibility tree and screenshot together.
4. Prefer element indexes when the target is clearly represented in accessibility.
5. Fall back to coordinates when accessibility is insufficient.
6. Use `perform_secondary_action` when the tree advertises a useful native action.
7. Re-read state after meaningful UI changes.

This is a hybrid vision-plus-accessibility model, not a pure screenshot bot and not a pure accessibility client.

## Local Bundle Clues

The installed bundle provides some useful implementation clues.

### Bundle location and version

Observed:

- Cached plugin root:
  - `~/.codex/plugins/cache/openai-bundled/computer-use/1.0.750/`
- Main app:
  - `Codex Computer Use.app`
- Live state output reported:
  - `CUA App Version: 750`

This lines up cleanly with the cached plugin version.

### Main bundle metadata

Observed from `Info.plist`:

- Bundle name: `Codex Computer Use`
- Bundle identifier: `com.openai.sky.CUAService`
- Executable: `SkyComputerUseService`
- Version: `1.0` / build `750`
- Minimum macOS version: `15.0`
- `LSUIElement` is `true`

`LSUIElement=true` suggests the service runs as an agent-style background app rather than a normal dock app.

### Nested client app

Observed:

- Nested client app:
  - `SharedSupport/SkyComputerUseClient.app`
- Bundle identifier:
  - `com.openai.sky.CUAService.cli`
- Executable:
  - `SkyComputerUseClient`

Inferred:

- There is likely a separation between the resident background service and a client-side helper used by Codex to communicate with it.

### Update machinery

Observed:

- The bundle contains Sparkle-style update keys including:
  - `SUFeedURL`
  - `SUPublicEDKey`
  - automatic update settings

This implies the computer-use component is versioned and updated as a discrete desktop-side binary, not just as prompt instructions.

## Bundled App-Specific Instructions

One of the clearest implementation clues is that the bundle ships app-specific markdown instructions.

Observed files:

- `AppInstructions/Clock.md`
- `AppInstructions/Numbers.md`
- `AppInstructions/AppleMusic.md`
- `AppInstructions/Spotify.md`
- `AppInstructions/Notion.md`

This is a big deal. It means the system does not rely only on general GUI reasoning. It supplements the generic toolset with targeted behavioral guidance for brittle or high-variance apps.

### What those instructions tell us

Observed patterns:

- `Clock.md` includes task-specific procedures for world clocks, timers, stopwatches, and alarms.
- `Numbers.md` explains spreadsheet editing patterns like single-click versus triple-click and warns against batching too much input in one text call.
- `Notion.md` explains block-based editing semantics and how Return behaves in different block types.
- `AppleMusic.md` explains search behavior, queue management, and using accessibility scroll actions.
- `Spotify.md` warns that playback state can lag and recommends re-reading state instead of acting too quickly.

Design takeaway:

- Generic tools are necessary but not sufficient.
- A Claude plugin that aims for parity should probably include:
  - a generic MCP server
  - one or more skills containing app-specific operating heuristics
  - perhaps per-app reference files that Claude can load on demand

## What We Can Safely Infer About The Engine

These are inferences, not proven facts:

- It almost certainly uses macOS accessibility APIs for the element tree and actions.
- It likely uses standard input-synthesis primitives under the hood for clicks, drags, scrolls, and key presses.
- It likely captures screenshots independently of the accessibility tree and returns both in one tool response.
- It likely maintains an app session layer so the same app can be targeted across several consecutive actions.
- It likely has its own internal lock or arbitration mechanism to prevent conflicting control.

What I cannot prove from the available evidence:

- the exact native frameworks used
- whether coordinates are in logical points or backing pixels
- whether the engine hides windows itself or asks the host app to do it
- how permission prompts are implemented on the Codex side
- whether there is any OCR beyond screenshot transmission to the model

## Claude Code Facts That Matter For A Port

These come from Anthropic's current official docs.

### Computer use in Claude Code

Documented:

- Claude Code exposes computer use as a built-in MCP server called `computer-use`.
- In the CLI, it is available on macOS only.
- It requires an interactive session.
- It requires Screen Recording and Accessibility permissions.
- Only one Claude session can use computer use at a time because it holds a machine-wide lock.
- Other visible apps are hidden while Claude works, and the terminal stays visible but excluded from screenshots.
- Screenshots are downscaled automatically before being sent to the model.
- Per-app approvals happen per session.

Those details are from the current [computer-use docs](https://code.claude.com/docs/en/computer-use).

### What a Claude plugin can actually ship

Documented:

- Plugins can include:
  - skills
  - agents
  - hooks
  - MCP servers
  - LSP servers
  - monitors
- MCP servers can be declared in `.mcp.json` or inline in `.claude-plugin/plugin.json`.
- Monitors can run background commands for the life of the session and stream stdout back to Claude.
- Skills are auto-discovered and can be invoked automatically based on context.
- Custom tools come from MCP servers, not from skills alone.

Those details are from the current [plugins reference](https://code.claude.com/docs/en/plugins-reference), [plugins guide](https://code.claude.com/docs/en/plugins), and [tools reference](https://code.claude.com/docs/en/tools-reference).

## What This Means For A Claude Plugin Design

If the target is "same exact background computer use abilities," the closest Claude-side shape is probably:

```text
your-plugin/
├── .claude-plugin/
│   └── plugin.json
├── .mcp.json
├── skills/
│   ├── computer-use/
│   │   └── SKILL.md
│   ├── notion/
│   │   └── SKILL.md
│   └── numbers/
│       └── SKILL.md
├── monitors/
│   └── monitors.json
└── bin/ or native service files
```

### The MCP server should provide the real capability

Recommended parity tool surface:

- `list_apps`
- `get_app_state`
- `click`
- `drag`
- `perform_secondary_action`
- `press_key`
- `scroll`
- `set_value`
- `type_text`

Why preserve these names and separations:

- They already reflect a workable abstraction boundary.
- They split accessibility-native operations from coordinate fallbacks.
- They are expressive without being too low-level.
- They let skills encode operating policy without hardcoding every app into the server.

### Skills should encode operating heuristics

A skill layer should teach Claude things like:

- always call `get_app_state` before interacting in a new turn
- prefer element indexes over coordinates when available
- re-read state after opening menus, navigating views, or waiting for async results
- use `set_value` only for truly settable controls
- use `type_text` for freeform typing and `press_key` for shortcuts
- check app-specific reference files before acting in fragile apps

### Monitors are useful, but not the core computer-use engine

Claude plugin monitors can help with background status watching, but they do not replace GUI control. They are useful for sidecar behaviors like:

- watching logs while Claude drives a desktop app
- notifying Claude when a build finishes so it can resume GUI testing
- tracking simulator output while GUI automation is in flight

### Native service requirements

To match this Codex capability well, your local service will likely need:

- screenshot capture
- accessibility tree extraction
- accessibility actions
- keyboard input synthesis
- mouse input synthesis
- app discovery
- app/session locking
- permission management
- a stable mapping from each UI snapshot to element indexes

Nice-to-have parity features:

- app-specific instruction bundles
- per-app approval tiers
- hiding non-approved apps while automation runs
- terminal exclusion from screenshots
- emergency stop support

## Porting Risks And Unknowns

These are the main parity risks I see right now:

- The exact screenshot-to-coordinate mapping is unknown.
- The exact indexing strategy for accessibility nodes is unknown.
- The Codex bundle likely includes host-side safety logic that is not visible from the external tool surface.
- Some app behaviors may depend on custom per-app instructions rather than the raw tool API.
- Claude Code's built-in computer use has its own policies and lock behavior; a custom plugin may not be able to integrate with those internals exactly unless you reimplement them yourself.

## Practical Conclusion

The important lesson from Codex's computer-use capability is not just "it can click and type."

The real recipe appears to be:

1. a native local service
2. a compact but expressive tool surface
3. a state payload that combines screenshot plus accessibility tree
4. a per-turn interaction discipline
5. app-specific operating knowledge layered on top
6. host-side safety and session control

If we want to reproduce this in Claude Code, the right next move is to scaffold a plugin whose MCP server matches the Codex tool surface first, then add skills and app-specific instruction files until the behavior feels comparable.

## Suggested Next Step

After this README, the next concrete step should be to scaffold a Claude plugin with:

- `.claude-plugin/plugin.json`
- `.mcp.json`
- a local server binary or script stub
- a `skills/computer-use/SKILL.md`
- one or two app-specific skills or references

That would turn this reverse-engineering note into a working plugin skeleton.

## Plugin Implementation

That scaffold exists in this repo. See [`../README.md`](../README.md) for
install + quickstart and [`DEVELOPING.md`](./DEVELOPING.md) for the
local-development loop.

Layout:

```text
background-computer-use/
├── .claude-plugin/
│   ├── plugin.json              # plugin manifest
│   └── marketplace.json         # so `/plugin marketplace add` works on the repo
├── .mcp.json                    # registers the MCP server
├── bin/
│   └── cua-server               # bash launcher, bootstraps venv
├── server/
│   ├── requirements.txt         # mcp + PyObjC + Pillow
│   ├── cua_server.py            # FastMCP server implementing all 9 tools
│   ├── cursor_ghost.py          # ghost-cursor overlay daemon
│   ├── cursor_paths.py          # bezier path generator for the ghost
│   ├── cursor_playground.py     # local dev harness for the ghost
│   └── app-hints/               # per-app <app_hints> payloads
│       ├── Safari.md
│       ├── Chrome.md
│       ├── Clock.md
│       ├── Numbers.md
│       ├── AppleMusic.md
│       ├── Spotify.md
│       └── Notion.md
├── skills/
│   ├── computer-use/SKILL.md    # main operating-loop skill
│   ├── safari/SKILL.md
│   ├── chrome/SKILL.md
│   ├── clock/SKILL.md
│   ├── numbers/SKILL.md
│   ├── apple-music/SKILL.md
│   ├── spotify/SKILL.md
│   └── notion/SKILL.md
└── docs/
    ├── DESIGN.md                # this file
    ├── APPS.md                  # app-support matrix + known limitations
    └── DEVELOPING.md            # local-dev loop + troubleshooting
```

Implementation highlights:

- Every GUI event is posted via `CGEventPostToPid(pid, event)`, so
  interactions do not activate the target app or move the user's cursor.
- `click` on an `element_index` uses `AXPress` when the element advertises
  it; otherwise it falls back to a synthesized mouse event at the element
  center.
- `get_app_state` returns both a tree (via `AXUIElementCopyAttributeValue`)
  and a window-scoped screenshot (via `CGWindowListCreateImage` with
  `kCGWindowListOptionIncludingWindow`), plus any bundled
  `server/app-hints/<App>.md` markdown as an `<app_hints>` trailer.
- Element indexes are snapshot-local: every `get_app_state` rebuilds a
  fresh breadth-first walk and caches it per pid, matching the Codex
  surface's "call state once per turn" contract.
- Chromium browsers and Electron apps are auto-opted into
  `AXManualAccessibility`, and Chromium scroll is done via AppleScript
  `execute ... javascript` (the only background-safe scroll path for
  Chrome since wheel events to Chrome's main pid are dropped when the
  window isn't key).
