# Safari

Safari is mostly well-covered by accessibility, but its address bar and web
content areas have a couple of quirks that bite naive GUI automation. Read
this before driving Safari.

## URL bar: use `set_value` with the default `submit=True`

The smart search field (`ID: WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD`) accepts
`AXValue` writes and its accessibility tree will reflect the new value, but
the visible URL bar and the navigation engine don't actually observe that
write. The result is that `set_value` followed by `press_key key="Return"`
silently does nothing.

The server works around this automatically:

- `set_value(app="Safari", element_index=<smart-search-field>, value="…")`
  now defaults to `submit=True` and, because the field is on the internal
  sticky-identifier list, it skips AX `kAXValue` and goes straight to a
  focus → `cmd+a` → `type_text` → `Return` sequence.

So the correct recipe is simply:

```text
get_app_state app="Safari"
# Find the smart search field (ID: WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD)
set_value app="Safari" element_index=<N> value="https://example.com"
```

If for some reason you want to stage a URL without navigating, pass
`submit=False` — but note that doing so on Safari will also silently fail
at the view layer (the typing fallback is what makes it work).

A fully manual fallback that also works:

```text
press_key app="Safari" key="cmd+l"
type_text app="Safari" text="https://example.com"
press_key app="Safari" key="Return"
```

## Scrolling web content

Safari's web content lives in a separate `Safari Web Content` helper
process per WKWebView. Scroll-wheel events posted to Safari's main pid
are dropped by the content process's event loop.

The `scroll` tool is **strictly background-safe**: it tries native AX
scroll-by-page actions, then direct `AXValue` writes on the scroll bar,
then pixel-unit wheel events routed to the WebContent helper pid.

On Safari web content specifically, all three of these frequently fail
because WKWebView doesn't honor AX scroll actions on arbitrary tabs and
the content-process pid routing is best-effort. If `scroll` errors out
saying "could not scroll in a background-safe way", you have two honest
options:

- Call `perform_secondary_action(element_index=<off-screen target>, action=
  "ScrollToVisible")` if you know a specific child element you want
  brought into view. This is background-safe and usually works.
- Call `press_key(key="PageDown")` or `press_key(key="space")`
  explicitly. This **will** bring Safari to the foreground as the key
  window, because that's the only way WKWebView will accept a synthetic
  key event. Only do this when the user has asked you to scroll and
  you've warned them (or when you can infer it's acceptable).

The tool deliberately does not silently escalate to key events, because
that would violate the background-use contract.

## Links and buttons in the page

These work well. `click(element_index=<link-index>)` uses `AXPress` on the
underlying `AXLink` / `AXButton` and navigates as expected, even when the
Safari window is occluded.

## Tabs

- Tabs are `AXTabButton` rows in the tab bar. `click(element_index=<tab>)`
  switches via `AXPress`.
- Each tab advertises `Name:close tab` as a secondary action. Use
  `perform_secondary_action(action="close tab")` to close a tab without
  having to hit-test the tiny `x` glyph.
- `ShowMenu` on a tab opens the tab's context menu (pin, mute, duplicate,
  etc.). Re-read state after the menu appears.

## Reader / Page Menu

- The toolbar `Page Menu` button (`ID: AssistantButton`) exposes the
  Reader-mode / translate / summary affordances in modern Safari. It
  supports `AXPress`.
- The `Add page to Reading List` button (`ID: OneStepBookmarkingButton`)
  supports both `AXPress` (add to reading list) and `ShowMenu` (the full
  bookmark menu).

## Tab bar overflow

When a window has enough tabs that the bar overflows, trailing tabs may
report positions off-screen. `AXPress` still works on them -- do not
switch to a coordinate click in that case, or you'll miss.
