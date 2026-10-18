# HomeTiles Bridge v0.6.45

Fixes missing forecast days and incorrect daily high/low temperatures for weather providers that offer twice-daily forecasts, including Yandex Pogoda.

- Uses supported day/night forecasts when a daily forecast is unavailable. A short hourly forecast no longer limits a full week to only one or two days.
- Groups day/night periods by the Home Assistant local date, preserves daily highs and lows, and prefers the daytime weather condition.
- Adds day/night rainfall totals and keeps the highest precipitation probability per day. Short hourly forecasts do not overwrite these totals; hourly temperature and rain data remain available for charts.
- Preserves existing daily-provider behavior, current weather values, units, and the MQTT payload format. Also fixes an undefined limit in the legacy forecast-attribute path.

Update through HACS and restart Home Assistant. No firmware update or weather entity reconfiguration is required.

Validation: 126 Bridge regression tests pass, covering forecast feature combinations, caching, local dates, missing values, dry/rainy periods, current weather fields, provider failures, and legacy forecasts. The original two-day failure was reproduced in Home Assistant and on a Waveshare 8-inch display; confirmation of the updated Bridge on that display is pending.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.6.44...v0.6.45
