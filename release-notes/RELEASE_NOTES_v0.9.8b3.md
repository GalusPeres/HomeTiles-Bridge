# HomeTiles Bridge v0.9.8b3 (beta)

The rain radar gets its time bar.

## Changes

- **Time bar for the rain radar.** The Bridge now keeps the last two hours of the radar, one picture every 10 minutes, and tells the panel their times. With the new firmware the Camera popup shows a time bar: press Play or drag it by hand. The panel stores no pictures, it asks for the one it shows.
- **The picture is ready at once.** The newest picture loads first, the older ones behind it.
- **Camera kind.** The Bridge now tells the panel whether a camera is live or shows still images, so the popup can show "Live" only where it is true.

## Update Notes

Beta: install through HACS with "Show beta versions". The time bar needs firmware b663 or newer; older firmware keeps showing the newest picture. The history comes from RainViewer: with a fallback source (DWD, NASA) the popup shows the newest picture without a bar.

## Validation

Automated tests passed (958). All three sources were fetched and drawn for real locations, and the time a picked picture takes through FFmpeg was measured (about 4 ms on a desktop PC). Not yet checked in the maintainer's Home Assistant or on a panel.
