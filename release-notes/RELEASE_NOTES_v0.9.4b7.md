# HomeTiles Bridge v0.9.4b7 (beta)

Seventh beta: the panel speaker as a small player (HomeTiles firmware beta b501 adds the matching panel side).

## Changes

- Home Assistant's player bar now works with the panel speaker: it shows the title, the position and the length of a clip, and offers pause and play. The title comes from the caller, a spoken message or the file name. A live stream has no length; only its time runs.
- Pause and play appear once the panel reports that it can pause (firmware b501). With older firmware the speaker keeps its former controls.

The speaker stays a small player for a microcontroller: no queue, no next or previous track. While it plays, the panel does not listen for its wake word.

## Update Notes

Beta: install through HACS with "Show beta versions". Firmware b501 also plays every MP3 (8 to 48 kHz) and keeps HTTPS downloads out of the panel's internal RAM.

## Validation

Automated tests passed (778), including the player bar with position, length, pause and title. Not yet checked in a running Home Assistant.
