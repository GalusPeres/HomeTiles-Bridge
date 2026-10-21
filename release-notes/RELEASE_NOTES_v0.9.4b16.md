# HomeTiles Bridge v0.9.4b16 (beta)

Sixteenth beta: add a panel by its IP address, and a quieter View select.

## Changes

- **Add device** now asks how: **Panel by IP address (direct connection)** or **Manually over MQTT** (the form from before). Where Home Assistant cannot find panels over mDNS (Docker without host networking, VLANs), press Pair on the panel (Settings > System > Security), choose the IP address and enter it. The panel shows it under Settings > WiFi; a fixed address in the router helps. The rest is the same as for a panel found over mDNS: "Panel found", then the same six-digit number on the panel and in Home Assistant. Without Pair pressed, the dialog says so.
- The View select of a panel changes its option list at most once a minute. A backup import or a long redesign changed it with every tile, and Home Assistant logged "updating its capabilities too often". A new list still shows at once when the last change is older than a minute.

## Update Notes

Beta: install through HACS with "Show beta versions". Adding a panel by its IP address needs HomeTiles firmware test build b513 or newer; older firmware answers that it has to be updated first. Panels found over mDNS and panels on MQTT work as before.

## Validation

Automated tests passed (819). Not yet checked in a Home Assistant installation or on a panel.
