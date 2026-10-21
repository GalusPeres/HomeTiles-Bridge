# HomeTiles Bridge v0.9.4b11 (beta)

Eleventh beta: camera sound on the panel (pairs with HomeTiles firmware beta b504).

## Changes

- When you open a camera on the panel and its stream carries sound, the panel's speaker now plays that sound, in the camera popup and in full screen. Switching between the two keeps the sound running; closing the camera ends it.
- There is no button yet: cameras with sound always play it. Cameras without sound, still-image cameras and the panels' own built-in cameras stay silent.
- The sound uses the panel speaker like any clip, so a song that is playing stops. The panel keeps the sound short (well under a second) so it stays close to the picture; exact lip sync is not attempted.

## Update Notes

Beta: install through HACS with "Show beta versions". Camera sound needs HomeTiles firmware b504 or newer; older firmware does not ask for it.

## Validation

Automated tests passed (805), including the camera open, the switch to full screen, closing, and cameras without sound. The check for a sound track was run locally with the real FFmpeg. Not yet checked with a real camera in a running Home Assistant.
