# HomeTiles Bridge v0.9.0b14 (beta)

Each panel gets only the entities it uses.

- Until now every panel received the entities you released for any panel. With firmware b238 or later the panel tells the Bridge everything it uses, and the Bridge sends it only the entities released for this panel plus the entities on its tiles. The panel gets a smaller configuration and follows fewer values.
- Entities released for another panel still work on a tile of this panel, also without a Web Admin password. The search in the panel's Web Admin still offers every released entity.
- Lock rules and scenes are unchanged.
- Panels with older firmware or without pairing keep receiving every released entity, as before. Everything else from v0.9.0b13 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 575 Bridge tests pass, including runs of panel and Bridge together with panels on older and newer firmware. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b13...v0.9.0b14
