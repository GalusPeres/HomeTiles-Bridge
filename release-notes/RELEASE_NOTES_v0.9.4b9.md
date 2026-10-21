# HomeTiles Bridge v0.9.4b9 (beta)

Ninth beta: less panel memory for the speaker (pairs with HomeTiles firmware beta b503).

## Changes

- The Bridge now sends the panel speaker's audio in 4 KB blocks instead of 8 KB. With two blocks on their way at most, a playing panel keeps more of its fast internal memory free. In the first test with 8 KB blocks, a panel had about 30 KB less while a song played.

Firmware b503 fixes two things on the panel side that the b502 test showed: the cause of the slowdown while a song plays, and the player bar in Home Assistant briefly disappearing or jumping back when you seek.

## Update Notes

Beta: install through HACS with "Show beta versions". Works with firmware b502 as well; b503 is recommended.

## Validation

Automated tests passed (798), and two songs played completely over the Bridge locally with the real FFmpeg, also from a seek position. Not yet checked in a running Home Assistant with b503.
