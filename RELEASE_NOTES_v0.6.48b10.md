# HomeTiles Bridge v0.6.48b10 (beta)

Beta that removes a Home Assistant deprecation warning.

- Home Assistant 2026.8 and newer logged that the Bridge calls `device_registry.async_get_device`, which stops working in Home Assistant 2027.8. The Bridge now looks up its display device within its own config entry. The lookup keeps the link to the display's Web Admin on the Home Assistant device page up to date.
- Home Assistant versions before 2026.8 keep the previous lookup, so the minimum supported version is unchanged.
- Everything from v0.6.48b9 is unchanged.

No firmware update is required.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 257 Bridge tests pass, including the lookup on current and older Home Assistant versions. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b9...v0.6.48b10
