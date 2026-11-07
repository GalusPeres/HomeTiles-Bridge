# HomeTiles Bridge v0.9.8b7 (beta)

A webcam site that refuses the Bridge is now told in the Web Admin. Goes with firmware b668.

## Changes

- **A site that refuses is named.** Some sites do not let the Bridge read their pages (for example Bergfex on its .de, .at and .ch addresses, HTTP 403). The panel's Web Admin now says so and asks for the picture's own address, instead of saying nothing.
- **A second chance for a picture.** A radar or webcam repeats an unchanged picture every 2 seconds instead of every 10, because a panel may skip a frame it cannot show at that moment. Between changes it stays quiet as before.

## Update Notes

Beta: install through HACS with "Show beta versions".

## Validation

Automated tests passed. Not yet checked in the maintainer's Home Assistant or on a panel.
