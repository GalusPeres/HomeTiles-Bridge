# HomeTiles Bridge v0.7.0

Adds built-in display cameras to Home Assistant, improves camera sharing and climate controls, and tightens panel discovery and command validation.

## Highlights

- **Built-in cameras:** compatible displays can provide snapshots and live video to Home Assistant. Multiple viewers and other HomeTiles displays share one live upload. Cameras remain opt-in, with a Camera switch to pause and resume capture.
- **Camera playback:** handles slow live rates, keeps the last picture after a stream ends on the display, turns sideways camera images upright, and adapts still-image camera popups to the source's response rate.
- **Climate modes:** fan, swing and horizontal swing names are matched without regard to case, while Home Assistant receives the exact mode name required by the device.
- **Panel discovery:** an unknown MQTT panel announcement requires confirmation before adding an integration entry. One panel cannot take over another panel's entry through a shared base topic.
- **Configured entities only:** light, climate, media and camera commands, plus history and weather requests, must target entities selected for that panel. Existing switch and cover restrictions remain in place.
- **Home Assistant compatibility:** uses the panel's own config entry for device-registry lookup on newer Home Assistant versions while preserving the older-version path.

## Update Notes

Update through HACS and restart Home Assistant before updating displays to HomeTiles v0.7.0. This is a stable release; enabling beta versions in HACS is no longer required.

Existing panel configurations remain valid. Built-in camera features require compatible firmware that announces the capability, and the camera must be enabled in the display's Web Admin. Older firmware continues to use its existing features. The complete Climate mode list requires the corresponding firmware update.

Camera support remains experimental and hardware validation varies by display. Image rotation uses the established Pillow path; the unvalidated lossless backend remains disabled.

## Validation

267 automated tests passed; one optional TurboJPEG test was skipped because the native library is not installed. That backend remains disabled. Python compilation passed for the integration and tests.

This release promotes the committed v0.6.48b12 functionality without additional runtime changes. Automated tests use Home Assistant stubs and do not establish compatibility with every camera or Home Assistant installation.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.47...v0.7.0
