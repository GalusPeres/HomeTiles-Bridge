# HomeTiles Bridge v0.9.0b17 (beta)

Sharp media covers for panels on the direct link, and the groundwork for more pictures from Home Assistant.

- The direct link can now carry messages above 64 KB from the Bridge to a panel, in pieces, while other messages keep flowing in between. A panel announces how much it accepts; panels without this support get nothing new.
- Media covers for linked panels: the Bridge renders the cover in exactly the size the panel shows it and sends it as its own picture in good quality. Before, a 240 px cover was squeezed into 14 KB inside the player state, which made detailed covers blocky.
- The player state names its picture with an "image_key", so a panel never shows a cover with the wrong song.
- A cover is rendered only for players the panel is served, and only while a panel shows it.
- Panels on MQTT and older firmware keep the cover inside the player state as before.
- The new covers need the panel test firmware b310 or newer.
- Everything else from v0.9.0b16 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 601 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b16...v0.9.0b17
