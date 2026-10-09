# HomeTiles Bridge v0.9.0b25 (beta)

Sharper camera pictures on the ESP32-S3 panels (test, issue #65).

- A panel may name its own JPEG quality when it opens a camera. The ESP32-S3 panels (480 x 480) decode only a few small frames per second and use a fraction of their network, so the Bridge's fixed quality looked blocky there (about 7 KB per frame). The S3 test firmware b329 asks for a better quality; the frames become about two to three times larger.
- Panels that do not ask (every ESP32-P4 panel and older firmware) keep the Bridge's quality as before, so their 30 FPS stream is unchanged.
- Everything else from v0.9.0b24 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 638 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b24...v0.9.0b25
