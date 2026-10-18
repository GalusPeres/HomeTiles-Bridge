# HomeTiles Bridge v0.9.1b1 (beta)

Energy sources in the panel's entity picker.

- The Energy tile's source picker in the panel's Web Admin now finds the sources of the Home Assistant Energy dashboard on every panel, also on a panel without an Energy tile. With a Web Admin password on the panel it lists every source; without one, the categories enabled under Energy Dashboard in the Bridge options. This needs the matching panel firmware; older firmware keeps its current list.
- Everything else from v0.9.0 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 660 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0...v0.9.1b1
