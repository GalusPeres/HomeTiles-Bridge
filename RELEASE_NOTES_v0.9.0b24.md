# HomeTiles Bridge v0.9.0b24 (beta)

A faster camera stream for every panel (test, issue #65).

- A panel may ask for 32 KB chunks instead of 8 KB (still at most two in flight, so never more than 64 KB on its way). Frames now follow each other without waiting for the last acknowledgements of the previous frame, and the send buffer holds every chunk in flight (before, an 8 KB buffer made each chunk wait for the one before).
- Measured on a Guition V2: the panel receives 28 Mbit/s, while one 8 KB chunk at a time held the camera at about 12 Mbit/s. The panel test firmware b320 asks for the fast transport on every stream and falls back to 8 KB by itself after a transport error or a restart during a fast stream.
- Panels that do not ask (older firmware) keep 8 KB chunks as before.
- Everything else from v0.9.0b23 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 635 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b23...v0.9.0b24
