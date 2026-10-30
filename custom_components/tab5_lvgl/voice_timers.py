"""Voice timers: Home Assistant's timer intents shown and controlled on the panel.

Home Assistant sends timer events ("set a timer for 10 minutes") only to the
device that started the timer, and only when that device registered a timer
handler. The panel's Assist satellite registers one (assist_satellite.py),
keeps this panel's timers here and passes them on with the Assist command
(sealed when paired). The panel's touch controls come back as requests.

Contract v1, the firmware's src/audio/voice_timer_contract.h:
  {base}/cmnd/assist   Bridge -> panel:
      {"v":1,"op":"timers","timers":[{"id":..,"total":600,"left":272,
          "active":true,"name":"Pasta"},...]}   on every change and connect
      {"v":1,"op":"timer_done","id":..,"total":600,"name":"Pasta"}
      {"v":1,"op":"timer_sound","sound":"rising_three","volume":60}
  {base}/stat/assist_timer   panel -> Bridge, not retained:
      {"v":1,"op":"start","seconds":300[,"name":"Pasta"]}
      {"v":1,"op":"pause"|"resume"|"cancel"|"speak"|"play_sound","id":..}
      {"v":1,"op":"add","id":..,"seconds":60}

The panel reads the keys with a small scanner: "op" comes before the list,
each timer's keys keep the order above and the name comes last.

The finished-timer sound is a built-in chime the panel generates itself, a
sound file of the user's own from <config>/hometiles/sounds (the Bridge
plays it when the panel asks), or off.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import os
from typing import Any

TIMER_PROTOCOL_VERSION = 1
ASSIST_TIMER_LEAF = "assist_timer"
MAX_TIMERS = 8
MAX_ID_CHARS = 32
MAX_NAME_BYTES = 48
# Longest timer a panel request may start or add (24 h).
MAX_SECONDS = 24 * 3600
REQUEST_MAX_PAYLOAD = 256
# Finished timers kept for the panel's "speak" request.
MAX_FINISHED = 8

BUILTIN_SOUNDS = ("rising_three", "kitchen_beep", "soft_bell", "marimba", "westminster")
SOUND_OFF = "off"
SOUND_BRIDGE = "bridge"
DEFAULT_SOUND = BUILTIN_SOUNDS[0]
PANEL_SOUNDS = frozenset((*BUILTIN_SOUNDS, SOUND_OFF, SOUND_BRIDGE))

SOUNDS_DIR = ("hometiles", "sounds")
SOUND_EXTENSIONS = (".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a")
MAX_SOUND_FILES = 32
MAX_SOUND_FILE_BYTES = 4 * 1024 * 1024
TIMER_SOUNDS_KEY = "timer_sounds"
TIMER_VOLUMES_KEY = "timer_volumes"
# Timer volume in percent: its own level, apart from the media volume.
DEFAULT_VOLUME = 60

_ID_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_ACTIONS = frozenset({"pause", "resume", "cancel", "speak", "play_sound"})

# What Home Assistant says once the panel rang: (named, unnamed), by language.
_DONE_PHRASES = {
    "en": ("{name} timer is done", "Your timer is done"),
    "de": ("Der Timer {name} ist abgelaufen", "Dein Timer ist abgelaufen"),
    "fr": ("Le minuteur {name} est terminé", "Votre minuteur est terminé"),
    "es": ("El temporizador {name} ha terminado", "Tu temporizador ha terminado"),
    "it": ("Il timer {name} è terminato", "Il tuo timer è terminato"),
    "nl": ("De timer {name} is afgelopen", "Je timer is afgelopen"),
    "pl": ("Minutnik {name} się skończył", "Twój minutnik się skończył"),
    "cs": ("Časovač {name} skončil", "Váš časovač skončil"),
    "sk": ("Časovač {name} skončil", "Váš časovač skončil"),
    "pt": ("O temporizador {name} terminou", "O seu temporizador terminou"),
    "sv": ("Timern {name} är klar", "Din timer är klar"),
    "ru": ("Таймер {name} завершён", "Ваш таймер завершён"),
}


def assist_timer_topic(base_topic: str) -> str:
    return f"{base_topic}/stat/{ASSIST_TIMER_LEAF}"


def timer_sound_select_unique_id(device_id: str) -> str:
    return f"{device_id}_timer_sound"


def timer_volume_number_unique_id(device_id: str) -> str:
    return f"{device_id}_timer_volume"


def clamp_volume(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return DEFAULT_VOLUME
    return max(0, min(100, int(round(value))))


def valid_timer_id(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_ID_CHARS
            and all(char in _ID_CHARS for char in value))


def cut_utf8(text: str, limit: int) -> str:
    """At most limit UTF-8 bytes, never cutting a character in half."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(MAX_SECONDS, int(value)))


def clean_name(name: Any) -> str:
    """A timer name without control characters, at most MAX_NAME_BYTES."""
    if not isinstance(name, str):
        return ""
    # Control characters never reach the panel's labels or Home Assistant.
    name = "".join(char for char in name.strip() if char >= " ")
    return cut_utf8(name, MAX_NAME_BYTES)


def _name(timer: Any) -> str:
    return clean_name(getattr(timer, "name", None))


def _encode(payload: dict[str, Any]) -> str:
    # Raw UTF-8: names keep their size limit on the panel.
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def timer_entry(timer: Any) -> dict[str, Any]:
    """One timer for the panel, keys in the order its scanner expects."""
    entry: dict[str, Any] = {
        "id": timer.id,
        "total": _seconds(timer.created_seconds),
        "left": _seconds(timer.seconds_left),
        "active": bool(timer.is_active),
    }
    name = _name(timer)
    if name:
        entry["name"] = name
    return entry


def build_timers_command(timers: list[Any]) -> str:
    entries = [timer_entry(timer) for timer in timers if valid_timer_id(getattr(timer, "id", None))]
    # The panel shows at most MAX_TIMERS: the soonest ones, paused ones last.
    entries.sort(key=lambda entry: (not entry["active"], entry["left"]))
    return _encode({"v": TIMER_PROTOCOL_VERSION, "op": "timers", "timers": entries[:MAX_TIMERS]})


def build_done_command(timer: Any) -> str:
    if not valid_timer_id(getattr(timer, "id", None)):
        raise ValueError("invalid_timer_id")
    payload: dict[str, Any] = {"v": TIMER_PROTOCOL_VERSION, "op": "timer_done", "id": timer.id,
                               "total": _seconds(timer.created_seconds)}
    name = _name(timer)
    if name:
        payload["name"] = name
    return _encode(payload)


def build_sound_command(sound: str, volume: Any = DEFAULT_VOLUME) -> str:
    if sound not in PANEL_SOUNDS:
        raise ValueError("invalid_timer_sound")
    return _encode({"v": TIMER_PROTOCOL_VERSION, "op": "timer_sound", "sound": sound,
                    "volume": clamp_volume(volume)})


def parse_request(payload: Any) -> dict[str, Any] | None:
    """A request of the panel's timer controls; None when invalid."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str) or not payload or len(payload) > REQUEST_MAX_PAYLOAD:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != TIMER_PROTOCOL_VERSION:
        return None
    op = data.get("op")
    seconds = data.get("seconds")
    valid_seconds = (isinstance(seconds, int) and not isinstance(seconds, bool)
                     and 0 < seconds <= MAX_SECONDS)
    if op == "start":
        if not valid_seconds:
            return None
        name = data.get("name")
        if name is not None and not isinstance(name, str):
            return None
        return {"op": op, "seconds": seconds, "name": clean_name(name) or None}
    timer_id = data.get("id")
    if not valid_timer_id(timer_id):
        return None
    if op == "add":
        return {"op": op, "id": timer_id, "seconds": seconds} if valid_seconds else None
    if op in _ACTIONS:
        return {"op": op, "id": timer_id}
    return None


def done_phrase(name: str | None, language: str | None) -> str:
    """The sentence Home Assistant speaks once the panel rang."""
    code = (language or "en").replace("_", "-").lower()
    phrases = _DONE_PHRASES.get(code) or _DONE_PHRASES.get(code.split("-")[0]) or _DONE_PHRASES["en"]
    named, unnamed = phrases
    return named.format(name=name) if name else unnamed


def valid_sound_file_name(name: Any) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 64 and not name.startswith(".")
            and os.path.basename(name) == name and name.lower().endswith(SOUND_EXTENSIONS)
            and all(char >= " " for char in name))


def list_sound_files(directory: str) -> list[str]:
    """The user's own timer sounds (blocking; run in the executor)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    files = []
    for name in names:
        if name in BUILTIN_SOUNDS or name == SOUND_OFF or not valid_sound_file_name(name):
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
        except OSError:
            continue
        if 0 < size <= MAX_SOUND_FILE_BYTES:
            files.append(name)
    return sorted(files, key=str.casefold)[:MAX_SOUND_FILES]


def sound_file_usable(path: str) -> bool:
    """An own sound file that is still there and small enough (blocking)."""
    try:
        return os.path.isfile(path) and 0 < os.path.getsize(path) <= MAX_SOUND_FILE_BYTES
    except OSError:
        return False


def panel_sound(option: str | None) -> str:
    """The panel's sound id for a Timer sound option (an own file = "bridge")."""
    if option in BUILTIN_SOUNDS or option == SOUND_OFF:
        return option
    if valid_sound_file_name(option):
        return SOUND_BRIDGE
    return DEFAULT_SOUND


class PanelTimers:
    """This panel's running and paused Home Assistant timers.

    Holds Home Assistant's own TimerInfo objects, so the seconds left are
    current whenever a list is built. Finished timers stay a little while
    for the panel's "speak" request.
    """

    def __init__(self) -> None:
        self._timers: OrderedDict[str, Any] = OrderedDict()
        self._finished: OrderedDict[str, tuple[str | None, str | None]] = OrderedDict()

    def handle_event(self, event_type: Any, timer: Any) -> str | None:
        """Apply one timer event: "list" or "done" for what to send, else None."""
        timer_id = getattr(timer, "id", None)
        if not valid_timer_id(timer_id):
            return None
        kind = str(getattr(event_type, "value", event_type))
        if kind in ("started", "updated"):
            self._timers[timer_id] = timer
            return "list"
        if kind == "cancelled":
            return "list" if self._timers.pop(timer_id, None) is not None else None
        if kind == "finished":
            self._timers.pop(timer_id, None)
            self._finished[timer_id] = (_name(timer) or None, getattr(timer, "language", None))
            while len(self._finished) > MAX_FINISHED:
                self._finished.popitem(last=False)
            return "done"
        return None

    def seed(self, timers: Any, device_id: str) -> None:
        """Timers Home Assistant already runs for this device (after a reload)."""
        if not isinstance(timers, dict):
            return
        for timer in timers.values():
            if getattr(timer, "device_id", None) == device_id:
                self.handle_event("started", timer)

    def timers(self) -> list[Any]:
        return list(self._timers.values())

    def owns(self, timer_id: str) -> bool:
        return timer_id in self._timers

    def finished(self, timer_id: str) -> tuple[str | None, str | None] | None:
        return self._finished.get(timer_id)

    def clear(self) -> None:
        self._timers.clear()
        self._finished.clear()
