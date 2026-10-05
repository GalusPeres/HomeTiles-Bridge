# HomeTiles Bridge v0.9.0b8 (beta)

The old MQTT card of a panel on the direct link stays away, also right after a restart.

- v0.9.0b7 could still show the card after a restart: Home Assistant runs the discoveries of the start together, and the old MQTT announcement could come after the panel's mDNS announcement. Now the Bridge also checks Home Assistant's mDNS cache before it opens a card for an announcement from before the start. A panel that shows itself there as new, without MQTT, gets no card, and its old announcement is deleted on the broker.
- A panel that announces itself over MQTT right now still gets its card as before.
- Everything from v0.9.0b7 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 548 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b7...v0.9.0b8
