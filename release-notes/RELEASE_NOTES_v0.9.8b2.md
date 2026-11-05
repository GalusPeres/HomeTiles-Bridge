# HomeTiles Bridge v0.9.8b2 (beta)

A rain radar every panel can simply pick.

## Changes

- **Rain radar.** The Bridge now brings a **Rain radar** camera along. Choose it in a Camera tile; there is nothing to set up, no option, no key and no extra integration. It shows the rain around Home Assistant's home location on a map, with the picture's time and source in it, and loads pictures only while somebody shows it.
- **It keeps working when a source fails.** RainViewer comes first (worldwide), then the DWD inside Germany, then NASA's worldwide precipitation layer, which runs several hours behind. If all of them fail, the last picture stays.
- **Your location stays yours.** Only the home location rounded to 0.1 degrees (about 10 km) leaves Home Assistant.

## Update Notes

Beta: install through HACS with "Show beta versions". No new firmware is needed: the panel treats the radar as a camera. Switch the tile to show its picture. Panels on MQTT show the picture in the popup only; the tile's picture needs the direct link. The time bar comes with a later firmware.

## Validation

Automated tests passed (939). All three sources were fetched and drawn for real locations. Not yet checked in the maintainer's Home Assistant or on a panel.
