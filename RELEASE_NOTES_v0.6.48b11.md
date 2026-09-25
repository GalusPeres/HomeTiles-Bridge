# HomeTiles Bridge v0.6.48b11 (beta)

Beta that tightens what a display may do through the Bridge.

- Light, climate, media player and camera commands are only carried out for entities selected in the Bridge options, as switches and covers already were. A tile whose entity was removed from the Bridge selection no longer controls it.
- History and weather refresh requests are only answered for selected entities.
- A display that announces itself over MQTT without an existing entry now appears under Settings > Devices & services > Discovered and is only added after you confirm it. Setup through the regular discovery card is unchanged, and existing displays keep working.
- Entity lists sent by a display must match their type (for example only lights in the light list), and scene targets must be scenes, scripts or buttons.
- A display can no longer take over the entry of another display that uses the same MQTT base topic.
- Everything from v0.6.48b10 is unchanged.

No firmware update is required.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 267 Bridge tests pass. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b10...v0.6.48b11
