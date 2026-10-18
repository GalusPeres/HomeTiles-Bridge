# HomeTiles Bridge v0.9.0b16 (beta)

Fewer requests from the panels, and changes reach them by themselves.

- The Bridge now tells the panel that it sends changes by itself. Panels with the performance test firmware (b259-perf builds) then refresh everything only every 15 minutes instead of every minute. Older panels ignore this and keep refreshing every minute.
- When a used entity gets a new name, unit, device class or state class in Home Assistant, the Bridge sends the panels a new configuration after 5 seconds.
- Refresh, energy and weather requests of a panel are limited like history requests: one at a time, a short waiting line and a budget per minute.
- Without an Energy dashboard, energy requests get an empty answer, so the panel stops asking again. The warning about it appears at most once an hour.
- Everything else from v0.9.0b15 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 586 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.0b15...v0.9.0b16
