# HomeTiles Bridge v0.9.0b28 (beta)

Media covers with Home Assistant on https, and the Bridge in more languages.

- Media covers reach the panel when Home Assistant runs on https with a certificate from a private CA (issue #10). The Bridge used to download a player's cover from Home Assistant's own address and failed the certificate check; it now reads such a cover from the player itself, as Home Assistant's media proxy does. Covers from other addresses (for example Spotify) load as before.
- Entity names follow the Home Assistant language instead of always German, for example "Display Brightness", "MQTT Connection" and the Auto-Sleep options (#12, thanks @derhally). Entity IDs stay the same.
- The Bridge's dialogs and entity names are now in German, English, French, Polish, Dutch, Spanish, Italian, Russian, Brazilian Portuguese and Swedish. Other languages show English.
- The pairing texts name the panel's Pair button; panels with firmware up to v0.8.0 still call it Encrypt.
- Everything else from v0.9.0b27 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 659 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b27...v0.9.0b28
