# HomeTiles Bridge v0.9.4b10 (beta)

Tenth beta: seeking in the panel speaker's player bar.

## Changes

- When you seek in Home Assistant's player bar, the bar now stays at the chosen position. Before, it jumped back to the old position for a moment until the panel reported the new one.

## Update Notes

Beta: install through HACS with "Show beta versions". Use it with HomeTiles firmware beta b503.

## Validation

Automated tests passed (798). Firmware b503 with Bridge b9 was checked on a Guition V2: every song played, the panel kept about 148 KB of its fast internal memory free while playing (169 KB before), and a camera full screen next to a song ran at 30 FPS. The seek bar change itself is not yet checked in a running Home Assistant.
