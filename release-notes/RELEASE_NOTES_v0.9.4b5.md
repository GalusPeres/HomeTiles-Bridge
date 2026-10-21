# HomeTiles Bridge v0.9.4b5 (beta)

Fifth beta, with a camera fix on top of the Media popup betas (HomeTiles firmware beta b496 adds the matching panel logs).

## Fixes

- The camera popup no longer ends after a short Wi-Fi stall (#116). The Bridge waited 5 s for the panel's acknowledgement of each picture and then closed the stream without a word; the panel showed "Stream connection ended" until the popup was opened again. A panel now counts as gone only after 45 s without an acknowledgement, the same limit as the direct link, so the picture pauses and then continues. When the Bridge does end a stream for this reason, the Home Assistant log shows one warning.

## Update Notes

Beta: install through HACS with "Show beta versions".

## Validation

Automated tests passed (764), including a 6 s pause of the panel over real sockets. Checked with the real stream code and FFmpeg: stalls of 6 s and 20 s continued, a panel silent for 45 s ended with one warning. A Wi-Fi stall on a real panel still needs a test.
