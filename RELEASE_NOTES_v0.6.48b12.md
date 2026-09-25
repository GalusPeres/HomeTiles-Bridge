# HomeTiles Bridge v0.6.48b12 (beta)

Beta for climate devices with their own fan and swing mode names (issue #43).

- Fan, swing and horizontal swing modes are matched without regard to upper and lower case, and Home Assistant receives the exact name the device uses. Modes such as "Silent" or "Swing Up-Down" can now be selected from a display.
- The display side of this fix (showing all modes, for example 1, 2, 3) needs the next HomeTiles firmware; with older firmware the display still offers only the modes it knows.
- Everything from v0.6.48b11 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 268 Bridge tests pass. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b11...v0.6.48b12
