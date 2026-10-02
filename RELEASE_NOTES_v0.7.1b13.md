# HomeTiles Bridge v0.7.1b13 (beta)

Media covers from a source without artwork.

- A media player without a picture, for example a Sonos speaker switched to its TV input, kept showing the cover of the last song on the panel (#16). Once a player has had no artwork for three seconds, the Bridge now tells the panel, and the tile shows its layout without a cover like Home Assistant does.
- Short gaps between two songs, buffering, unavailable and unknown players keep the cover, so the tile does not jump during a song change.
- Covers can still come back after a folder change or a reload on panels until the matching firmware update.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 495 Bridge tests.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b12...v0.7.1b13
