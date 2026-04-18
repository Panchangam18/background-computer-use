# Apple Music

## Search

- The search field lives in the top-left sidebar area. It is a `text field`
  with the placeholder `Search`. Prefer `set_value` over `type_text` when
  replacing an existing query — `type_text` appends to the current value.
- After setting the search value, `press_key key="Return"` to execute the
  search. Results only populate after Enter.

## Queue and playback

- Play / Pause is a single toolbar button whose title toggles between
  `Play` and `Pause`. Re-read state after each press — the button's
  `element_index` changes between snapshots.
- The Up Next queue is reachable via `perform_secondary_action` with
  `"ShowMenu"` on the queue button, or the shortcut `press_key key="option+command+u"`.
- Add a track to the queue with `perform_secondary_action index "ShowMenu"`
  on the track row, then `click` the matching menu item. Re-read state
  between the menu open and the menu click.

## Scrolling long lists

- Library rows use a `collection` (list) view. Use the `scroll` tool against
  the collection's `element_index`, not the window — scrolling the window
  may move the sidebar instead.
- For jumping to a specific row, try
  `perform_secondary_action index "ScrollToVisible"` first; that avoids
  having to estimate pages.

## Volume

- The volume slider is a `slider` element with `settable=true`. Use
  `set_value` with a float-compatible string in `"0"`..`"1"`; do not use
  the keyboard arrow keys, they change playback position instead.
