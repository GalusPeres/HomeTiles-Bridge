# HomeTiles Bridge v0.6.43

Adds Number, Select and Date/Time entity support for the new HomeTiles tile controls, including Recorder history and Activity.

- Configure number/input_number, select/input_select and time/date/datetime/input_datetime entities in the integration's entity settings.
- Publishes current values, availability, numeric bounds/steps/units, selection options and supported date/time fields. Numeric history includes a graph and Activity; selections include a state timeline and Activity; date/time controls include Activity.
- Validates commands against the configured entity, its current capabilities and a fixed Home Assistant service allow-list. Retained, expired, duplicate and outdated commands are rejected. Date/time handling uses the Home Assistant timezone and validates daylight-saving transitions.
- Preserves older firmware and existing configurations: the new lists are optional, control metadata uses additive MQTT topics, and legacy state/history contracts remain available.

Update through HACS and restart Home Assistant. The new controls require the accompanying HomeTiles firmware test build; existing firmware can continue using its current features. Select the new entities in the Bridge configuration before assigning their tiles in HomeTiles Web Admin.

All 118 Bridge regression tests pass, covering old/new configurations, supported domains, bounds, availability, command validation, Recorder gaps and timezones. The firmware's 78 host tests and incremental Waveshare 8-inch build also pass. End-to-end Home Assistant/display behavior, touch interaction and sleep/reconnect behavior still require hardware testing.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.42...v0.6.43
