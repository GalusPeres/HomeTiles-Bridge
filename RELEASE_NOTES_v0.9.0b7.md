# HomeTiles Bridge v0.9.0b7 (beta)

No more dead entries from old MQTT announcements.

- A panel that moved to the direct link left its last MQTT announcement on the broker. After a restart, Home Assistant offered it as a new panel, and Add created an entry the panel never received. The Bridge now deletes that old announcement on MQTT, and removes such a card as soon as the panel shows that it no longer uses MQTT.
- Pair on the panel replaces an old MQTT card for the same panel, so the pairing card appears.
- Everything from v0.9.0b6 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 544 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b6...v0.9.0b7
