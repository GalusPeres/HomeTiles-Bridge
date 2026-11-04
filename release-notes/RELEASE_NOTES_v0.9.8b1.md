# HomeTiles Bridge v0.9.8b1 (beta)

Panels can take Home Assistant's time zone.

## Changes

- **Time zone from Home Assistant.** The Bridge now sends Home Assistant's time zone with the panel's configuration. A panel set to the time zone **Auto (Home Assistant)** follows it, including summer time. The time itself still comes from NTP.

## Update Notes

Beta: install through HACS with "Show beta versions". The option needs a firmware that has it; older firmware ignores the new value.

## Validation

Automated tests passed (889). Every zone of the time zone database was checked hour by hour over a year. Not yet checked in the maintainer's Home Assistant.
