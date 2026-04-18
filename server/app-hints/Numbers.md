# Numbers

Numbers exposes a rich accessibility tree but has some brittle editing
semantics. Read these before automating it.

## Selecting vs. editing a cell

- A single `click` on a cell **selects** it. The cell is highlighted but not
  editable — typing will replace the contents but cancel formulas.
- A `click(click_count=2)` (double-click) or `press_key key="Return"`
  **enters edit mode**. Always enter edit mode before typing a formula that
  starts with `=`.
- A `click(click_count=3)` (triple-click) selects the cell's entire current
  value for replacement. This is the fastest way to overwrite a cell.

## Typing discipline

- `type_text` chunks characters; for cells, prefer shorter inputs per call
  (one cell's worth). Batching many cells into one `type_text` risks losing
  keystrokes during cell-to-cell transitions.
- Move between cells with `press_key` using `Tab` (right), `shift+Tab`
  (left), `Return` (down), and `shift+Return` (up). Do **not** embed those
  semantics as literal characters via `type_text`.
- Commit a formula with `press_key key="Return"`; `type_text` ending in a
  newline sometimes leaves the formula open.

## Selection and ranges

- To select a range, use `drag` from the top-left cell's center to the
  bottom-right cell's center. Accessibility exposes each cell but not a
  range primitive.
- The **Format** inspector on the right is a sidebar whose controls mostly
  have `settable=true`. Prefer `set_value` over clicking + typing for
  numeric inputs (row height, column width, font size).

## Sheets and tables

- Sheet tabs live at the top. Switch with a single `click` on the sheet row,
  then **re-call `get_app_state`** before interacting — the entire tree
  changes.
- A workbook with multiple tables per sheet exposes each table as a separate
  collection; always check the `description` field to pick the right one.
