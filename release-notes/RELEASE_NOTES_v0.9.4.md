# HomeTiles Bridge v0.9.4

The media popup gets its library and speaker grouping, the panel speaker plays every format, and a panel's theme and rotation can be set from Home Assistant.

## Changes

- **Media popup:** the Bridge sends the player's sources, its group and the speakers it can join, and pages through Home Assistant's media library for the panel. The library and grouping work over the direct link and over MQTT; every panel sees the same speakers.
- **Panel speaker (experimental):** the Bridge decodes every clip and stream Home Assistant plays on the panel and sends it plain audio at the pace it plays. Title, position, length and pause show in Home Assistant's player bar; a pause ends the stream and play starts it again from there.
- **Camera sound (experimental, like camera streaming itself):** a camera stream with sound plays its sound on panels with a speaker, in step with the picture. Camera streams never repeat pictures. A panel counts as gone only after 45 s without an acknowledgement instead of 5 s (#116).
- **Device page:** a **Theme** select (Dark, Light, Automatic) and a **Display Rotation** select with the steps the panel offers; the old 180° switch stays for panels with older firmware. A **Listening** switch for the wake word, a wake word list with only the words, and the panel's settings under Configuration. The battery auto-sleep select is gone; no panel has a battery sleep profile.
- **Add device** asks how: **Panel by IP address (direct connection)** for networks where mDNS does not reach Home Assistant (#16), or **Manually over MQTT**.
- The View select changes its option list at most once a minute and keeps its options while a new list arrives (#17).

## Update Notes

Update through HACS and restart Home Assistant, then update the displays to HomeTiles v0.9.4. The media library, speaker streaming, camera sound and the Theme and Display Rotation selects need v0.9.4 on the display; older firmware keeps working as before.

## Validation

830 automated tests passed. The media popup, speaker streaming and camera sound were tested with HomeTiles test builds on the Guition JC8012P4A1 V2, adding a panel by its IP address on the M5Stack Tab5. Speaker streaming, camera sound and camera streaming are experimental; please report what you notice. The Theme and Display Rotation selects are new and not yet checked in Home Assistant by the maintainer.

Thanks to @memooox3 for the media browser (#11), and to everyone reporting.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.3...v0.9.4
