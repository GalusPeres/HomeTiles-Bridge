# HomeTiles Bridge v0.9.0b12 (beta)

The entity picker loads further matches while you scroll.

- Instead of the line "More matches: refine the search", the picker in the panel's Web Admin asks the Bridge for the next 60 matches when you scroll to the end of the list, so you can scroll through every entity like in Home Assistant.
- Icons are still loaded only for the entities that are shown.
- Everything from v0.9.0b11 is unchanged. Panel firmware without this picker is unaffected.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 568 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b11...v0.9.0b12
