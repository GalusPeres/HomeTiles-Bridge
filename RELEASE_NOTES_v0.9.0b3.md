# HomeTiles Bridge v0.9.0b3 (beta)

Pairing starts on the panel, and panels on MQTT can switch to the direct connection.

- A panel is added by pressing Pair on the panel (Settings > System > Security). For two minutes it shows up in Home Assistant; Add, compare the six-digit number, confirm on both. No Web Admin password is asked: the panel accepts the Bridge only within these two minutes.
- A panel that is already set up over MQTT switches to the direct connection the same way. Its entry, area, names and entities stay.
- Fixed: on a panel that had moved from MQTT to the direct connection, the view select, the panel camera and other entities that follow the panel's online state were unavailable. The MQTT broker still held the panel's old offline message; the Bridge now ignores MQTT for panels on the direct connection.
- The direct connection needs HomeTiles firmware that is not released yet. With the current firmware nothing changes.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 530 Bridge tests, ten full runs without a failure. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b2...v0.9.0b3
