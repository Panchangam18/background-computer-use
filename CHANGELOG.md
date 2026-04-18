# Changelog

All notable changes to this project are documented here. Dates are in
YYYY-MM-DD. Versions follow [semver](https://semver.org/).

## [0.2.0] — 2026-04-18

First reasonably-polished release. Most of the fixes below came from
running the plugin against a dozen real apps in one session and
patching every footgun I hit. Also reframed the project from "Claude
Code plugin" to "MCP server + skill library usable with any MCP
client" — the server and skills were already generic; the docs and
README were previously Claude-Code-only in tone. Also reframed the project from "Claude
Code plugin" to "MCP server + skill library usable with any MCP
client" — the implementation was already generic; the docs were
previously Claude-Code-only in tone.

### Added

- `smooth=True` default on `scroll`: scrolls animate over ~200ms per
  page instead of teleporting.
- Chrome / Chromium scroll: a new Path 2b in `scroll` drives page and
  sub-scroller scrolling via AppleScript `execute ... javascript`, the
  only background-safe scroll primitive for Chromium. Uses a nearest-
  scrollable-ancestor heuristic so it picks the right sub-div on
  app-style pages (Gmail, LinkedIn, Notion).
- Electron app support: `_maybe_enable_chromium_ax` now flips on
  `AXManualAccessibility` (and `AXEnhancedUserInterface` where
  applicable) for known Electron bundles (Slack, VS Code, Cursor,
  Discord, Notion, Linear, Figma, Docker, etc.), and `get_app_state`
  retries briefly (150ms / 400ms / 600ms) for the renderer tree to
  populate.
- App-specific hints for Safari and Chrome.
- Matching skill files under `skills/safari/` and `skills/chrome/`.
- Smooth `scroll` path via direct `AXValue` writes on scroll-bar
  children (the AppKit `NSScrollView` path).
- `allow_foreground_activation` opt-in on `perform_secondary_action`:
  `AXShowMenu`, `AXShowAlternateUI`, and `AXRaise` are now refused by
  default, since they virtually always bring the target app to the
  foreground.
- Key aliases in `press_key`: `Page_Down`, `Page_Up`, `Caps_Lock`,
  `BackSpace`, `Insert`, `Forward_Delete`, plus case-and-underscore
  tolerance so xdotool names all just work.
- Marketplace manifest (`.claude-plugin/marketplace.json`) so the repo
  itself is installable via `/plugin marketplace add`.
- `docs/DESIGN.md`, `docs/APPS.md`, and `docs/DEVELOPING.md`.
- `.gitignore`, `LICENSE`, `CHANGELOG.md`.

### Changed

- `set_value` on Safari's URL bar (and any known-sticky identifier)
  now skips the AX write and goes straight to a focus → `cmd+a` →
  `type_text` → `Return` sequence. `submit=True` is the new default.
- `set_value` auto-detects non-sticky AX writes that were accepted but
  didn't commit (some Chromium form fields, some Electron inputs) and
  falls back to the typing path.
- `scroll` is strictly background-safe: it no longer silently falls
  back to keyboard events (`PageDown`) that would raise the target
  window. Instead it errors out honestly and lets the caller decide
  whether to use `press_key` with a foreground-activation cost.
- `_running_apps` pumps the NSRunLoop before reading
  `NSWorkspace.runningApplications()`, so apps launched after server
  startup are visible. Previously the server's snapshot was frozen at
  startup because it doesn't run a Cocoa runloop itself.
- `_web_content_pid_for_rect` (used by scroll's wheel-event path) now
  recognizes Chrome, Brave, and Edge renderer helpers, not just
  Safari's `Safari Web Content`.
- App hints moved from `app-instructions/` to `server/app-hints/`
  (server-private data, not a standard plugin directory). The old
  path is still checked as a legacy fallback.
- `perform_secondary_action` error messages are now actionable —
  including a specific hint for AXError `-25205` (`AttributeUnsupported`).

### Fixed

- Safari's URL bar now actually navigates when you `set_value` it.
  Previously the AX write succeeded but Safari's address bar kept its
  old value and ignored subsequent `Return` key presses.
- Safari's web content now scrolls. Previously the scroll tool reported
  success but the page didn't move because wheel events to Safari's
  main pid were discarded by the WKWebView renderer process.
- Scroll no longer silently steals focus when the wheel path can't
  drive the target view.
- `press_key(key="Page_Down")` (xdotool canonical name) works;
  previously only the lowercase-no-underscore `pagedown` form did.

## [0.1.0] — 2026-04-17

Initial scaffold. Parity port of the Codex computer-use tool surface
(nine tools) with per-app skills for Clock, Numbers, Apple Music,
Spotify, and Notion.
