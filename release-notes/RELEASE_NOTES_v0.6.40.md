# HomeTiles Bridge v0.6.40

Restart Home Assistant after installing this update.

> **Important:** Flash the matching HomeTiles firmware build to use the new
> full-resolution Binary Sensor timeline and textual sensor-state history.
> Older Binary Sensor firmware remains compatible and falls back to the
> bounded segment list; older firmware ignores the additive sensor metadata.

- Fixes busy 7-day Binary Sensor histories showing a large unknown prefix after
  the most recent 96 Recorder rows displaced older state changes.
- Reads real state changes in bounded Recorder pages across the requested
  period, ignoring attribute-only updates.
- Adds a compact, fixed-size full-range timeline while keeping recent Activity
  entries and the legacy segment fallback bounded to 96 items.
- Preserves short detected intervals in the compact timeline and reports an
  explicitly incomplete timeline if the Recorder safety cap is reached.
- Adds automatic `number` versus `state` metadata for ordinary `sensor.*`
  entities. Nonnumeric sensors such as a next-waste-collection sensor now use a
  bounded History/Activity response while numeric sensors retain their graph.
- Keeps the existing kindless numeric request/response and all prior sensor
  metadata fields unchanged. Binary `segments` and `activity` remain available
  and bounded alongside the additive timeline, so older firmware stays
  compatible when new fields are absent or ignored.
- Preserves textual live states including commas and Unicode, collapses
  attribute-only duplicate states, and transports a full-range 768-point
  `palette4-hex` timeline with at most 16 canonical state values. The raw,
  trimmed current state remains available for display and live comparison;
  palette overflow deliberately falls back to the safe unknown color.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.39...v0.6.40
