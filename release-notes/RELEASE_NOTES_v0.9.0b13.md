# HomeTiles Bridge v0.9.0b13 (beta)

Tiles with entities you did not release in the Bridge now work reliably.

- With firmware b236 or later the panel tells the Bridge every entity its tiles use, like an ESPHome device does, and the Bridge serves them when the panel is paired and has a Web Admin password. Fixed: a Select tile with such an entity lost its value and options every few seconds, because panel and Bridge kept undoing each other.
- The Bridge confirms what it received, and the panel repeats it until it is confirmed.
- After a Home Assistant restart these tiles work right away. The list is kept for the panel's pairing and is deleted when the panel is removed.
- A single invalid entity on a tile no longer affects the others.
- Panels with older firmware and unpaired panels are unaffected. Everything else from v0.9.0b12 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 572 Bridge tests pass, including runs of panel and Bridge together through panel and Home Assistant restarts, tile changes and password changes. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b12...v0.9.0b13
