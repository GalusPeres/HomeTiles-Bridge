# HomeTiles Bridge v0.9.3

Home Assistant restarts again while a panel is connected.

## Fixes

- Restarting Home Assistant from its UI hung while a panel was connected over the direct link; it only came back after the panel was restarted or Home Assistant was stopped by hand (#15). The Bridge now closes the panel connections before it stops its link server and waits at most three seconds for them. The camera server stops the same way.
- Stopping Home Assistant no longer logs "Unable to remove unknown job listener" for the weather updates.

## Update Notes

Update through HACS and restart Home Assistant. Nothing changes on the displays; HomeTiles v0.9.3 and older firmware keep working.

## Validation

735 automated tests passed, including new tests that stop the link and camera servers while a panel is still connected. A restart of a real Home Assistant with a connected panel has not been tested by the maintainer yet.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.2...v0.9.3
