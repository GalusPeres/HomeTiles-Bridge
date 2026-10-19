# HomeTiles Bridge v0.9.4b1 (beta)

Beta for the reworked Media popup (HomeTiles firmware beta of the same day).

## New

- Media players send what the new popup needs: their feature bits, shuffle, repeat, the source and sound mode lists and the group with the players it can be grouped with. Only players this panel already has as Media entities, of the same integration and able to group, are offered for grouping.
- New media commands from the panel: shuffle, repeat, sound mode and joining or leaving a group. Each is checked against what the player supports and offers; a source or sound mode must be one of its lists.
- The library: the panel browses the player's media like Home Assistant's media browser and starts a track, playlist or station. Thumbnails come from this Bridge as small JPEGs, only for pictures it listed itself. Based on memooox3's media browse work (#11).

## Fixes

- The View select keeps its options while a new catalog arrives, so Home Assistant no longer warns about changing capabilities (#17).

## Update Notes

Beta: install through HACS with "Show beta versions". Panels without the new firmware keep working; they ignore the new fields.

## Validation

Automated tests passed, including new tests for the control commands, the group checks and the library pages. Not tested with real players yet.
