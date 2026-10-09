# HomeTiles Bridge v0.9.0b26 (beta)

A faster camera full screen on the ESP32-S3 panels (test, issue #65).

- A panel may ask for full-screen frames that hold only the picture, as large as it fits, without black bars ("inside"). The ESP32-S3 panels decode in software, and with the bars almost half of every 480 x 480 frame was black; the panel now draws the black around the picture itself. Nothing of the picture is cut.
- The S3 test firmware b330 asks for this. ESP32-P4 panels keep frames in their exact screen size with the bars, as their hardware decoder writes them straight to the screen.
- Everything else from v0.9.0b25 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 639 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b25...v0.9.0b26
