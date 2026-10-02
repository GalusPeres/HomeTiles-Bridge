# HomeTiles Bridge v0.7.1b11 (beta)

Wrong codes for locks and alarm panels that ignore them.

- New options step "Codes for locks and alarm panels (all displays)": one masked field per offered lock and alarm panel, up to ten codes per device separated by commas.
- Many devices ignore a wrong code without an error, for example MQTT alarm panels, Risco, Concord232, IFTTT and Verisure locks. Home Assistant then reports success, and the panel could show neither "Wrong code" nor the lockout. With codes entered, the Bridge checks every code from a panel itself: a wrong one is answered with "Wrong code" and counts towards the lockout without reaching Home Assistant; a correct one is passed on to Home Assistant as before.
- Leave a field empty when Home Assistant already reports wrong codes for the device; nothing changes then. When the code on the device changes, change it in the Bridge too.
- The entered codes are stored in the Bridge's Home Assistant configuration. Codes typed on a panel are still never logged or stored.
- Existing configurations, topics and tiles are unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 483 Bridge tests.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b10...v0.7.1b11
