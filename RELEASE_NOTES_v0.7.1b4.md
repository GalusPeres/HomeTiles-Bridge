# HomeTiles Bridge v0.7.1b4 (beta)

Clearer encryption setup, after the first test on hardware. Use it with the HomeTiles firmware test build b79 or newer, which uses the same words.

**After updating, reload the Home Assistant page in the browser (or restart the app).** Otherwise the browser can keep the old texts and the dialog shows empty lines.

- The card under Discovered now says what it is for: "Encrypt <display> · 116 721". It encrypts the existing display; no new device is added.
- The dialog shows the number large on its own line. The buttons read "Numbers match: encrypt" and "Reject (stays unencrypted)".
- The texts speak of encryption instead of pairing, like the display does, and are one or two short sentences each (English and German).
- New diagnostic sensor "Encryption" for each display (closed or open shield, with the key id). The options menu shows "Security: encrypted", "not encrypted" or "turning off".
- "Entity configuration" and "Energy Dashboard" in the options menu say "(all displays)": these settings apply to every display.
- Everything else from v0.7.1b3 is unchanged; so is the protocol.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page.

Validation: 376 Bridge tests. They all pass except the timing-sensitive camera tests while the test machine is under heavy load (unchanged code, same as in v0.7.1b3). The encryption setup was tested on hardware with v0.7.1b3 and firmware b78; this version changes texts and adds the sensor.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b3...v0.7.1b4
