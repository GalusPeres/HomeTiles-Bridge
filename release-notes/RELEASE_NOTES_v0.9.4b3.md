# HomeTiles Bridge v0.9.4b3 (beta)

Third beta for the reworked Media popup (HomeTiles firmware beta b489).

## Fixes

- Folders of the library open: a folder's id goes to the player exactly as it was listed, an empty one too. Sonos lists "Favorites" and "Music Library" with an empty id; the Bridge asked for "no id", which Sonos answers with its first level again, so a tap seemed to do nothing.
- Ids and types are never cut or trimmed any more (a changed id does not open); an entry whose id is too long for the panel is left out.

## Update Notes

Beta: install through HACS with "Show beta versions".

## Validation

Automated tests passed. The library on real players still needs a test.
