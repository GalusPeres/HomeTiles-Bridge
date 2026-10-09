# HomeTiles Bridge v0.9.0b27 (beta)

Open a camera straight in full screen from an automation (test, issue #65).

- The panel's view select lists every camera tile a second time with "(Full screen)" after its name. Selecting it opens that camera's full screen on the panel, for example from a doorbell automation; the select shows the full-screen option while it is open.
- The panel test firmware b331 sends these options in a separate list, so older Bridges keep their view select unchanged; older firmware sends none.
- Everything else from v0.9.0b26 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 641 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b26...v0.9.0b27
