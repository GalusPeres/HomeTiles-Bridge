# HomeTiles Bridge v0.8.0

Adds locks, alarm panels and fans, encrypted commands, faster sensor graphs and shared camera streams for HomeTiles v0.8.0.

## Highlights

- New **Fans**, **Locks** and **Alarm panels** in the entity configuration for the new display tiles.
- Locks and alarm panels work only from an encrypted display with a Web Admin password. Home Assistant checks every code; for devices that ignore wrong codes, the Bridge can check them.
- Encrypted commands: pair a display by comparing a six-digit number.
- 7-day graphs load much faster from Home Assistant's statistics and are no longer cut off.
- Displays showing the same camera share one stream at up to 30 FPS, and large streams keep up on slower hosts.
- Night icons for the weather, and no old Media cover for a player without artwork.

## Update Notes

Update through HACS, restart Home Assistant and reload the browser page before updating displays to HomeTiles v0.8.0. Existing configurations stay valid; older firmware keeps working unencrypted.

## Validation

495 automated tests passed. Locks, alarm panels, fans, encryption and camera streams were tested with HomeTiles displays during the beta series.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.0...v0.8.0
