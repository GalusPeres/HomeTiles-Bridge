# HomeTiles Bridge v0.9.4b6 (beta)

Sixth beta: a tidier device page in Home Assistant and a separate Listening switch (HomeTiles firmware beta b500 adds the matching panel side).

## Changes

- Listening is its own switch. The Wake word list shows only the words (Okay Nabu, Hey Jarvis, Alexa) and keeps the chosen word while Listening is off. Firmware b500 starts a new panel with Listening and the Microphone off.
- The Camera switch moved to Configuration, next to Microphone.
- Clearer names, translated in every language: Display Rotation and Display Standby (was the English-only "Display Sleep"), Display Standby After (was Auto-Sleep Power), Camera Image for the picture, Start Assist for the button, Connection (was MQTT Connection, also shown on the direct link). The German screensaver light reads Bildschirmschoner Helligkeit, as on the panel.
- Auto-Sleep Battery is removed: no panel has its own battery sleep time, it always copied the other one. Its old entity is removed from Home Assistant automatically.

Entity IDs stay the same, so automations keep working. Values such as "Never" or "Idle" follow the language of your Home Assistant user profile.

## Update Notes

Beta: install through HACS with "Show beta versions". With firmware older than b500 the Listening switch still works; choosing a word there also turns listening on.

## Validation

Automated tests passed (776), including the device page grouping, the Listening switch with new and older firmware, and the removal of the old battery select. Not yet checked in a running Home Assistant.
