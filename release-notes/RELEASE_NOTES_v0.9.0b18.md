# HomeTiles Bridge v0.9.0b18 (beta)

Pictures from Home Assistant for the screensaver of linked panels (issue #69).

- The screensaver of a panel on the direct link can show the picture of an image entity or a camera instead of its microSD slideshow. The source is chosen in the panel's Web Admin; no microSD card is needed.
- The Bridge fits the picture to the panel's screen and sends it in good quality, in pieces when it is large.
- A new picture of an image entity reaches the panel at once. A camera's still image is loaded every 10 seconds, and only while a panel's screensaver shows it.
- New list "Images (screensaver)" in the Bridge options to release image entities. Cameras released under "Cameras" work too.
- A picture is rendered only for entities the panel is served.
- Needs the panel test firmware b311 or newer; older firmware is unaffected.
- Everything else from v0.9.0b17 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 604 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b17...v0.9.0b18
