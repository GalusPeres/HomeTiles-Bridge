# HomeTiles Bridge v0.9.4b17 (beta)

Seventeenth beta: a panel's theme and rotation from Home Assistant.

## Changes

- **Theme** select on the panel's device page: Dark, Light or Automatic (light by day, dark by night), the same choice as on the panel and in its Web Admin. A change made on the panel shows in Home Assistant too.
- **Display Rotation** is now a select with the steps the panel offers: 0°, 90°, 180° and 270° on panels that turn in quarter turns, 0° and 180° on the others. It replaces the old switch, which only knew 180°.

## Update Notes

Beta: install through HACS with "Show beta versions". Both selects need HomeTiles firmware test build b530 or newer; with it the old Display Rotation switch leaves the device page. Panels with older firmware keep the switch and show no Theme select.

## Validation

Automated tests passed (830). Not yet checked in a Home Assistant installation or on a panel.
