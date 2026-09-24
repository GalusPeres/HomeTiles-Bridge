# HomeTiles Bridge v0.6.48b8 (beta)

Beta for slow display camera rates.

- The display camera can now stream at very low rates (down to 1 frame per second, firmware setting "Custom"). The Home Assistant camera picture keeps using the live frame at these rates instead of asking the display for a separate still image, which the display would answer as busy.
- Everything from v0.6.48b7 (frozen picture after the stream was ended on the display) is unchanged.

Requires HomeTiles firmware with the Custom stream mode (Guition JC8012P4A1 V2 test build v0.6.12b24). Other firmware keeps the previous behaviour.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 242 Bridge tests pass, including a live frame that stays fresh at one frame per second. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b7...v0.6.48b8
