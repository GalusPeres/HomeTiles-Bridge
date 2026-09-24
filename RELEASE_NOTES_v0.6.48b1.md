# HomeTiles Bridge v0.6.48b1 (beta)

Beta for the built-in display camera. Only displays whose firmware reports the `local_camera` capability get the new camera entity; all other displays are unchanged.

- Adds a Home Assistant camera entity for the display's built-in camera. Snapshots are requested over MQTT (`cmnd/local_camera`) only when Home Assistant asks for an image; the display captures a single still image and returns it as JPEG.
- The camera stays off on the display until it is enabled in the display's Web Admin Settings. The entity reports the display's retained camera status.
- Removes a stale camera entity when a display stops reporting the capability.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant. Requires a HomeTiles firmware with built-in camera support (Guition JC8012P4A1 V2 first); the firmware side is still in testing.

Validation: 159 Bridge tests pass, covering the snapshot request/response contract, timeouts, error replies, capability gating and stale-entity cleanup. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.47...v0.6.48b1
