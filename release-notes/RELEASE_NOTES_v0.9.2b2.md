# HomeTiles Bridge v0.9.2b2 (beta)

The Button tile (formerly Scene) finds every scene, script and button.

- The entity picker of the panel's Button tile now searches through the Bridge like every other tile: with a Web Admin password on the panel every scene, script and button of Home Assistant, otherwise the ones released under Scenes in the Bridge options. Before, it listed only the released ones.
- A Button tile can store the entity id it runs; the Bridge runs such an entity only when the panel's declaration allows it (released, or declared with a Web Admin password). Tiles that use a Scene alias keep working.
- Needs panel firmware that knows the search (entity search level 4); older firmware is unchanged.
- Everything from v0.9.2b1 (speaker, Assist, wake words) is included.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 732 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.2b1...v0.9.2b2
