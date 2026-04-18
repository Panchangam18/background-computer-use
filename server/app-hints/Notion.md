# Notion

Notion is a block-based editor rendered inside a WebView. Accessibility
coverage is workable but has some Notion-specific rules you must respect.

## Blocks and Return

- Each line/paragraph/heading/bullet is a **block**. Pressing `Return` creates
  a new block of the **same type** — except inside list/toggle blocks, where
  a second consecutive `Return` on an empty bullet **exits** the list.
- To add a hard line break inside the same block, use `shift+Return`, never
  `Return`.
- To convert a block's type, type `/` at the start of the block and then the
  type name (`heading 1`, `code`, `todo`, `bullet`, `toggle`, etc.), then
  `press_key key="Return"` to accept the top suggestion.

## The slash menu is async

- After `type_text` sending `/`, **wait** and re-call `get_app_state` before
  clicking a menu item. The menu is not in the tree the instant you type the
  slash.
- Pick menu items by `click(element_index=...)` on the row, not by keyboard
  arrow + Return — arrow key navigation in the slash menu is inconsistent
  across Notion versions.

## Pages and sidebar

- Page rows in the sidebar usually advertise `ShowMenu` as a secondary
  action. Use `perform_secondary_action` to open the ellipsis menu rather
  than hovering to find the `…` button.
- Drag-and-drop to reorder pages works with `drag`, but the drop target is
  only valid when hovered for ~300 ms. Perform the drag slowly by stepping
  the coordinates if the first attempt fails.

## Text editing

- `set_value` is **not reliable** for most Notion editable regions because
  the underlying DOM is contenteditable rather than a native text input.
  Prefer: `click` the block, `press_key key="command+a"`, `type_text` the
  replacement.
- Inline formatting uses standard shortcuts: `command+b`, `command+i`,
  `command+u`, `command+shift+s` (strikethrough), `command+e` (inline code).

## Tables and databases

- Database row cells behave like Numbers: single-click selects, double-click
  edits. Always double-click before typing.
- To add a row, `click` the `+ New` button in the bottom-left of the table,
  then re-read state before interacting with the new row.
