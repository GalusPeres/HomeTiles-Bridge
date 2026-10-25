# HomeTiles Bridge v0.9.6b1 (beta)

First beta after v0.9.4: events for the new Calendar tile.

## Changes

- **Calendars** selection in the entity configuration. For each chosen calendar the Bridge reads the events with Home Assistant's `calendar.get_events` (the current month from a week before the 1st up to the two months after it) and publishes them retained on `ha_prefix/calendar/<object>/events`, in Home Assistant's local time.
- The events are sent again when an event starts or ends, every 15 minutes (for edited future events and the new day) and on a snapshot. The list is bounded (96 events, 16 KB); when events have to be dropped, the latest go and the window the panel shows gets shorter, so a dropped day never looks free.
- The panel's entity picker finds calendars too.
- `docker/test-ha/`: a Docker test Home Assistant for the Calendar tile. `HOMETILES_ADVERTISE_HOST` names the address the Bridge hands to a panel when it runs in a container.

Thanks to @ph15ch for the Calendar tile and this Bridge part (#18).

## Update Notes

Beta: install through HACS with "Show beta versions". The Calendar tile needs HomeTiles firmware test build b543 or newer. Older firmware ignores the new topic.

## Validation

Automated tests passed (845). Contributor-tested with a Docker test Home Assistant and on an ESP32-4848S040C_I panel. Not yet checked in the maintainer's Home Assistant.
