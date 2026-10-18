# HomeTiles Bridge v0.9.1b3 (beta)

Energy data only as released.

- Without a Web Admin password on the panel, the Bridge sends energy values only for the categories enabled under Energy Dashboard in the Bridge options (Electricity, Gas, Water), like every other entity. Before, the energy answer held every source of the Home Assistant Energy dashboard, also with nothing enabled. With a Web Admin password on a paired panel the panel gets every source as before.
- Energy tiles of sources outside the enabled categories show no value without a Web Admin password. Enable the category or set a Web Admin password on the panel.
- Everything else from v0.9.1b2 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 663 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.1b2...v0.9.1b3
