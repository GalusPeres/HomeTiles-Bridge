# HomeTiles Bridge v0.9.4b4 (beta)

Fourth beta for the reworked Media popup (HomeTiles firmware beta b491).

## Fixes

- Grouping speakers from a panel works with several panels: the speakers a panel shows under "Speakers" travel in the player's shared state, which every panel's entry publishes. Each entry built that list from its own players only, so a panel could show a speaker another panel uses, and its join was then refused ("not_a_candidate" in the log). Every entry now offers and accepts the same speakers: the media players served to any panel on the same state prefix.

## Update Notes

Beta: install through HACS with "Show beta versions".

## Validation

Automated tests passed. Grouping from two panels on real players still needs a test.
