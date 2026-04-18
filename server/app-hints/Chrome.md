# Google Chrome

Chrome is Chromium-based, so the server automatically enables renderer
accessibility on first contact. That gives you the full webpage DOM (often
1000+ elements).

Key things to know:

## The DOM is fully there, but no AXScrollArea

Unlike Safari, Chrome exposes its web content as a root `HTML content`
element (role `AXWebArea`), not as an `AXScrollArea`. Links, buttons,
form fields, headings etc. are all there and respond to `AXPress` via
`click(element_index=...)` in the background.

## Scrolling web content

Chrome's renderer discards wheel events sent to its main pid when the
window isn't key, so the wheel-event path that works on Safari doesn't
work here.

The `scroll` tool works around this by calling AppleScript
``execute ... javascript`` against Chrome's active tab -- which runs
``window.scrollBy`` inside the page and is fully background-safe
(doesn't raise the window, doesn't move the cursor). This path is
automatic; just call
``scroll(app="Google Chrome", element_index=<any web element>, direction="down")``
and it will work as long as the tab is a normal webpage.

The JavaScript path always scrolls the top-level window (the page), not
a specific sub-scrollable region inside the page. If you need to scroll
a sub-scroller (e.g. a chat list that scrolls independently of the
page), targeting it through the AX tree isn't enough -- you'd want to
do it with a bespoke JS snippet via your own tool extension.

Fallbacks when AppleScript JS isn't available:

1. **Click a specific off-screen element**: find the target in the tree,
   then call `click(element_index=...)`. `AXPress` works background-safely
   on `AXLink` / `AXButton`.
2. **`perform_secondary_action(..., "ScrollToVisible")`** on an in-page
   element, when advertised.
3. **`press_key(key="PageDown")`**: works, but **will** bring Chrome
   forward as the key window, violating the background-use contract.
   Only use when the user has explicitly asked and acknowledged.

## Address bar

The URL bar is an `AXTextField` with description `Address and search bar`.
It accepts AX value writes directly (no typing fallback needed), so
`set_value(element_index=<url-bar>, value="https://...")` works in the
background. Because the default `submit=True` posts a Return after the
value write, navigation happens immediately.

## Tabs

Tab bar tabs are `AXTabButton` elements, each with `AXPress`. Switching
tabs via `click(element_index=<tab>)` works background-safely.
