# HomeTiles Bridge v0.7.1b2 (beta)

Fixes pairing for encrypted commands.

- A pairing code entered under Configure > Security was saved but not used, so the display kept waiting for the Bridge. The Bridge now reads the saved code. A code entered with v0.7.1b1 is picked up after the update without entering it again.
- The Security dialog says what it did: "Pairing saved" with the key id, "Pairing removed", or a hint when the code field was left empty (nothing is changed then).
- With debug logging enabled, the log shows when the Bridge answers a paired display.
- Everything from v0.7.1b1 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 337 Bridge tests pass. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b1...v0.7.1b2
