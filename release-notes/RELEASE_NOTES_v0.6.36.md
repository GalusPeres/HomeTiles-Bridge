# HomeTiles Bridge v0.6.36

Restart Home Assistant after installing this update.

- Aligns Climate state and command handling with Home Assistant feature flags,
  availability, target humidity steps, and horizontal swing modes.
- Validates Climate modes, ranges, and service data before forwarding commands.
- Rejects Cover commands that the entity does not advertise as supported.
- Moves successful Energy response diagnostics from warning to debug while
  keeping malformed requests and real failures visible.
- Allows Home Assistant to remove stale duplicate HomeTiles devices while
  protecting the currently active panel and its entities.
- Keeps existing configurations compatible.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.35...v0.6.36
