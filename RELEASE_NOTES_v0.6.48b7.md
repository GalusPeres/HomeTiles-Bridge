# HomeTiles Bridge v0.6.48b7 (beta)

Beta that keeps the display camera off after it was ended on the display.

- After a tap on the camera pill on the display ends the live view, the Home Assistant camera picture stays on the last live frame. The Bridge no longer asks the display for a new still image every few seconds, so the camera really stays off.
- Opening the camera again, in Home Assistant or on another display, starts a new live session and the picture updates normally again.
- Everything from v0.6.48b6 (ending viewers everywhere, the pause switch) is unchanged.

Requires HomeTiles firmware that reports the ended session (Guition JC8012P4A1 V2 test build v0.6.12b21 or newer). Older firmware keeps the previous behaviour.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 242 Bridge tests pass, including the frozen picture after the panel's end without snapshot requests (also after the live frame went stale and when the retained end repeats) and a new session that unfreezes it. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b6...v0.6.48b7
