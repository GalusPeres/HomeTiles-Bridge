# HomeTiles Bridge v0.9.0b9 (beta)

The number card closes when the pairing is cancelled on the panel.

- When the pairing was cancelled on the display (or ran out there) while Home Assistant showed the number, the card stayed, also after closing the dialog, until it was clicked again. It now closes by itself.
- Works together with panel firmware that forgets the Home Assistant address after a cancelled or rejected pairing, so Pair on the panel offers it again right away.
- Everything from v0.9.0b8 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 552 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b8...v0.9.0b9
