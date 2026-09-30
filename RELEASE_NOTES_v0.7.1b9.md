# HomeTiles Bridge v0.7.1b9 (beta)

Camera popups at up to 30 pictures per second.

- Panels may now ask for camera streams at up to 30 FPS instead of 24. Panel firmware from the next beta asks for 30; with an older Bridge it falls back to 24 on its own.
- Older panel firmware keeps asking for 24 and is unchanged.
- The Bridge keeps only the frames a panel shows before scaling (since v0.7.1b8), so 30 FPS adds little work on the Home Assistant host.
- Everything else from v0.7.1b8 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 405 Bridge tests.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b8...v0.7.1b9
