# HomeTiles Bridge v0.9.8

A rain radar and webcams as the Bridge's own cameras, with a day of pictures for the panel's time bar, and Home Assistant's time zone for the panels.

## Changes

- **Rain radar:** a camera for Home Assistant's home, a zone, any town by name or your own coordinates, with nothing to set up. Sources in order: RainViewer worldwide, the DWD inside Germany, NASA's worldwide precipitation layer. Only the location rounded to about 10 km leaves Home Assistant.
- **Webcams:** a panel's Camera tile names a picture or a webcam page; the Bridge reads the page's picture and fetches it every 10 minutes. Foto-Webcam and snow-online bring their last day from their archives at once. A site that refuses the Bridge is named in the panel's Web Admin.
- **One device, one camera per thing:** the Bridge creates its own device "HomeTiles Bridge" without a form, with one camera per radar place and webcam, however many panels use it.
- **Pictures at fixed times:** radar places and webcams keep their pictures on Home Assistant's disk (`hometiles_cameras`), at fixed times on the 10-minute grid, for 24 hours, and still there after a restart. The panel's Camera popup plays them with Play and a time bar; the panel stores none.
- **Time zone:** the panels' **Time zone Automatic** gets Home Assistant's time zone as a rule the display understands.
- **Camera popup:** image entities play like a still camera; a picked frame shows at once, and a frame the panel could not show is sent again after 2 seconds.

## Update Notes

Update through HACS and restart Home Assistant, then update the displays to HomeTiles v0.9.8. Rain radar, webcams and the time bar need v0.9.8 on the display. Check a webcam site's terms; some ask for consent before their pictures are used. A radar place takes about 6 MB a day on the disk, a webcam up to about 14 MB.

## Validation

1012 automated tests passed. The maintainer used the beta of this release for two weeks with HomeTiles test builds: rain radar, webcams and Home Assistant cameras in the Camera tile.

Thanks to everyone reporting.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.7...v0.9.8
