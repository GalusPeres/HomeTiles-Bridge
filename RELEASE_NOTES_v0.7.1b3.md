# HomeTiles Bridge v0.7.1b3 (beta)

Pairing by number, and turning encryption off on both sides. Pairing needs the HomeTiles firmware test build b78 or newer; everything else works with any firmware.

- Pairing compares a six-digit number instead of a typed code. Tap Pair on the display (Settings > System > Security > Pair). Home Assistant shows a card under Discovered with the display's name and the number. Confirm it there and on the display if both show the same number. "Ignore" on the card rejects the pairing.
- A paired display cannot be paired again until the pairing is removed, so a careless click cannot replace the key.
- Configure > Security now only removes the pairing. Removing it in Home Assistant turns encryption off on the display as well, as soon as the display is connected. Turning it off on the display removes the pairing in Home Assistant and shows a notification.
- A pairing code entered with v0.7.1b1 or v0.7.1b2 is removed by this update. The display runs unencrypted until you pair it again.
- After a restart, the Bridge asks a paired display for a new session a few seconds after subscribing. Encrypted commands work right away instead of after about a minute.
- Everything else from v0.7.1b2 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 371 Bridge tests. They all pass except the timing-sensitive camera tests while the test machine was under heavy load; those fail the same way on the v0.7.1b2 code. Pairing, key and envelope vectors match the firmware byte for byte. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b2...v0.7.1b3
