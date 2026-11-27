# HomeTiles Bridge v0.9.9b1 (beta)

A sensor tile no longer keeps an older value. Works with firmware v0.9.8.

## Changes

- **No more stale values.** The Bridge sends its configuration again now and then, for example a few seconds after an entity was briefly unavailable, and the configuration carries every sensor's value. The Bridge read those values first and then waited for the Energy dashboard's sources. A sensor that changed in that wait reached the panel first, and the older value from the configuration replaced it. A temperature that then stayed the same kept the wrong value on its tile for hours or days, until a restart. The Bridge now waits first and reads the values right before it sends them.

## Update Notes

Beta: install through HACS with "Show beta versions".

## Validation

Automated tests passed (1015), with a new test that replays the change during the wait. Not yet checked in the maintainer's Home Assistant or on a panel.
