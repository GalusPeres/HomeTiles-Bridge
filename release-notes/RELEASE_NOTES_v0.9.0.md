# HomeTiles Bridge v0.9.0

Connects HomeTiles v0.9.0 displays directly, without an MQTT broker, and adds the entity search, pictures from Home Assistant and the full-screen camera.

## Highlights

- Direct link: press **Pair** on the display and add it in Home Assistant. No MQTT broker is needed. Displays on MQTT can switch to the direct link; existing MQTT setups keep working.
- Entity search for the display's new entity picker, with Home Assistant's icons. Each display gets only the entities it uses.
- Fewer requests from the displays, and changes reach them by themselves.
- Pictures from Home Assistant: sharp media covers, and an image or camera entity as the screensaver picture (#69).
- Full-screen camera with a faster stream for every display, also on the ESP32-S3 (#65). An automation can open a camera straight in full screen, for example for a doorbell.
- Media covers also when Home Assistant runs on https with a certificate from a private CA (#10).
- Entity names and dialogs follow the Home Assistant language: German, English, French, Polish, Dutch, Spanish, Italian, Russian, Brazilian Portuguese and Swedish (#12, thanks @derhally).

## Update Notes

Update through HACS, restart Home Assistant and reload the browser page before updating displays to HomeTiles v0.9.0. Existing configurations stay valid; displays with older firmware keep working over MQTT.

## Validation

659 automated tests passed. The direct link, pairing, entity search, pictures and camera streams were tested with HomeTiles displays during the beta series.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.8.0...v0.9.0
