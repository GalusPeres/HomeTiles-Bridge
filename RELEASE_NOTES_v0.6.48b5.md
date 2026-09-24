# HomeTiles Bridge v0.6.48b5 (beta)

Beta for pausing the display's built-in camera.

- New switch "Camera" on each display with a built-in camera. Off pauses the camera, on resumes it. The same pause can be started on the display itself (the camera pill at the top).
- While the camera is paused, the camera entity stays in Home Assistant. It is shown as off, delivers no images and never asks the display for a snapshot or a live stream. A running live view ends instead of freezing on the last frame. Resuming starts the stream again if someone is still watching.
- The switch appears together with the camera entity and is removed together with it when the camera is disabled completely in the display's Web Admin.

Requires HomeTiles firmware with the camera pause (Guition JC8012P4A1 V2 test builds from v0.6.12b17). Older test builds ignore the pause command.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 237 Bridge tests pass, covering the pause/resume commands, the status with and without the pause flag, the switch state, capability gating, and a paused camera that requests neither snapshots nor a stream. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b4...v0.6.48b5
