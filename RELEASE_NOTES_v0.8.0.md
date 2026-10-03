# HomeTiles Bridge v0.8.0

Adds locks, alarm panels and fans, encrypted commands with pairing by number, complete sensor graphs, and shared, faster camera streams for HomeTiles v0.8.0.

## Highlights

- **Locks, alarm panels and fans:** new **Fans**, **Locks** and **Alarm panels** fields in the entity configuration feed the new display tiles. Fans support on/off, speed, preset, oscillation and direction, each only when the fan supports it, and stay selectable as switchable entities.
- **Secure locks and alarm panels:** they can only be operated from an encrypted (paired) display with a Web Admin password, and only through encrypted commands. Home Assistant checks every code as in its own dashboard. **Allow opening without a code** is only for devices without their own code. Per display and entity, one command with a code runs at a time and at most ten per minute; after five wrong codes, code entry is blocked for 30 seconds, doubling up to one hour, with a Home Assistant notification. Codes are never logged or stored.
- **Wrong codes that devices ignore:** under **Codes for locks and alarm panels** the Bridge checks codes itself for devices that report success on a wrong code. Alarmo, Total Connect and Elmax wrong codes are recognised without it.
- **Encrypted commands:** pair a display by comparing a six-digit number on the display and in a card under Discovered. The Bridge then runs only encrypted, signed commands from that display, and camera stream tokens travel encrypted. Turning it off on either side turns it off on both. A diagnostic **Encryption** sensor shows the state.
- **Web Admin password:** the pairing form has an optional field for the display's Web Admin password; it is used only to sign in and never stored.
- **Faster, complete sensor graphs:** graphs use Home Assistant's statistics (mean values) where a sensor has them, so a 7-day graph loads much faster, and otherwise the sensor's full history for up to 7 days, so busy sensors are no longer cut off. Graph requests beyond two at a time wait instead of being dropped.
- **Camera streams:** displays showing the same camera share one stream. Large streams keep up on slower hosts: frames the display does not show are dropped before scaling, decoding uses four threads, and a stream that falls behind restarts at the live position. Displays may ask for up to 30 pictures per second.
- **Weather at night:** the Bridge sends sunrise and sunset for each forecast day, so displays show the night icons.
- **Media covers:** a player without artwork (for example a Sonos speaker on its TV input) no longer keeps the last song's cover; short gaps between songs keep it.
- **Bounded discovery:** display announcements and discovery cards are limited, and linking a display to an existing entry without a display needs confirmation.

## Update Notes

Update through HACS and restart Home Assistant, then reload the Home Assistant page in the browser, before updating displays to HomeTiles v0.8.0. This is a stable release; beta versions are no longer needed.

Existing configurations, topics and tiles remain valid. Older firmware keeps working unencrypted and ignores the new fields. Locks, alarm panels and fans, encryption and the Web Admin password need HomeTiles firmware v0.8.0.

## Validation

495 automated tests passed; one optional TurboJPEG test was skipped. Pairing, keys and encrypted envelopes match the firmware byte for byte. Locks, alarm panels, fans, encryption and the camera streams were tested with HomeTiles displays during the v0.7.1 beta series.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.0...v0.8.0
