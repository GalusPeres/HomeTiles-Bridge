# HomeTiles Bridge v0.6.48b2 (beta)

Beta for a live view of the display's built-in camera. Only displays whose firmware reports the `local_camera_stream` capability get the live view; all other displays keep the still-image camera from v0.6.48b1 or no camera at all.

- The camera entity shows a live MJPEG stream while someone watches it in Home Assistant (more-info dialog, or a picture card with `camera_view: live`). The display streams only while at least one viewer is open and stops about 3 seconds after the last viewer leaves.
- The display uploads its frames over the existing camera TCP listener (ports 8124-8131) with the same chunk-and-acknowledge transport that the camera popup uses, identified by a new `HTCAMUP/1` handshake. Only the newest frame is kept per display, so slow viewers never slow the display down.
- Frame size and rate are chosen on the display (Web Admin, built-in camera section).
- Still images keep working: a fresh live frame is returned while the stream runs, otherwise a single snapshot is requested as before.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant. Requires HomeTiles firmware with the camera live stream (Guition JC8012P4A1 V2 test builds from v0.6.12b11).

Validation: 192 Bridge tests pass, covering the upload handshake, per-chunk acknowledgements, oversize and corrupt JPEG rejection, token and session checks, connection replacement, the viewer lifecycle with keepalive and grace period, capability gating and snapshot compatibility. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b1...v0.6.48b2
