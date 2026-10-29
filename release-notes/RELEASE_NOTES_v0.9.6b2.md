# HomeTiles Bridge v0.9.6b2 (beta)

Faster Sensor graph ranges and the new 1H and 30D ranges.

## Changes

- **7D and 30D from the hourly statistics.** Graphs in whole-hour steps read Home Assistant's hourly statistics first and 5-minute statistics only for the running hour. Before, a week read about 2,000 five-minute rows (7D answered after about 690 ms); now 168 hourly rows, like 24H.
- **30D:** the Bridge answers up to 720 hours in steps of up to a day. The panel shows 30 days in three-hour steps (240 points).
- **6H and 1H** (minute steps from the state rows) read the newest hour in one query and then twice as much each time: 6H takes three queries instead of nine, 1H one. Weeks of a busy sensor without statistics still keep their newest hours when the row cap is reached.

## Update Notes

Beta: install through HACS with "Show beta versions". The 1H and 30D ranges come with the next HomeTiles firmware; v0.9.5 asks for 24H, 6H and 7D as before and gets 7D faster.

## Validation

Automated tests passed (848). Not yet checked in the maintainer's Home Assistant.
