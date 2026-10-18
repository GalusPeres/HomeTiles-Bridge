# HomeTiles Bridge v0.9.1

Energy sources in the display's entity picker, and energy data only as released.

## Highlights

- The Energy tile's picker on the display finds the sources of the Home Assistant Energy dashboard through the Bridge, with the icon the tile shows (solar, grid, battery, gas, water, costs).
- Energy data follows the same rule as every other entity. Without a Web Admin password on the display, the Bridge sends only the categories enabled under Energy Dashboard in the Bridge options (Electricity, Gas, Water). With a Web Admin password on a paired display it sends every source.

## Update Notes

Before, the Bridge sent every energy source to every display, also with nothing enabled under Energy Dashboard. After the update, Energy tiles on a display without a Web Admin password show values only for the enabled categories. Enable the categories you use in the Bridge options (Energy Dashboard), or set a Web Admin password on a paired display.

Update through HACS, restart Home Assistant and reload the browser page. HomeTiles v0.9.1 uses the energy search; displays with older firmware keep working.

## Validation

663 automated tests passed. The energy search and the energy rule were tested with HomeTiles displays during the v0.9.1 beta series.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0...v0.9.1
