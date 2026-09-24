# HomeTiles Bridge v0.6.48b6 (beta)

Beta for ending the display camera's live view from the display itself.

- A tap on the camera pill on the display ends the running live view. The Bridge now ends it everywhere: the Home Assistant camera view closes instead of freezing on the last frame, and the camera popup on other HomeTiles displays shows "Stream stopped" instead of a frozen image.
- After such an end the Bridge takes no still images in the background for the ended view, so the camera stays off.
- Opening the camera again, in Home Assistant or on another display, starts a new live session at once. Previously it could stay unavailable.
- The pause switch from v0.6.48b5 is unchanged; the camera pill on the display only ends the current view and no longer pauses the camera.

Requires HomeTiles firmware that reports the ended session (Guition JC8012P4A1 V2 test build v0.6.12b21). Older firmware keeps the previous behaviour.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 241 Bridge tests pass, including the ended-session status field, ending waiting viewers, a new session for the next viewer, the Home Assistant view ending on the panel's end, and a popup that stops without falling back to snapshots. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b5...v0.6.48b6
