# HomeTiles Bridge v0.7.1b1 (beta)

Beta for complete sensor graphs and for the optional security features of the next HomeTiles firmware.

- Sensor graphs use Home Assistant's statistics when the sensor has them: 5-minute statistics while the recorder keeps them, hourly statistics for older days. Other sensors read their full history for up to 7 days (up to 60,480 changes), so busy sensors such as PV power are no longer cut off. Energy meters (total, total_increasing) show their meter reading.
- In views with several graph tiles, history requests beyond two at a time now wait instead of being dropped, so every graph fills.
- The pairing form has an optional field for the display's Web Admin password. It is only used to log in to the display and is never stored.
- Under Configure > Security you can enter the pairing code shown on the display. Commands from that display are then accepted only encrypted.
- Display announcements and discovery cards are bounded, and linking a display to an existing entry without a display needs your confirmation.
- The Web Admin password, encrypted commands and signed announcements need the next HomeTiles firmware. With firmware v0.7.0, and without a password or pairing code, everything works as before.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 337 Bridge tests pass. Validation in a real Home Assistant installation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.0...v0.7.1b1
