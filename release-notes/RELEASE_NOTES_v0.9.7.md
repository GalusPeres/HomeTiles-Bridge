# HomeTiles Bridge v0.9.7

Vacuums for the new Vacuum tile, and today's energy bars that follow the running hour.

## Changes

- **Vacuums:** a new **Vacuums** selection in the entity configuration; a panel with a Web Admin password finds every vacuum in its picker. Each vacuum's retained detail state carries its state, supported features, fan speed and the speeds it offers, and the battery (the battery sensor of the vacuum's device, else the older battery attribute). A missing value stays empty instead of 0.
- **Rooms:** the Home Assistant areas mapped to the vacuum's segments come with the state by their area names, and a panel can clean the chosen rooms (`vacuum.clean_area`).
- **Map, area and time:** the image or camera entity of the vacuum's device is sent as its map and served to the panel as a picture; the device's cleaned area and cleaning time come with the state when the integration has them.
- **Safe commands:** start, pause, stop, return to the dock, locate, spot clean, fan speed and clean rooms run only when the vacuum supports them; a fan speed must be one of its own, rooms must be mapped. Paired panels send these commands encrypted.
- **Pictures:** a panel can name the color behind a picture; transparent parts and the bars of a fitted picture then take that color instead of black.
- **Energy today:** Home Assistant writes an hour's statistics only once the hour is over. The running hour now comes from the 5-minute statistics, so the Energy tile's bars and today's total follow it every five minutes.

## Update Notes

Update through HACS and restart Home Assistant, then update the displays to HomeTiles v0.9.7. The Vacuum tile needs v0.9.7 on the display. Cleaning single rooms needs Home Assistant 2026.3 or newer and the area mapping in the vacuum's entity settings.

## Validation

879 automated tests passed. The vacuum part was tested with HomeTiles test builds on the Guition JC8012P4A1 V2 with a vacuum that has a camera map. The Energy change is not yet checked in the maintainer's Home Assistant.

Thanks to everyone reporting.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.6...v0.9.7
