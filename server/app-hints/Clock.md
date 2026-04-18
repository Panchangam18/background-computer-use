# Clock

Apple's Clock app has four tabs: **World Clock**, **Alarms**, **Stopwatch**,
**Timers**. Always call `get_app_state` after switching tabs — the
sidebar replaces the main view entirely and your previous indexes become
stale.

## World Clock

- Add a city with the `+` button in the toolbar. Type the city into the search
  sheet and `click` the first result — do **not** press Return, that sometimes
  dismisses the sheet without adding the row.
- The running digital clock for each city appears as a `static text` element.
  Its value updates between snapshots; do not treat equality across turns as
  a failure.

## Alarms

- Alarms are list rows. Toggle one with `click` on the embedded `switch`
  element, not on the row itself.
- When editing an alarm, the time picker is three scrollable pickers (hours,
  minutes, AM/PM). Use `set_value` on each picker if `settable=true`; fall
  back to `scroll` otherwise.

## Stopwatch

- Start/stop is a single button whose title toggles between `Start`, `Stop`,
  and `Resume`. Re-read state after each press.
- `Lap` is only enabled while running.

## Timers

- The big-picker screen has three wheels (Hours, Minutes, Seconds). These are
  usually settable via `set_value` with a string like `"5"`.
- Created timers live in the sidebar. `perform_secondary_action` with
  `"Delete"` or `"Pause"` is often faster than navigating controls.
