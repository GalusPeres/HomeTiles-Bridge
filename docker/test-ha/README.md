# Test Home Assistant

A throwaway Home Assistant with a Mosquitto broker for trying the Bridge
without a production system. The Bridge source (`custom_components/tab5_lvgl`)
is mounted read-only, so the instance always runs the checked-out code
(restart the container after a change).

| What | Where |
| --- | --- |
| Home Assistant 2026.9.4, time zone Europe/Berlin | http://localhost:18123 |
| Mosquitto (anonymous, no persistence) | `localhost:11883` (inside Docker: `mosquitto:1883`) |
| Bridge link / camera servers | ports 8140 / 8124 |

## Start, set up, check

```bash
docker compose up -d
```

```bash
python3 testha.py all
```

`testha.py` (standard library only) runs these steps; each can run alone and
skips what is already done:

- `onboard`: creates the user `test` with a random password and stores it and
  a refresh token in `.secrets/auth.json` (git-ignored, mode 600). Log in to
  the web UI with those.
- `setup`: MQTT (to `mosquitto:1883`), three `local_calendar` calendars
  (`Test Family`, `Test Empty`, `Test Many`), a manual Bridge entry
  (base topic `hometiles-test`, prefix `ha/statestream`) and the three
  calendars in the Bridge's entity options.
- `seed`: events relative to today in `Test Family` (all-day, multi-day, timed,
  over midnight, ending at midnight, a long UTF-8 title and location, one that
  started before the window, and one across the next DST change when it falls
  in the window), and 130 short events in `Test Many` (more than the panel's
  96).
- `check`: calls `tab5_lvgl.publish_snapshot` and checks the retained
  `ha/statestream/calendar/<object>/events` topics against the contract in
  the firmware repo (`docs-dev/calendar.md`): version, window, bounds, stamp
  format, the exact expected `Test Family` events, and that `Test Many` moves
  `to` back without dropping days inside it. Exit code 1 on a mismatch.

Look at a payload yourself:

```bash
docker exec hometiles-test-mqtt mosquitto_sub -t 'ha/statestream/calendar/+/events' -v -W 3 --retained-only
```

## A test panel

The panel pairs over the Bridge's direct link (ports 8140–8147, camera
8124–8131), so it needs no MQTT broker for pairing. Inside Docker the Bridge
would hand the panel the container's address; `HOMETILES_ADVERTISE_HOST`
(set from `.env`) makes it send this Mac's LAN address instead. Before the
first start, and whenever the Mac's address changes:

```bash
echo "HOST_LAN_IP=$(ipconfig getifaddr en0)" > .env && docker compose up -d
```

1. Unpair the panel from the production Home Assistant first: on the panel,
   Settings → System → Security → Pairing → Unpair. If the production Home
   Assistant then shows "Panel found" for it, choose Ignore there.
2. Tap Pair on the panel. It accepts a link address for two minutes.
3. Within those two minutes, add it by its IP address (mDNS does not reach the
   container):

   ```bash
   python3 testha.py pair 192.168.20.88
   ```

   or in the web UI: Settings → Devices → Add integration → HomeTiles Bridge
   → by IP address.
4. Compare the number with the one on the panel, then accept it on the panel
   and here (`python3 testha.py accept <flow-id>` or the web UI).
5. Run `python3 testha.py setup` again so the new entry gets the test
   calendars, then add a Calendar tile on the panel.

The Bridge announces `calendars` in its configuration only once a panel is
paired. The macOS firewall must let incoming connections reach Docker
(it is off on this Mac).

## Tear down

```bash
docker compose down
```

To start from scratch (new onboarding), also delete what Home Assistant wrote
and the test credentials:

```bash
docker compose down && git clean -fdX config .secrets
```
