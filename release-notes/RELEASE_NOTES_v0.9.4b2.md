# HomeTiles Bridge v0.9.4b2 (beta)

Second beta for the reworked Media popup (HomeTiles firmware beta b488).

## Fixes

- The library loads on panels connected over the direct link: its pages now arrive on one fixed topic. The direct link takes no wildcard subscription, so the pages of v0.9.4b1 never reached the panel ("nothing loads" or "the library is not available").

## Update Notes

Beta: install through HACS with "Show beta versions". Use it with firmware b488 or newer; b487 still waits for the old page topics.

## Validation

Automated tests passed. The library over the direct link still needs a test on a real player.
