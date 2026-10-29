# HomeTiles Bridge v0.9.6

Events for the new Calendar tile, faster Sensor graphs with the 30D and 1H ranges, and fixed Year, Month, Week and Today periods for the Energy popup.

## Changes

- **Calendars:** a new **Calendars** selection in the entity configuration. For each chosen calendar the Bridge reads the events with Home Assistant's `calendar.get_events` (the current month from a week before the 1st up to the two months after it) and publishes them retained on `ha_prefix/calendar/<object>/events`, in Home Assistant's local time. The events are sent again when one starts or ends, every 15 minutes and on a snapshot. The list is bounded (96 events, 16 KB); when events have to be dropped, the latest go and the window the panel shows gets shorter, so a dropped day never looks free. The panel's entity picker finds calendars too.
- **Sensor graphs:** ranges in whole-hour steps read Home Assistant's hourly statistics first and 5-minute statistics only for the running hour; a week now reads 168 rows instead of about 2,000. The Bridge answers up to 30 days (the panel's 30D in three-hour steps). 6H and 1H read the newest hour first and then twice as much each time.
- **Energy periods:** this week from the panel's first day of the week, by the day, and the new year period from January 1, by the month; month and today stay as they were. Every statistics row lands in the bar of its own hour, day or month, so a sensor added in March no longer shows its March value under January, and a gap no longer moves the later bars forward.
- `docker/test-ha/`: a Docker test Home Assistant for the Calendar tile. `HOMETILES_ADVERTISE_HOST` names the address the Bridge hands to a panel when it runs in a container.

## Update Notes

Update through HACS and restart Home Assistant, then update the displays to HomeTiles v0.9.6. The Calendar tile, the Energy popup's Year and Week and the Sensor graph's 30D and 1H need v0.9.6 on the display. Displays with v0.9.5 keep their 7D and Today and get 7D faster.

## Validation

856 automated tests passed. The Sensor graph ranges and the Energy periods were tested with HomeTiles test builds on the Guition JC8012P4A1 V2 and the Guition ESP32-4848S040. The Calendar part was contributor-tested with a Docker test Home Assistant and on an ESP32-4848S040C_I panel.

Thanks to @ph15ch for the Calendar tile and its Bridge part (#18), and to everyone reporting.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.4...v0.9.6
