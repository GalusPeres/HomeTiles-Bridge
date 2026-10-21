# HomeTiles Bridge v0.9.4b12 (beta)

Twelfth beta: camera sound closer to the picture (pairs with HomeTiles firmware beta b505).

## Changes

- The Bridge now passes a camera's sound to the panel as soon as FFmpeg has decoded it, instead of collecting a full 4 KB block first (up to 93 ms of sound).

Firmware b505 shortens the panel side as well: it keeps 120-250 ms of camera sound instead of 300-600 ms, and its speaker buffer for camera sound is 44 ms instead of 174 ms. In the first test the sound ran audibly behind the picture.

## Update Notes

Beta: install through HACS with "Show beta versions". Use it with HomeTiles firmware b505.

## Validation

Automated tests passed (806), including a camera sound that arrives in small pieces. Exact lip sync is not attempted; not yet checked with a real camera.
