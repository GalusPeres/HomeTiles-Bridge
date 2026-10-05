# HomeTiles Bridge v0.9.0b10 (beta)

Entity icons like in Home Assistant.

- Many entities get their icon from their integration (for example the GitHub stars, Kostal or YouTube sensors, door and window sensors). The Bridge now reads these icons the way Home Assistant does, so the panel shows the same icon instead of a placeholder. State icons (door open or closed) and level icons follow the state.
- An icon you chose for an entity in Home Assistant still wins, then the entity's own icon.
- Media players keep their device icon while playing.
- Everything from v0.9.0b9 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 558 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b9...v0.9.0b10
