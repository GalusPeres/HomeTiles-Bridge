# HomeTiles Bridge v0.9.8b6 (beta)

Play in the Camera popup sends one picture per step, and a new place shows its time bar sooner. Goes with firmware b667.

## Changes

- **One picture per step of Play.** Every step used to send at least two pictures to the panel, the previous one first (measured on a desktop PC with the Bridge's FFmpeg command). A radar or webcam now sends a picture only when it changed, and an unchanged one every 10 seconds so the stream stays alive.
- **The time bar of a new place shows sooner.** It came only when all 13 pictures were loaded; it now shows after a few and grows.
- **Places load ahead.** When a popup of a radar or webcam opens, the other radar places of that panel load in the background, so switching to one shows its picture at once.

## Update Notes

Beta: install through HACS with "Show beta versions".

## Validation

Automated tests passed. Not yet checked in the maintainer's Home Assistant or on a panel.
