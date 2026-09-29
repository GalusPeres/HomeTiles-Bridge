# HomeTiles Bridge v0.7.1b6 (beta)

Weather icons at night.

- Home Assistant reports "partly cloudy" or "sunny" at night too, so the panel showed a sun at night. The Bridge now sends the local sunrise and sunset of every forecast day with the weather. Panels with the new firmware show the moon variants of the icons at night.
- The times come from the home location set in Home Assistant. No new setting is needed, and the sun integration does not have to be enabled. Polar day and polar night are handled. Without a home location the Bridge sends no sun times, and the panel keeps its day icons.
- Older firmware ignores the new field.
- Everything else from v0.7.1b5 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 392 Bridge tests.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b5...v0.7.1b6
