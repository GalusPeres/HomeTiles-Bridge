# HomeTiles Bridge v0.9.2

Speaker and Assist for the panels (early version), and the Button tile finds every scene, script and button.

## Highlights

- Voice assistant, early version: a panel that reports a speaker gets a media player entity, so Home Assistant can play audio and announcements on it. A panel that reports a microphone becomes an Assist satellite: the Assist button and Home Assistant's voice pipeline use its microphone, and the answer plays on its speaker. The Wake word setting (Okay Nabu, Hey Jarvis, Alexa or off) lets the panel listen on its own; Assistant and Finished speaking detection choose the pipeline and how quickly a request ends. The Microphone switch turns the microphone off; switching it on from Home Assistant needs the encrypted pairing.
- These entities exist only while the panel announces its speaker or microphone. Other panels are unchanged. It works on the direct link and over MQTT, with the texts in all ten languages.
- Button tile (formerly Scene): its picker on the panel searches through the Bridge like every other tile. With a Web Admin password on the panel it finds every scene, script and button in Home Assistant, otherwise the ones released under Scenes in the Bridge options; before, it listed only the released ones (HomeTiles#106). Tiles that use a Scene alias keep working.

## Update Notes

Update through HACS, restart Home Assistant and reload the browser page. HomeTiles v0.9.2 uses the speaker, Assist and the Button search; displays with older firmware keep working.

## Validation

732 automated tests passed. Speaker, Assist, wake words and the Button tile search were tested with HomeTiles displays during the v0.9.2 beta series.

Thanks to @derhally for the speaker and Assist (#13, #14).

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.1...v0.9.2
