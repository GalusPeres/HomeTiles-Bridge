# HomeTiles Bridge v0.9.0b23 (beta)

Quicker camera switching and up to four chunks in flight (test, issue #65).

- A camera pipeline keeps running for 30 seconds after the last panel closed it. A panel switching between the popup and the full screen, or opening the camera again, joins the running pipeline at once instead of waiting about a second for a new FFmpeg and the camera's first frame.
- A panel may ask for up to four 8 KB chunks in flight (two before). The panel test firmware b318 asks for four in the full screen; two reached 11.9 Mbit/s on a Guition V2. The popup's stream and older firmware keep one chunk.
- Everything else from v0.9.0b22 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 633 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b22...v0.9.0b23
