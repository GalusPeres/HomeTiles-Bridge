# HomeTiles Bridge v0.9.4b8 (beta)

Eighth beta: the Bridge decodes the panel speaker's clips (HomeTiles firmware beta b502 adds the matching panel side).

## Changes

- The Bridge now reads every clip for the panel speaker itself with FFmpeg and sends the panel plain audio, at the pace the panel plays it. The panel no longer downloads or decodes anything, so music, announcements and spoken answers in any format Home Assistant can serve play without slowing the panel down. Songs whose MP3 the panel could not read (two tags in a row with the cover in the second) play as well.
- Music plays at 44.1 kHz; spoken answers, text to speech and announcements at 22.05 kHz.
- Seeking in Home Assistant's player bar starts the clip at the chosen position, once its length is known.
- The audio travels over the same local connection as the camera picture, with at most two 8 KB blocks on their way to the panel at any moment.

The speaker stays a small player for a microcontroller: no queue, no next or previous track. While it plays, the panel does not listen for its wake word.

## Update Notes

Beta: install through HACS with "Show beta versions". Panels with firmware before b502 keep downloading clips themselves as before.

## Validation

Automated tests passed (798), including a complete clip against a simulated panel, pause, a panel that stops responding, a clip FFmpeg cannot read and the window of two blocks. Not yet checked in a running Home Assistant.
