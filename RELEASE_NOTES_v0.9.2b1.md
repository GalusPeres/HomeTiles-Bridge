# HomeTiles Bridge v0.9.2b1 (beta)

Speaker and Assist for panels with audio hardware.

- A panel that reports a speaker gets a media player entity: Home Assistant can play audio and spoken announcements on the panel.
- A panel that reports a microphone becomes an Assist satellite: the Assist button and Home Assistant's voice pipeline use the panel's microphone, and the answer plays on its speaker. The Wake word setting (Okay Nabu, Hey Jarvis, Alexa or off) lets the panel listen on its own. The Microphone switch turns the microphone off; switching it on from Home Assistant needs the encrypted pairing.
- These entities exist only while the panel announces its speaker or microphone. Other panels are unchanged.
- Works on the direct link and over MQTT. The new names and messages are in all ten languages.
- Needs panel firmware with audio support. No released firmware has it yet.

Contributed by @derhally (#13, #14).

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 731 Bridge tests pass. Validation in a real Home Assistant installation is in progress.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.9.1...v0.9.2b1
