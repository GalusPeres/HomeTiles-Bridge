# HomeTiles Bridge v0.9.7b3 (beta)

Today's energy bars follow the running hour.

## Changes

- **Energy today.** Home Assistant writes an hour's statistics only once the hour is over, so the Energy tile's bars and the "Today" total stood still until the next full hour. The running hour (and an hour not compiled yet) now comes from Home Assistant's 5-minute statistics, so the panel's bars and total follow every five minutes.

## Update Notes

Beta: install through HACS with "Show beta versions". Includes everything from v0.9.7b2. No firmware update needed for this change.

## Validation

Automated tests passed (879). Not yet checked in the maintainer's Home Assistant.
