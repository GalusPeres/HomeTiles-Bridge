# HomeTiles Bridge v0.6.47

Replaces v0.6.46's minute-based weather refresh with Home Assistant forecast subscriptions, the same mechanism used by Home Assistant's weather UI. New forecasts are forwarded without waiting for a timer or for the current weather state to change.

- Subscribes to supported daily, hourly, and twice-daily forecasts after Home Assistant starts, and when configured weather entities appear or are replaced.
- Reads an initial forecast after subscribing, then receives subsequent forecast notifications directly. No test-entity special cases or periodic weather timer remain.
- Coalesces forecast notifications within the event loop, suppresses unchanged MQTT payloads, and prevents an older in-flight state publication from overwriting newer forecasts.
- Preserves existing service/cache behavior for ordinary state and explicit refresh requests. Removes subscriptions and cancels pending delivery on integration unload or Home Assistant shutdown.

Update through HACS and restart Home Assistant. No firmware update is required for this Bridge change.

Validation: 134 Bridge tests pass, covering late forecasts without state changes, supported forecast combinations, initial snapshots, duplicate notifications, provider replacement, ownership, failure recovery, shutdown cleanup, and concurrent updates. Real Home Assistant restart validation of this version remains pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.46...v0.6.47
