# HomeTiles Bridge v0.6.32

This update prepares the Bridge for the local hardware I/O support in
HomeTiles firmware v0.6.4. Updating the Bridge before the firmware is
recommended.

## Changes

- Migrates integration-generated local switch and temperature entity IDs from
  older channel-based names to the current firmware-announced names.
- Preserves entity IDs that were renamed manually in Home Assistant.
- Preserves Home Assistant collision suffixes such as `_2` and `_3` when
  several identical panels announce the same preferred entity ID.
- Validates and deduplicates firmware-provided `legacy_entity_ids` before using
  them for a registry migration.
- Keeps the internal channel ID, MQTT topics and Home Assistant `unique_id`
  stable while the visible entity ID follows the configured channel name.

No MQTT topic changes are required. Existing firmware without local hardware
I/O announcements remains compatible.
