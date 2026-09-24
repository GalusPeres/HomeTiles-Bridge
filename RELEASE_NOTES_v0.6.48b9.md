# HomeTiles Bridge v0.6.48b9 (beta)

Beta for display cameras that are mounted sideways.

- The front camera of the Waveshare ESP32-P4-WIFI6-Touch-LCD-8 sits a quarter turn from the landscape screen. The display now sends its picture as it comes from the sensor, and the Bridge turns it upright before Home Assistant shows it: live view, still images and cameras on other HomeTiles displays. The display no longer spends time turning every frame itself, which made its screen sluggish.
- The Bridge turns the picture with Pillow and encodes it again at high quality. A lossless turn that needs no new encoding follows in a later beta after it has been validated in the Home Assistant container.
- Displays whose camera is mounted upright (Guition JC8012P4A1 V2) are unchanged.
- Everything from v0.6.48b8 is unchanged.

Requires HomeTiles firmware v0.6.12b29 on the Waveshare 8-inch camera test build. With older firmware the picture stays as the display sends it.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 253 Bridge tests pass, including the turn direction, the frame order in the live stream and the still-image cache. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b8...v0.6.48b9
