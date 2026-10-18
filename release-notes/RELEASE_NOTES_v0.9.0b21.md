# HomeTiles Bridge v0.9.0b21 (beta)

Full-screen camera frames for the camera popup (test, issue #65).

- A panel can ask for camera frames in its own screen size and orientation: the whole picture with black bars (or filled), already turned the way the panel's display is mounted. The panel's JPEG decoder then writes each frame straight into the screen, sharp and edge to edge, without scaling or turning on the panel.
- The popup's normal stream (752 x 424) is unchanged; the popup and a full-screen view of the same camera run separate pipelines.
- Needs the panel test firmware b315 (Guition V2 for now); older firmware never asks for it.
- Everything else from v0.9.0b20 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 618 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b20...v0.9.0b21
