# HomeTiles Bridge v0.6.39

Restart Home Assistant after installing this update.

> **Important:** Flash a HomeTiles firmware build with the dedicated Binary
> Sensor tile (type 20) before upgrading the bridge. Older firmware does not
> understand the new structured binary-state payload.

- Adds a dedicated Binary Sensor selector and automatically migrates existing
  `binary_sensor.*` selections from the generic Sensor list.
- Publishes retained state, availability, device class, last-change time and the
  current Home Assistant icon without conflating `unknown`, `unavailable` and a
  missing entity.
- Clears stale retained state with an explicit JSON-null tombstone when a
  configured entity disappears.
- Adds bounded 24-hour and 7-day Recorder history for the Binary Sensor timeline
  and activity list; numeric Sensor history remains unchanged.
- Updates the tag release workflow to the supported Node 24 release action.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.38...v0.6.39
