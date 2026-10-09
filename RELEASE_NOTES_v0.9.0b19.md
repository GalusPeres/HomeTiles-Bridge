# HomeTiles Bridge v0.9.0b19 (beta)

Fixes the screensaver picture of image entities that are not released in the Bridge options.

- A panel subscribes a newly chosen picture at once and tells the Bridge a moment later that it uses the entity. The Bridge dropped such a subscription, so the picture never arrived until the panel reconnected. It now keeps the subscription and sends the picture as soon as the panel's entities are known. The same applies to covers of newly chosen media players.
- When an entity is no longer served to a panel, its picture stops.
- The picture of an image entity is now read from the entity itself, like Home Assistant's image proxy, up to 12 MB. Before, it was downloaded through its address with a limit of 1.5 MB, so large pictures were dropped.
- Image entities that do not report the time of their picture (state "unknown") show their picture too.
- The Home Assistant log shows one line per picture: sent with its size, or why it waits or could not be loaded or rendered.
- Everything else from v0.9.0b18 is unchanged; panel test firmware b311 or newer.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 607 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b18...v0.9.0b19
