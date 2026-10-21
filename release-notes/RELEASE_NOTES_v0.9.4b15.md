# HomeTiles Bridge v0.9.4b15 (beta)

Fifteenth beta: a paused song on the panel's speaker holds nothing.

## Changes

- Pausing a song on a panel's speaker now ends its stream: the panel frees the memory it used for it and the Bridge stops its FFmpeg. Play starts the song again from the paused point, the same way a jump in the song does, so a pause can last as long as you like. Until b14 the panel kept the stream open while paused, and the Bridge gave the song up after 10 minutes.
- A camera's live sound is not affected.

## Update Notes

Beta: install through HACS with "Show beta versions". Needs HomeTiles firmware test build b512 or newer for the new pause; older firmware keeps the stream while paused, as before.

## Validation

Automated tests passed (809). Not yet checked in a Home Assistant installation or on a panel.
