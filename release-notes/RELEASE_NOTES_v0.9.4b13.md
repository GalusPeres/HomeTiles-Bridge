# HomeTiles Bridge v0.9.4b13 (beta)

Thirteenth beta: camera picture and sound in step (pairs with HomeTiles firmware beta b506).

## Changes

- When the Bridge starts a camera's sound on a panel, it now also tells the panel how much earlier the sound leaves the Bridge than the matching picture. It works this out from the camera's frame rate and its sound format, which it reads once when the camera opens.
- With firmware b506 the panel then holds each picture back until its sound reaches the loudspeaker, so lips and voice match. The picture comes about 0.1-0.25 s later than without sound. Cameras without sound and panels without a speaker show the picture without any delay, as before.

## Update Notes

Beta: install through HACS with "Show beta versions". Use it with HomeTiles firmware b506.

## Validation

Automated tests passed (808), including the frame rate and sound format of RTSP cameras with AAC and G.711 sound. Not yet checked with a real camera.
