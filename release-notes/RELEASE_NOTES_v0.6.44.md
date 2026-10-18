# HomeTiles Bridge v0.6.44

Fixes entity icons disappearing after a configuration sync or delayed Home Assistant startup.

- Refreshes the icon cache from current entity states and registry overrides before publishing it. A stale empty startup cache can no longer overwrite newer icons from the configuration payload.
- Preserves configured user icons and intentional icon removal. MQTT payloads and legacy firmware support remain unchanged.

Update through HACS and restart Home Assistant. The accompanying HomeTiles firmware test build additionally refreshes automatic icons in an already open editable popup and corrects local editing and popup layout behavior.

All 119 Bridge regression tests pass, including delayed entity availability, stale cache replacement, registry overrides and icon removal. End-to-end confirmation on the display remains a hardware test.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.43...v0.6.44
