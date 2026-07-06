# ESP32-P4 HomeAssistant Display Bridge

Home Assistant custom integration for the [ESP32-P4 HomeAssistant Display](https://github.com/GalusPeres/ESP32-P4-HomeAssistant-Display) project. Bridges Home Assistant entities, sensors, weather, energy data and more to ESP32-based LVGL displays via MQTT.

## About

This integration is the Home Assistant companion for the **ESP32-P4 HomeAssistant Display** firmware. It handles:

- Pushing entity states, metadata and icons to the display in real time
- Sensor history for popup charts (24h / 5min buckets)
- Weather forecasts (daily + hourly)
- Energy dashboard data (consumption, solar, grid, battery, gas, water)
- Light, switch, media player and scene control from the display
- Auto-discovery of integration-owned sensors (battery, temperature)

**Firmware repository:** [ESP32-P4-HomeAssistant-Display](https://github.com/GalusPeres/ESP32-P4-HomeAssistant-Display)

**Documentation:** [galusperes.github.io/ESP32-P4-HomeAssistant-Display](https://galusperes.github.io/ESP32-P4-HomeAssistant-Display/) — full setup guide, [bridge configuration](https://galusperes.github.io/ESP32-P4-HomeAssistant-Display/bridge/), tile reference, and FAQ

## Installation

### Via HACS (Recommended)

1. Add this repository as a custom repository in HACS:
   - HACS > Integrations > three-dot menu (top right) > Custom repositories
   - Repository: `https://github.com/GalusPeres/ESP32-P4-HomeAssistant-Display-Bridge`
   - Category: Integration
   - Click "Add"

2. Install the integration:
   - HACS > Integrations > Search for "ESP32-P4 HomeAssistant Display Bridge"
   - Click "Download"

3. Restart Home Assistant

4. Add the integration:
   - Settings > Devices & Services > Add Integration
   - Search for "ESP32-P4 HomeAssistant Display Bridge"

### Manual Installation

1. Copy the `custom_components/tab5_lvgl` directory to your Home Assistant `custom_components` folder
2. Restart Home Assistant
3. Add the integration via Settings > Devices & Services

## Configuration

Detailed instructions: [bridge documentation](https://galusperes.github.io/ESP32-P4-HomeAssistant-Display/bridge/)

Configure via the Home Assistant UI:

- **Panel Settings** - MQTT base topic, HA prefix, device metadata
- **Entity Configuration** - Sensors, weather, lights, switches, media players, scenes
- **Energy Dashboard** - Electricity, gas and water from the HA Energy Dashboard

## MQTT Topics

The integration communicates with the display firmware via MQTT:

| Topic | Direction | Description |
|---|---|---|
| `base_topic/stat/connected` | Display > HA | Connection status |
| `tab5_lvgl/config/{id}/bridge/apply` | HA > Display | Full configuration push |
| `tab5_lvgl/config/{id}/bridge/icons` | HA > Display | Lightweight icon updates |
| `tab5_lvgl/config/{id}/history/*` | Bidirectional | Sensor history request/response |
| `tab5_lvgl/config/{id}/weather/*` | Bidirectional | Weather forecast request/response |
| `tab5_lvgl/config/{id}/energy/*` | Bidirectional | Energy data request/response |
| `base_topic/cmnd/light` | Display > HA | Light control commands |
| `base_topic/cmnd/switch` | Display > HA | Switch control commands |
| `base_topic/cmnd/media` | Display > HA | Media player commands |
| `base_topic/cmnd/scene` | Display > HA | Scene activation |

## Requirements

- Home Assistant 2025.11 or newer
- MQTT broker configured in Home Assistant
- [ESP32-P4 HomeAssistant Display](https://github.com/GalusPeres/ESP32-P4-HomeAssistant-Display) firmware

## Release Process

- Bump `custom_components/tab5_lvgl/manifest.json` version
- Commit and push to `main`
- Create a GitHub release with a `v*` tag (e.g. `v0.5.18`)

## License

MIT License
