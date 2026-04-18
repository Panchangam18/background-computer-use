# Spotify

Spotify's Mac app is an Electron/WebView hybrid, which means accessibility
coverage is **partial**. Expect to mix element indexes with coordinate
fallbacks.

## Playback state lag

- The UI often lags the actual playback state by 300–800 ms after a key press
  or click. Do **not** re-read state immediately after a playback change and
  treat the stale value as authoritative. If you need to confirm state,
  wait a short beat before calling `get_app_state` again.
- The Play/Pause button's `title` toggles between `Play` and `Pause`. Trust
  the most recent post-action snapshot, not the pre-action one.

## Search

- The search box is a standard `text field` in the left sidebar / search
  page. `set_value` usually works; if it silently fails (a known
  WebView quirk), fall back to `click` on the field, then
  `press_key key="command+a"` and `type_text` the new query.
- Submit with `press_key key="Return"`.

## Tracks and queue

- Track rows expose `perform_secondary_action` with `ShowMenu` — use it to
  access "Add to Queue", "Add to Playlist", and "Copy Song Link". Menus are
  themselves not in the original tree; re-read state after the menu opens.
- Play a specific row by `click(click_count=2)` (double-click) on the row.
- Like/unlike is a `toggle button` that shows as `selected=true` when liked.

## Shortcuts that do not need accessibility

When accessibility is flaky, these xdotool-style keys usually work:

- `space` — play / pause
- `command+Right` — next track
- `command+Left` — previous track
- `command+Up` / `command+Down` — volume up / down

Prefer these over coordinate clicks when you just need transport control.
