# HomeTiles Bridge v0.9.7b1 (beta)

Vacuums for the new Vacuum tile (#50 in the firmware repository).

## Changes

- **Vacuums list.** The options have a new "Vacuums" list; a panel with a Web Admin password finds every vacuum in its picker, like the other lists.
- **Vacuum state.** Each vacuum's retained detail state carries its state, its supported features, the fan speed and the speeds it offers, and the battery: the battery sensor of the vacuum's device (Home Assistant's way since 2025.8), else the older battery attribute. A missing value stays empty instead of 0.
- **Rooms.** The Home Assistant areas you mapped to the vacuum's segments (Home Assistant's "Clean area") come with the state, by their area names. A panel can clean the chosen rooms.
- **Map, area and time.** The image or camera entity of the vacuum's device is sent as its map and served to the panel with the vacuum. The device's current cleaned area and cleaning time sensors come with the state when the integration has them.
- **Safe commands.** Start, pause, stop, return to the dock, locate, spot clean, fan speed and clean rooms run only when the vacuum supports them; a fan speed must be one of its own, rooms must be mapped. Paired panels send these commands encrypted.

## Update Notes

Beta: install through HACS with "Show beta versions". The Vacuum tile comes with the next HomeTiles firmware. Room cleaning needs Home Assistant 2026.3 or newer and the area mapping in the vacuum's entity settings.

## Validation

Automated tests passed (876). Not yet checked in the maintainer's Home Assistant.
