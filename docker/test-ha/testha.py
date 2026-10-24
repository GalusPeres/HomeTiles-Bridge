#!/usr/bin/env python3
"""Onboard, set up, seed and check the throwaway test Home Assistant (README.md).

Standard library only. Credentials of the test instance live in .secrets/
(git-ignored) and are never printed.

  python3 testha.py all       # onboard + setup + seed + check
  python3 testha.py onboard | setup | seed | check
  python3 testha.py pair <panel-ip>   # then: accept <flow-id>
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SECRETS = HERE / ".secrets" / "auth.json"
BASE = os.environ.get("TEST_HA_URL", "http://127.0.0.1:18123")
CLIENT_ID = f"{BASE}/"
ZONE = ZoneInfo("Europe/Berlin")
HA_PREFIX = "ha/statestream"
BASE_TOPIC = "hometiles-test"
BROKER = "hometiles-test-mqtt"

CAL_FAMILY = "Test Family"
CAL_EMPTY = "Test Empty"
CAL_MANY = "Test Many"

# The contract in the firmware repo, docs-dev/calendar.md.
MAX_EVENTS = 96
MAX_TITLE = 63
MAX_LOCATION = 47
MAX_PAYLOAD = 16384
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")


# --- HTTP ---------------------------------------------------------------

def _request(method: str, path: str, body=None, token: str | None = None, form: bool = False):
  headers = {}
  data = None
  if body is not None:
    if form:
      data = urllib.parse.urlencode(body).encode()
      headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
      data = json.dumps(body).encode()
      headers["Content-Type"] = "application/json"
  if token:
    headers["Authorization"] = f"Bearer {token}"
  request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
  try:
    with urllib.request.urlopen(request, timeout=60) as response:
      raw = response.read().decode()
  except urllib.error.HTTPError as err:
    raise SystemExit(f"{method} {path} -> HTTP {err.code}: {err.read().decode()[:300]}")
  return json.loads(raw) if raw.strip() else None


def _save_secrets(values: dict) -> None:
  SECRETS.parent.mkdir(mode=0o700, exist_ok=True)
  SECRETS.write_text(json.dumps(values))
  SECRETS.chmod(0o600)


def token() -> str:
  if not SECRETS.exists():
    raise SystemExit("not onboarded yet: run `python3 testha.py onboard`")
  stored = json.loads(SECRETS.read_text())
  result = _request("POST", "/auth/token", {
    "grant_type": "refresh_token", "refresh_token": stored["refresh_token"], "client_id": CLIENT_ID,
  }, form=True)
  return result["access_token"]


def wait_for_ha() -> None:
  # The front page answers once HA runs; /api/onboarding is 404 after onboarding.
  for _ in range(100):
    try:
      urllib.request.urlopen(BASE + "/", timeout=5)
      return
    except Exception:
      time.sleep(3)
  raise SystemExit(f"Home Assistant does not answer on {BASE}")


# --- onboarding ---------------------------------------------------------

def onboard() -> None:
  wait_for_ha()
  try:
    with urllib.request.urlopen(BASE + "/api/onboarding", timeout=10) as response:
      steps = {s["step"]: s["done"] for s in json.loads(response.read())}
  except urllib.error.HTTPError as err:
    if err.code != 404:
      raise
    steps = {"user": True, "core_config": True, "analytics": True, "integration": True}
  if steps.get("user"):
    if not SECRETS.exists():
      raise SystemExit("instance is onboarded but .secrets/auth.json is missing: run `reset` (README)")
    print("onboarding: user already done")
    access = token()
  else:
    password = secrets.token_urlsafe(18)
    result = _request("POST", "/api/onboarding/users", {
      "client_id": CLIENT_ID, "name": "Test", "username": "test", "password": password, "language": "en",
    })
    tokens = _request("POST", "/auth/token", {
      "grant_type": "authorization_code", "code": result["auth_code"], "client_id": CLIENT_ID,
    }, form=True)
    _save_secrets({"username": "test", "password": password, "refresh_token": tokens["refresh_token"]})
    access = tokens["access_token"]
    print("onboarding: user created (credentials in .secrets/auth.json)")
  for step, body in (("core_config", {}), ("analytics", {}),
                     ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID})):
    if not steps.get(step):
      _request("POST", f"/api/onboarding/{step}", body, token=access)
      print(f"onboarding: {step} done")


# --- config flows -------------------------------------------------------

def _drive_flow(access: str, kind: str, start: dict, answers: dict) -> dict:
  """Runs a config/options flow; `answers` maps step_id to a menu choice or form data."""
  base = f"/api/config/config_entries/{kind}"
  result = _request("POST", base, start, token=access)
  for _ in range(10):
    kind_ = result.get("type")
    if kind_ == "create_entry":
      return result
    if kind_ == "abort":
      raise SystemExit(f"flow {start} aborted: {result.get('reason')}")
    step = result.get("step_id")
    if step not in answers:
      raise SystemExit(f"flow {start}: no answer for step {step!r} ({kind_}): {json.dumps(result)[:400]}")
    if result.get("errors"):
      raise SystemExit(f"flow {start}: step {step} errors {result['errors']}")
    answer = answers[step]
    body = {"next_step_id": answer} if kind_ == "menu" else answer
    result = _request("POST", f"{base}/{result['flow_id']}", body, token=access)
  raise SystemExit(f"flow {start} did not finish")


def _entries(access: str, domain: str) -> list:
  return [e for e in _request("GET", "/api/config/config_entries/entry", token=access) if e["domain"] == domain]


def _states(access: str) -> list:
  return _request("GET", "/api/states", token=access)


def _calendar_id(access: str, name: str) -> str:
  for state in _states(access):
    if state["entity_id"].startswith("calendar.") and state["attributes"].get("friendly_name") == name:
      return state["entity_id"]
  raise SystemExit(f"no calendar entity named {name!r}")


def setup() -> None:
  access = token()
  if not _entries(access, "mqtt"):
    _drive_flow(access, "flow", {"handler": "mqtt"}, {"broker": {
      "broker": "mosquitto", "port": 1883,
      "other_settings": {"set_client_cert": False, "set_ca_cert": "off", "transport": "tcp"},
    }})
    print("setup: MQTT -> mosquitto:1883")
  existing = {e["title"] for e in _entries(access, "local_calendar")}
  for name in (CAL_FAMILY, CAL_EMPTY, CAL_MANY):
    if name not in existing:
      _drive_flow(access, "flow", {"handler": "local_calendar"}, {"user": {"calendar_name": name}})
      print(f"setup: local calendar {name!r}")
  time.sleep(2)
  calendars = [_calendar_id(access, n) for n in (CAL_FAMILY, CAL_EMPTY, CAL_MANY)]
  bridge = _entries(access, "tab5_lvgl")
  if not bridge:
    _drive_flow(access, "flow", {"handler": "tab5_lvgl"}, {
      "user": "manual", "manual": {"base_topic": BASE_TOPIC, "ha_prefix": HA_PREFIX},
    })
    print(f"setup: Bridge entry (base {BASE_TOPIC}, prefix {HA_PREFIX})")
    bridge = _entries(access, "tab5_lvgl")
  for entry in bridge:
    _drive_flow(access, "options/flow", {"handler": entry["entry_id"]}, {
      "init": "entities", "entities": {"calendars": calendars},
    })
  print(f"setup: Bridge calendars {calendars}")


# --- seeding ------------------------------------------------------------

def _window(today: date) -> tuple[date, date]:
  """calendar_events.fetch_window, restated from the contract."""
  first = today.replace(day=1)
  index = first.year * 12 + first.month - 1 + 3
  return first - timedelta(days=7), date(index // 12, index % 12 + 1, 1) - timedelta(days=1) + timedelta(days=7)


def _dst_day(start: date, end: date) -> date | None:
  day = start
  while day < end:
    a = datetime.combine(day, datetime.min.time(), ZONE).utcoffset()
    b = datetime.combine(day + timedelta(days=1), datetime.min.time(), ZONE).utcoffset()
    if a != b:
      return day
    day += timedelta(days=1)
  return None


def family_events(today: date) -> list[tuple[dict, dict]]:
  """(create_event data, expected payload entry) for the family calendar."""
  d = lambda n: today + timedelta(days=n)
  start, end = _window(today)
  long_title = "Elternabend Grundschule – Jahrgangsstufe 3 b, Raum 12 (Schäfer)"  # > 63 bytes
  long_place = "Großer Saal im Bürgerhaus, Müllerstraße 101, Hinterhof"  # > 47 bytes
  cases = [
    ({"summary": "Bio waste", "start_date": d(0), "end_date": d(1)},
     {"s": d(0).isoformat(), "e": d(1).isoformat(), "t": "Bio waste"}),
    ({"summary": "Holidays", "start_date": d(2), "end_date": d(6)},
     {"s": d(2).isoformat(), "e": d(6).isoformat(), "t": "Holidays"}),
    ({"summary": "Swimming", "location": "Pool", "start_date_time": f"{d(0)} 10:00:00", "end_date_time": f"{d(0)} 11:30:00"},
     {"s": f"{d(0)}T10:00", "e": f"{d(0)}T11:30", "t": "Swimming", "l": "Pool"}),
    ({"summary": "Night shift", "start_date_time": f"{d(1)} 22:30:00", "end_date_time": f"{d(2)} 01:15:00"},
     {"s": f"{d(1)}T22:30", "e": f"{d(2)}T01:15", "t": "Night shift"}),
    ({"summary": "Until midnight", "start_date_time": f"{d(3)} 20:00:00", "end_date_time": f"{d(4)} 00:00:00"},
     {"s": f"{d(3)}T20:00", "e": f"{d(4)}T00:00", "t": "Until midnight"}),
    ({"summary": long_title, "location": long_place, "start_date_time": f"{d(5)} 19:00:00", "end_date_time": f"{d(5)} 21:00:00"},
     {"s": f"{d(5)}T19:00", "e": f"{d(5)}T21:00", "t": _clip(long_title, MAX_TITLE), "l": _clip(long_place, MAX_LOCATION)}),
    # Started before the window and still running in it.
    ({"summary": "Renovation", "start_date": start - timedelta(days=10), "end_date": start + timedelta(days=5)},
     {"s": (start - timedelta(days=10)).isoformat(), "e": (start + timedelta(days=5)).isoformat(), "t": "Renovation"}),
  ]
  dst = _dst_day(today, end)
  if dst:
    # 01:30 local before the change to 04:00 after it.
    cases.append(({"summary": "Clock change", "start_date_time": f"{dst} 01:30:00", "end_date_time": f"{dst} 04:00:00"},
                  {"s": f"{dst}T01:30", "e": f"{dst}T04:00", "t": "Clock change"}))
  return cases


def _clip(text: str, limit: int) -> str:
  return text.encode()[:limit].decode("utf-8", errors="ignore").rstrip()


def _event_count(access: str, entity_id: str, today: date) -> int:
  start, end = _window(today)
  begin = datetime.combine(start - timedelta(days=30), datetime.min.time(), ZONE).isoformat()
  finish = datetime.combine(end + timedelta(days=1), datetime.min.time(), ZONE).isoformat()
  result = _request("POST", "/api/services/calendar/get_events?return_response", {
    "entity_id": entity_id, "start_date_time": begin, "end_date_time": finish,
  }, token=access)
  return len(result["service_response"][entity_id]["events"])


def _create(access: str, entity_id: str, data: dict) -> None:
  body = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in data.items()}
  _request("POST", "/api/services/calendar/create_event", {"entity_id": entity_id, **body}, token=access)


def seed() -> None:
  access = token()
  today = datetime.now(ZONE).date()
  family = _calendar_id(access, CAL_FAMILY)
  if _event_count(access, family, today) == 0:
    for data, _ in family_events(today):
      _create(access, family, data)
    print(f"seed: {family} events created")
  many = _calendar_id(access, CAL_MANY)
  if _event_count(access, many, today) == 0:
    start, _ = _window(today)
    for i in range(130):
      day = start + timedelta(days=i // 2)
      hour = 8 + 2 * (i % 2)
      _create(access, many, {"summary": f"Slot {i:03d}", "start_date_time": f"{day} {hour:02d}:00:00",
                             "end_date_time": f"{day} {hour:02d}:45:00"})
    print(f"seed: {many} 130 events created")


# --- check --------------------------------------------------------------

def _retained(topic_filter: str) -> dict:
  out = subprocess.run(
    ["docker", "exec", BROKER, "mosquitto_sub", "-t", topic_filter, "-v", "-W", "3", "--retained-only"],
    capture_output=True, text=True,
  ).stdout
  messages = {}
  for line in out.splitlines():
    topic, _, payload = line.partition(" ")
    messages[topic] = payload
  return messages


def _validate(topic: str, text: str, today: date) -> list[str]:
  problems = []
  if len(text.encode()) > MAX_PAYLOAD:
    problems.append(f"payload {len(text.encode())} bytes > {MAX_PAYLOAD}")
  body = json.loads(text)
  start, end = _window(today)
  if body.get("v") != 1:
    problems.append(f"v={body.get('v')!r}")
  if not isinstance(body.get("state"), str):
    problems.append("state is not a string")
  if body.get("from") != start.isoformat():
    problems.append(f"from {body.get('from')} != {start}")
  to = date.fromisoformat(body["to"])
  if to > end or to < start or (to - start).days > 400:
    problems.append(f"to {to} outside {start}..{end}")
  events = body.get("e", [])
  if len(events) > MAX_EVENTS:
    problems.append(f"{len(events)} events > {MAX_EVENTS}")
  for event in events:
    s, e = event.get("s", ""), event.get("e", "")
    if not STAMP.match(s) or not STAMP.match(e) or (len(s) == 10) != (len(e) == 10):
      problems.append(f"bad stamps {s} / {e}")
    elif e < s:
      problems.append(f"end before start {s} / {e}")
    if date.fromisoformat(s[:10]) > to:
      problems.append(f"event {s} after to {to}")
    if len(event.get("t", "").encode()) > MAX_TITLE:
      problems.append(f"title over {MAX_TITLE} bytes")
    if len(event.get("l", "").encode()) > MAX_LOCATION:
      problems.append(f"location over {MAX_LOCATION} bytes")
  return problems


def check() -> int:
  access = token()
  today = datetime.now(ZONE).date()
  _request("POST", "/api/services/tab5_lvgl/publish_snapshot", {}, token=access)
  time.sleep(5)
  messages = _retained(f"{HA_PREFIX}/calendar/+/events")
  ids = {name: _calendar_id(access, name) for name in (CAL_FAMILY, CAL_EMPTY, CAL_MANY)}
  failures = 0
  for name, entity_id in ids.items():
    topic = f"{HA_PREFIX}/calendar/{entity_id.split('.', 1)[1]}/events"
    text = messages.get(topic)
    if text is None:
      print(f"FAIL {topic}: no retained payload")
      failures += 1
      continue
    problems = _validate(topic, text, today)
    body = json.loads(text)
    if name == CAL_FAMILY:
      expected = sorted((e for _, e in family_events(today)),
                        key=lambda e: (e["s"][:10], len(e["s"]) > 10, e["s"], e["t"]))
      if body["e"] != expected:
        problems.append("events differ from the seed:\n    got      " + json.dumps(body["e"], ensure_ascii=False)
                        + "\n    expected " + json.dumps(expected, ensure_ascii=False))
    elif name == CAL_EMPTY and body["e"]:
      problems.append("empty calendar has events")
    elif name == CAL_MANY:
      to = date.fromisoformat(body["to"])
      start, _ = _window(today)
      if len(body["e"]) != 2 * ((to - start).days + 1):
        problems.append(f"{len(body['e'])} events up to {to}: days dropped inside the window")
    status = "FAIL" if problems else "ok  "
    failures += bool(problems)
    print(f"{status} {topic}: {len(text.encode())} bytes, {len(body['e'])} events, "
          f"state {body['state']}, {body['from']}..{body['to']}")
    for problem in problems:
      print(f"     - {problem}")
  return 1 if failures else 0


# --- pairing a test panel over the direct link ----------------------------

def pair(panel_host: str) -> None:
  """Adds a panel by IP (tap Pair on it first); prints the number to compare."""
  access = token()
  flows = "/api/config/config_entries/flow"
  result = _request("POST", flows, {"handler": "tab5_lvgl"}, token=access)
  result = _request("POST", f"{flows}/{result['flow_id']}", {"next_step_id": "link_ip"}, token=access)
  result = _request("POST", f"{flows}/{result['flow_id']}", {"panel_host": panel_host}, token=access)
  if result.get("errors"):
    raise SystemExit(f"panel at {panel_host}: {result['errors']}")
  flow_id = result["flow_id"]
  if result.get("step_id") in ("link_confirm", "link_switch"):
    result = _request("POST", f"{flows}/{flow_id}", {}, token=access)
  if result.get("errors"):
    raise SystemExit(f"link to {panel_host}: {result['errors']} (was Pair tapped on the panel?)")
  print(f"link address sent to {panel_host}; waiting for the panel ...")
  for _ in range(60):
    if result.get("type") == "menu" and result.get("step_id") == "link_number":
      print(f"number: {result['description_placeholders']['number']}  (flow {flow_id})")
      print(f"same number on the panel? then: python3 testha.py accept {flow_id}")
      return
    if result.get("type") in ("abort", "create_entry") or result.get("step_id") == "link_failed":
      raise SystemExit(f"pairing ended: {result.get('step_id') or result.get('reason')}")
    time.sleep(2)
    result = _request("GET", f"{flows}/{flow_id}", token=access)
  raise SystemExit("the panel did not answer")


def accept(flow_id: str) -> None:
  access = token()
  result = _request("POST", f"/api/config/config_entries/flow/{flow_id}", {"next_step_id": "link_accept"},
                    token=access)
  print(f"pairing: {result.get('type')} {result.get('title') or result.get('step_id') or result.get('reason')}")


def main(argv: list[str]) -> int:
  command = argv[1] if len(argv) > 1 else "all"
  if command == "pair" and len(argv) > 2:
    pair(argv[2])
  elif command == "accept" and len(argv) > 2:
    accept(argv[2])
  elif command == "onboard":
    onboard()
  elif command == "setup":
    setup()
  elif command == "seed":
    seed()
  elif command == "check":
    return check()
  elif command == "all":
    onboard()
    setup()
    seed()
    return check()
  else:
    print(__doc__)
    return 2
  return 0


if __name__ == "__main__":
  sys.exit(main(sys.argv))
