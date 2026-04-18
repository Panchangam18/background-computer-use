# App support matrix

Tested against a macOS 15 install. Apps not listed here are probably
fine if they use native AppKit / SwiftUI; try them and open an issue if
anything misbehaves.

**Read-only** means the tree + screenshots work but interactions are
limited. **Full** means read + click + type + scroll all behave
background-safely.

| App                | Framework          | Read | Click | Type | Scroll | Notes |
|--------------------|--------------------|------|-------|------|--------|-------|
| Safari             | WebKit (native)    | ✅    | ✅     | ✅    | ✅      | URL bar + in-page links + smooth scroll via AXValue on scroll-bar child. `set_value` on URL bar uses typing fallback automatically. |
| Google Chrome      | Blink (Chromium)   | ✅    | ✅     | ✅    | ✅      | URL bar, link clicks, typing into forms, scroll via AppleScript `execute ... javascript` (picks nearest scrollable ancestor at viewport center). |
| Chromium (other)   | Blink              | ✅    | ✅     | ✅    | ✅      | Brave, Edge, Arc, Opera, Vivaldi, ChatGPT Atlas. Same code path as Chrome. |
| Finder             | AppKit             | ✅    | ⚠️     | —    | ✅      | Sidebar selection is focus-dependent (macOS limitation). Scroll works great. |
| Notes              | AppKit             | ✅    | ✅     | ✅    | ✅      | Full AX tree, smooth scroll. |
| Calculator         | SwiftUI            | ✅    | ✅     | —    | —      | Button taps via AXPress work perfectly. |
| System Settings    | SwiftUI            | ✅    | ⚠️     | —    | ✅      | Navigation between panels is focus-dependent like Finder. Inner-pane scroll works. |
| Slack              | Electron           | ✅    | ⚠️     | ⚠️    | —      | After `AXManualAccessibility` flip the tree exposes 300+ nodes. Interaction inside the webview works but sidebar-channel switching is focus-dependent. |
| Cursor (self)      | Electron           | ✅    | ⚠️     | —    | —      | Only the focused window exposes a rich tree; unfocused windows show outer-chrome only. |
| Notion             | Electron           | Unknown | —  | —    | —      | Not tested in this session; likely similar to Slack. |
| Apple Music        | AppKit/WebKit      | ✅    | ✅     | ✅    | ✅      | Includes bundled `<app_hints>` for playback control. |
| Spotify            | Electron           | ✅    | ✅     | ⚠️    | ✅      | Playback state lags the UI 300-800ms after key presses. |
| Clock              | SwiftUI            | ✅    | ✅     | —    | —      | Bundled hints for alarms/timers/world-clocks. |
| Numbers            | AppKit             | ✅    | ✅     | ✅    | ✅      | Bundled hints for cell editing quirks. |

Legend:

- ✅ Works fully background-safe
- ⚠️ Works but has a known limitation (see Notes)
- — Not tested or not applicable
- Focus-dependent: the action succeeds at the AX level but the target
  app's view layer won't commit it unless the window is key. Not a bug
  in the tool; it's how those apps are wired.

## App frameworks at a glance

| Framework | Typical characteristics | Works in background? |
|-----------|------------------------|----------------------|
| AppKit (pure)    | Rich AX tree by default. `AXValue`, `AXPress`, `AXScroll*ByPage` all reliable. | Yes, excellent |
| SwiftUI          | Similar to AppKit. Sometimes exposes fewer semantic attributes on custom views. | Yes, excellent |
| WebKit (Safari)  | WKWebView wraps an `AXWebArea` inside a host `AXScrollArea` with a sibling `AXScrollBar`. | Yes |
| Blink (Chrome)   | Renders a single `AXWebArea` under the host window. No scroll bar in the AX tree. | Yes, via AppleScript JS for scroll; AX for everything else |
| Electron         | Needs `AXManualAccessibility=True` to expose renderer DOM. Tree only populates for the focused window. | Yes, once opted in |
| Catalyst (UIKit) | Not explicitly tested. Should behave like AppKit via the Catalyst AX bridge. | Probably yes |
| Java Swing       | Uses Apple's Java AX peer; coverage is sparse. | Partial — coordinates often needed |
| Qt               | Depends on the Qt version's AX backend. | Usually partial |

## Known limitations

### Sidebar selection in focus-dependent apps

Finder, System Settings, Mail, Apple Music's library sidebar, etc. use
`NSOutlineView` / `NSTableView` sidebars that ignore clicks unless the
containing window is key. Our `click(element_index=...)` synthesizes
`AXPress` successfully but the app's own delegate refuses to commit the
selection change.

**Workarounds**:
- Use menu-item shortcuts via `press_key` where possible (e.g.
  `cmd+shift+o` for Finder's "Go to folder").
- Accept the activation cost when the UX clearly requires it.

### Electron multi-window

When an Electron app has multiple windows, only the **key** window
exposes a rich AX tree. Secondary windows return ~10 elements (outer
chrome only). There's no clean workaround other than calling
`get_app_state` with the right window focused.

### Chrome scroll without a scrollable ancestor

Our Chrome scroll heuristic picks the nearest scrollable ancestor at
the viewport center. If a user wants to scroll a specific sub-region
that isn't at the viewport center, or an iframe the JS can't reach,
the tool reports the `window`/`document` scroll amount, which might be
zero on app-style pages with all-sticky headers.

**Workarounds**:
- `click` into the sub-region first (to re-center the heuristic).
- Use `press_key PageDown` after acknowledging the foreground-activation
  cost.
