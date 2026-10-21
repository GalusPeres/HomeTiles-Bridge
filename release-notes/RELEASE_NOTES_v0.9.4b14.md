# HomeTiles Bridge v0.9.4b14 (beta)

Fourteenth beta: camera streams no longer repeat pictures.

## Changes

- A camera slower than the panel's rate (for example 20 FPS against the panel's 30) now reaches the panel at its own rate. Until b13 the Bridge filled the gap with copies of the last picture: a 20 FPS stream arrived as 30 FPS with every third picture a copy, so the picture stuttered and the panel decoded a third more than needed.
- Faster cameras are thinned to the panel's rate as before.

## Update Notes

Beta: install through HACS with "Show beta versions". Works with every HomeTiles firmware; nothing changes on the panel.

## Validation

Automated tests passed (809). The new camera command was checked with FFmpeg 8.1 against a 20 FPS RTSP stream: 200 pictures in 10 s, none repeated (the b13 command: 300 pictures, about 115 copies). Not yet checked in a Home Assistant installation.
