# HomeTiles Bridge v0.6.48b4 (beta)

Beta for a higher frame rate from the display's built-in camera.

- A camera popup that shows another display's built-in camera now runs at up to 24 fps instead of 15 fps.
- The Auto stream mode asks the camera display for 25 fps. The display caps the request to its own mode table and lowers the JPEG quality at high rates. How many frames really arrive also depends on the Wi-Fi upload of the camera display.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, and restart Home Assistant.

Validation: 225 Bridge tests pass. Real Home Assistant and hardware validation is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.48b3...v0.6.48b4
