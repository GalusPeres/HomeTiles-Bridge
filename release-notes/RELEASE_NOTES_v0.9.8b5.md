# HomeTiles Bridge v0.9.8b5 (beta)

Rain radar places, webcams and several entries per Camera tile. Goes with firmware b665, whose Web Admin sets it all up.

## Changes

- **The Bridge has its own device.** "HomeTiles Bridge" appears by itself, without a form. Its cameras belong to it, never to a panel, so removing a panel moves nothing.
- **One entity per thing.** The radar of Home Assistant's home always exists, as `camera.ht_radar_home`. Every other place and every webcam exists while a panel uses it and goes with the last one. Several panels and tiles share one entity and one download.
- **Rain radar places.** Home Assistant's home and zones, any town by name (through Open-Meteo's geocoding, no key) or your own coordinates.
- **Webcams.** An address is a picture or a page; on a page the Bridge reads its `og:image`, with no code per site. It fetches every 10 minutes and keeps changed pictures for 24 hours, so the time bar fills up over a day. Only https addresses of public names. If a page has no picture the Web Admin says so. Check the site's terms: some ask for consent before their pictures are used.
- **The picture takes its own shape.** A still camera's frames fit the panel's popup without bars or cropping, panoramas too. Direct streams stay as they were.
- **Image entities have a popup.** They play like a still camera; a PNG is converted to JPEG once per picture.
- **New pictures come by themselves.** The radar asks when a frame is due, then every minute, also while a panel is parked on an older frame.
- **Home Assistant's camera window plays the radar as a loop**, with the time drawn in. The panel's pictures name only the source.
- **Emergency radar address.** A panel can name another list in RainViewer's format on its System page.

## Update Notes

Beta: install through HACS with "Show beta versions". The old per-panel radar entities are removed; a tile that used one must pick the radar again. Panels before firmware b665 keep working with everything else, only without the new choices.

## Validation

Automated tests passed. Not yet checked in the maintainer's Home Assistant or on a panel.
