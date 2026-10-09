# HomeTiles Bridge v0.9.0b22 (beta)

Faster full-screen camera frames (test, issue #65).

- A panel can ask for two 8 KB chunks in flight instead of one. With one chunk the full-screen stream stopped at about 9.4 Mbit/s, so large frames (50-70 KB) reached only 15-22 FPS; two chunks in flight aim for 30 FPS.
- Only a panel that asks for it gets two chunks (the panel test firmware b317 does so for the full screen). The popup's stream and every older firmware keep one chunk, as before.
- Everything else from v0.9.0b21 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 622 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b21...v0.9.0b22
