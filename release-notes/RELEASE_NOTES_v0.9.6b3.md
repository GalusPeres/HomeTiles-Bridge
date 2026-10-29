# HomeTiles Bridge v0.9.6b3 (beta)

Fixed periods for the Energy popup: Year, Month, Week and Today.

## Changes

- **This week and this year.** A panel that names its first day of the week gets the week from that day up to now, by the day; the new year period runs from January 1 up to now, by the month. Month (from the 1st, by the day) and Today (from midnight, by the hour) stay as they were. Firmware that sends no first weekday gets the last seven days as before.
- **Each bar in its place.** Every statistics row lands in the bar of its own hour, day or month. Before, the rows were counted from the first one, so a sensor added in March showed its March value under January and a gap moved all later bars forward. The hour repeated when the clocks go back adds to its bar.

## Update Notes

Beta: install through HACS with "Show beta versions". The Year, Month, Week and Today buttons come with the next HomeTiles firmware; v0.9.5 keeps its 7D and Today.

## Validation

Automated tests passed (856). Not yet checked in the maintainer's Home Assistant.
