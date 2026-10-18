# HomeTiles Bridge v0.9.0b11 (beta)

Entity search for the new entity picker in the panel's Web Admin.

- With a paired panel (direct link or encrypted commands), the entity picker asks the Bridge and shows the area and device of each entity, like Home Assistant.
- With a Web Admin password set on the panel, the picker finds every Home Assistant entity of the tile type, not only the released ones. Without a password it shows the released entities and a hint to set one. The Bridge decides this the same way as for Lock and Alarm.
- Tiles that use an entity beyond the released lists now get its state: the panel reports these entities, the Bridge checks them and publishes their states.
- Search and report travel only over the encrypted channel. Unpaired panels and older firmware work as before.
- Everything from v0.9.0b10 is unchanged.

Requires panel firmware with the new entity picker for the search; older firmware is unaffected.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 567 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b10...v0.9.0b11
