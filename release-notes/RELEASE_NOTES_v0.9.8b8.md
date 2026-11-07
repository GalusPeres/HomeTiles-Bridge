# HomeTiles Bridge v0.9.8b8 (beta)

Every Bridge camera keeps its pictures alike, at fixed times. Goes with firmware b668.

## Changes

- **Fixed times.** A webcam picture carries the time its site gives it (Last-Modified), otherwise the time it was fetched, on the 10-minute grid: 10:00, 10:10, and so on. One picture never shows under two times, also not after a restart. Two times with the same picture after updating the Bridge are merged into one.
- **Archives.** A webcam from Foto-Webcam or snow-online gets the last 24 hours from the portal's archive at once, so the time bar and Play work from the start. Other webcams collect from when they are added.
- **The rain radar works like the webcams.** Its frames stay on Home Assistant's disk for 24 hours, also after RainViewer no longer lists them, and survive a restart. A place a panel uses collects every 10 minutes, also while nobody looks. The home radar without a panel still loads only when it is shown.
- **One folder.** All pictures are kept in `hometiles_cameras` in Home Assistant's configuration folder. The webcam pictures of earlier betas move there.

## Update Notes

Beta: install through HACS with "Show beta versions".

Measured on a desktop PC: a radar place takes about 40 KB per picture (about 6 MB a day), a Foto-Webcam about 14 MB a day. snow-online's archive holds only a few pictures a day.

## Validation

Automated tests passed (1012). Checked against the real Foto-Webcam and snow-online archives and RainViewer from a desktop PC. Not yet checked in the maintainer's Home Assistant or on a panel.
