# HomeTiles Bridge v0.9.0b20 (beta)

Placement and refresh interval for the screensaver picture from Home Assistant.

- The panel chooses how the picture uses the screen: **Fill** (covers the screen, cuts what stands out, as before), **Fit** (the whole picture as large as possible, black bars where needed) or **Original** (a smaller picture at its own size, a larger one like Fit).
- Pictures with few colours, such as QR codes and vacuum maps, are enlarged with sharp edges instead of being blurred.
- The panel chooses how often a camera sends a still image, from 3 to 60 seconds (10 by default).
- Needs the panel test firmware b312 or newer for the new settings; older firmware keeps filling the screen every 10 seconds.
- Everything else from v0.9.0b19 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 611 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b19...v0.9.0b20
