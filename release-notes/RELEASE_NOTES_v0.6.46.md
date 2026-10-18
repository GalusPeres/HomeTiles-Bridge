# HomeTiles Bridge v0.6.46

Fixes weather forecasts staying empty after Home Assistant starts when the provider becomes ready later without changing the current weather state. Manual integration reloads are no longer needed to trigger another forecast request.

- Checks configured weather entities every 60 seconds independently of state changes. Successful forecast requests retain their existing ten-minute cache.
- Publishes changed weather payloads only during these checks and respects shared entity ownership across displays.
- Cancels the refresh timer and in-flight request on integration unload or Home Assistant shutdown. The periodic check produces no normal log messages; failures are debug-only.

Update through HACS and restart Home Assistant. No firmware update is required for this startup recovery fix.

Validation: 130 Bridge tests pass, including delayed forecasts with no state event, cache expiry, unchanged payload suppression, ownership, MQTT failure recovery, and timer/task cancellation. Confirmation of automatic recovery after a real Home Assistant restart remains pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.45...v0.6.46
