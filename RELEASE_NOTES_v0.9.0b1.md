# HomeTiles Bridge v0.9.0b1 (beta)

Direct connection to the panels, without an MQTT broker.

- Panels with link support connect to the Bridge over TCP port 8140 instead of MQTT. The Bridge passes the same messages as before, and every message is encrypted with the key from pairing.
- Adding such a panel takes one dialog: Add, the panel restarts, the panel and Home Assistant show the same six-digit number, confirm on both. No MQTT host, user or password.
- The MQTT integration is now optional. Panels on MQTT keep working unchanged, and panels with older firmware are still added through MQTT.
- The direct link needs HomeTiles firmware that is not released yet. With the current firmware nothing changes.
- Home Assistant in Docker without host networking must publish TCP port 8140, like the camera ports 8124-8131.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 527 Bridge tests. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.8.0...v0.9.0b1
