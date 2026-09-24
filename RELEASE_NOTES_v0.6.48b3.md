# HomeTiles Bridge v0.6.48b3 (beta)

Beta for faster camera popups on the displays.

- A camera popup that shows another display's built-in camera now joins that camera's live stream instead of requesting a snapshot twice per second. Several popups and Home Assistant viewers share one upload from the camera display.
- Camera popups for still-image cameras (cameras without a video stream source) are no longer limited to 2 fps. The Bridge requests the next image as soon as the previous one has arrived, up to 10 fps. A slow camera runs at its own pace, and only one request per popup is ever open. An unchanged image is not sent again.
- Still images are fetched at full size and scaled by FFmpeg, so Home Assistant does not rescale every image on its event loop.
- Once per popup, the log shows the still-image rate that was reached (`[CameraDiag] ... still=X fps`).

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 225 Bridge tests pass, covering live-stream sharing between popups, the adaptive still-image rate with fast, slow and failing cameras, frame repetition while a request is open, and the failure limit. The tests use fake cameras and a fake FFmpeg. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b2...v0.6.48b3
