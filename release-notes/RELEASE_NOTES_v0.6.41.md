# HomeTiles Bridge v0.6.41

Restart Home Assistant after installing this update through HACS.

- Adds a **View / Ansicht** select to the existing display device when matching
  firmware announces navigation support. Home, nested folders and configured
  popup tiles can be selected from Home Assistant or an automation.
- Uses persistent target IDs, distinct labels and device-confirmed state.
  Manual navigation and popup closing update the select. Commands are
  non-retained, expire after five seconds and are bound to a connection session.
- Keeps existing firmware compatible: all previous entity, history and command
  contracts remain available. Older firmware does not receive navigation
  commands or gain an unusable View entity.
- Creates battery and external-temperature entities according to announced
  capabilities. Preserves supported legacy Tab5 telemetry and configured local
  temperature channels; known panels without battery measurement no longer
  receive synthetic battery entities.
- Repairs integration-owned stale entity-registry entries and saved sensor
  selections on setup. Stops rewriting internal sensors to global Tab5 aliases,
  preserves user entities and user-assigned names, and separates configured
  sensors from runtime-added sensors during MQTT synchronization.
- Includes executable regressions for navigation feedback, offline/reconnect
  behavior, expiry, existing-installation cleanup and fresh pairing.

The navigation feature requires the accompanying new HomeTiles firmware.
Firmware test builds for Waveshare 8-inch and Guition ESP32-4848S040 are local;
end-to-end Home Assistant and physical-display validation remains pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.40...v0.6.41
