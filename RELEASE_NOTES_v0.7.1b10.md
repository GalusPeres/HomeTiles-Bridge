# HomeTiles Bridge v0.7.1b10 (beta)

Locks, alarm panels and fans for the coming panel firmware, and one camera stream per camera.

- New fields in the entity configuration: Fans, Locks, Alarm panels and "Allow opening without a code". Panels need a newer firmware to show these tiles; until then the fields only prepare the setup.
- Locks and alarm panels can only be operated from an encrypted (paired) panel with a Web Admin password, and only through encrypted commands. Home Assistant checks every code, as in its own dashboard. Unlocking, opening or disarming without a code works only for entities listed under "Allow opening without a code"; anyone at the panel can then do so.
- Per panel and entity, one command with a code runs at a time and at most ten per minute. After five wrong codes, code entry is blocked for 30 seconds, doubling with every further wrong code up to one hour, and Home Assistant shows a notification. The Bridge never logs or stores a code.
- Fans: on/off, speed, preset, oscillation and direction, each only when the fan supports it. Fans stay selectable as switchable entities as well.
- Panels that show the same camera now share one camera stream (one FFmpeg process and one RTSP connection) instead of starting one each. A slow panel skips frames without holding back the others.
- Existing configurations, topics and tiles are unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 479 Bridge tests. Two panels on one live RTSP camera used one FFmpeg process; both received 134 frames in six seconds.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b9...v0.7.1b10
