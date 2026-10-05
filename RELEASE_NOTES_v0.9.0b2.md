# HomeTiles Bridge v0.9.0b2 (beta)

Camera popup restart from the newest still image.

- A camera that only delivers still images could resume from the second newest image after FFmpeg restarted: an image that arrived at the very moment the restart began was dropped. The Bridge now keeps it.
- Everything from v0.9.0b1 (direct connection to the panels without an MQTT broker) is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 528 Bridge tests, ten full runs without a failure. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b1...v0.9.0b2
