# HomeTiles Bridge v0.9.8b4 (beta)

The rain radar opens faster.

## Changes

- **The first picture comes at once.** FFmpeg held the radar's first picture back for half a second. It now comes right after the stream starts (measured on a desktop PC: 23 ms instead of 505 ms).

## Update Notes

Beta: install through HACS with "Show beta versions". Goes with firmware b664, which also opens the popup without the "Preparing stream" text and plays the radar in a loop.

## Validation

Automated tests passed. Not yet checked in the maintainer's Home Assistant or on a panel.
