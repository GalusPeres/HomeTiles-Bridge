# HomeTiles Bridge v0.7.1b5 (beta)

Fixes the card for setting up encryption.

**After updating, reload the Home Assistant page with Ctrl+Shift+R (or restart the app).** Otherwise the browser keeps the old texts and the dialog shows the old wording or empty lines.

- The card under Discovered shows "<display> (116 721)" again, as in v0.7.1b3. The title in v0.7.1b4 was too long, so the card cut the number off.
- Everything else from v0.7.1b4 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page.

Validation: 375 Bridge tests. They all pass except the timing-sensitive camera tests while the test machine is under heavy load (unchanged code).

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b4...v0.7.1b5
