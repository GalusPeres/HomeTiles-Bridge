"""Dependency-free tests for the Lock and Alarm panel tile rules."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


def _load(name: str):
  path = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl" / f"{name}.py"
  spec = importlib.util.spec_from_file_location(f"_hometiles_{name}", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


ACCESS = _load("access_helpers")
NOW = 1_760_000_000.0


def _command(**fields):
  body = {"entity_id": "lock.front_door", "id": "c1", "deadline": NOW + 5, "action": "unlock"}
  body.update(fields)
  return json.dumps({key: value for key, value in body.items() if value is not None})


class _HAError(Exception):
  def __init__(self, message: str = "", translation_key: str | None = None) -> None:
    super().__init__(message)
    self.translation_key = translation_key


class FeatureValuesTest(unittest.TestCase):
  def test_feature_bits_match_home_assistant(self) -> None:
    self.assertEqual(ACCESS.LOCK_OPEN, 1)
    self.assertEqual(
      (ACCESS.ALARM_ARM_HOME, ACCESS.ALARM_ARM_AWAY, ACCESS.ALARM_ARM_NIGHT,
       ACCESS.ALARM_ARM_CUSTOM_BYPASS, ACCESS.ALARM_ARM_VACATION),
      (1, 2, 4, 16, 32),
    )

  def test_trigger_is_never_offered(self) -> None:
    self.assertNotIn("trigger", ACCESS.ALARM_ACTIONS)
    command = ACCESS.parse_access_command(
      _command(entity_id="alarm_control_panel.home", action="trigger"), NOW)
    with self.assertRaises(ACCESS.AccessError) as caught:
      ACCESS.plan_access_call("alarm_control_panel", command, "disarmed",
                              {"supported_features": 63}, has_default_code=False,
                              allowed_without_code=True)
    self.assertEqual(caught.exception.status, "not_allowed")


class AccessCodesTest(unittest.TestCase):
  """Codes the Bridge itself checks for devices that ignore wrong codes."""

  def test_codes_are_split_cleaned_and_limited(self) -> None:
    self.assertEqual(ACCESS.parse_access_codes(" 1234, 5678;1234 \n 9 "), ["1234", "5678", "9"])
    self.assertEqual(ACCESS.parse_access_codes(""), [])
    self.assertEqual(ACCESS.parse_access_codes(None), [])
    for bad in (5, "x" * 33, ",".join(str(i) for i in range(11)), "12\x0034"):
      with self.assertRaises(ValueError, msg=repr(bad)):
        ACCESS.parse_access_codes(bad)

  def test_only_a_known_code_matches(self) -> None:
    self.assertTrue(ACCESS.access_code_matches("5678", ["1234", "5678"]))
    for wrong in ("567", "56789", "", None, 5678):
      self.assertFalse(ACCESS.access_code_matches(wrong, ["1234", "5678"]), repr(wrong))
    self.assertFalse(ACCESS.access_code_matches("1234", []))


class CodeFormatTest(unittest.TestCase):
  def test_lock_regex_is_classified_like_home_assistant_matches_it(self) -> None:
    self.assertEqual(ACCESS.lock_code_format(r"^\d{4}$"), "number")
    self.assertEqual(ACCESS.lock_code_format(r"^\d{4,8}$"), "number")
    self.assertEqual(ACCESS.lock_code_format(r"^[A-Z]{4}$"), "text")
    self.assertEqual(ACCESS.lock_code_format("[unclosed"), "text")
    self.assertIsNone(ACCESS.lock_code_format(None))
    self.assertIsNone(ACCESS.lock_code_format(""))

  def test_alarm_code_format_accepts_only_home_assistant_values(self) -> None:
    self.assertEqual(ACCESS.alarm_code_format("number"), "number")
    self.assertEqual(ACCESS.alarm_code_format("text"), "text")
    self.assertIsNone(ACCESS.alarm_code_format("digits"))
    self.assertIsNone(ACCESS.alarm_code_format(None))


class DetailPayloadTest(unittest.TestCase):
  def test_lock_with_code_needs_it_for_lock_and_unlock(self) -> None:
    detail = ACCESS.build_lock_detail("locked", {"code_format": r"^\d{4}$", "supported_features": 1},
                                      has_default_code=False, allowed_without_code=False)
    self.assertEqual(detail, {
      "state": "locked", "supported_features": 1, "code_format": "number",
      "lock_code": True, "unlock_code": True, "unlock_allowed": True,
    })

  def test_lock_without_code_cannot_be_unlocked_unless_allowed(self) -> None:
    blocked = ACCESS.build_lock_detail("locked", {}, has_default_code=False, allowed_without_code=False)
    self.assertFalse(blocked["unlock_code"])
    self.assertFalse(blocked["unlock_allowed"])
    allowed = ACCESS.build_lock_detail("locked", {}, has_default_code=False, allowed_without_code=True)
    self.assertTrue(allowed["unlock_allowed"])

  def test_default_code_removes_the_prompt_but_needs_permission(self) -> None:
    detail = ACCESS.build_lock_detail("locked", {"code_format": r"^\d{4}$"},
                                      has_default_code=True, allowed_without_code=False)
    self.assertFalse(detail["lock_code"])
    self.assertFalse(detail["unlock_code"])
    self.assertFalse(detail["unlock_allowed"])

  def test_missing_entity_is_null_not_unavailable(self) -> None:
    detail = ACCESS.build_lock_detail(None, {}, has_default_code=False, allowed_without_code=False)
    self.assertIsNone(detail["state"])
    self.assertEqual(detail["supported_features"], 0)

  def test_alarm_arm_code_follows_code_arm_required(self) -> None:
    required = ACCESS.build_alarm_detail(
      "disarmed", {"code_format": "number", "supported_features": 3},
      has_default_code=False, allowed_without_code=False)
    self.assertEqual((required["arm_code"], required["disarm_code"], required["disarm_allowed"]),
                     (True, True, True))
    optional = ACCESS.build_alarm_detail(
      "disarmed", {"code_format": "number", "code_arm_required": False},
      has_default_code=False, allowed_without_code=False)
    self.assertFalse(optional["arm_code"])
    self.assertTrue(optional["disarm_code"])

  def test_alarm_without_code_cannot_be_disarmed_unless_allowed(self) -> None:
    detail = ACCESS.build_alarm_detail("armed_away", {"code_format": None},
                                       has_default_code=False, allowed_without_code=False)
    self.assertEqual((detail["arm_code"], detail["disarm_code"], detail["disarm_allowed"]),
                     (False, False, False))

  def test_invalid_feature_masks_become_zero(self) -> None:
    for value in (True, -1, "3", None):
      detail = ACCESS.build_alarm_detail("disarmed", {"supported_features": value},
                                         has_default_code=False, allowed_without_code=False)
      self.assertEqual(detail["supported_features"], 0)


class ParseCommandTest(unittest.TestCase):
  def test_valid_command(self) -> None:
    command = ACCESS.parse_access_command(_command(code="1234", web_auth=True), NOW)
    self.assertEqual(command, {"entity_id": "lock.front_door", "id": "c1",
                               "action": "unlock", "code": "1234", "web_auth": True})
    for claim in (None, False, "true", 1):
      self.assertFalse(ACCESS.parse_access_command(_command(web_auth=claim), NOW)["web_auth"])

  def test_unanswerable_bodies(self) -> None:
    for payload in ("", "not json", "[]", json.dumps({"id": "c1"}),
                    _command(id="bad id"), _command(entity_id="Lock.X"), "x" * 2000):
      with self.assertRaises(ACCESS.AccessError) as caught:
        ACCESS.parse_access_command(payload, NOW)
      self.assertNotIsInstance(caught.exception, ACCESS.AnsweredAccessError)

  def test_deadline_window(self) -> None:
    for deadline in (NOW, NOW - 1, NOW + 16, True, "soon", None, float("nan")):
      with self.assertRaises(ACCESS.AnsweredAccessError) as caught:
        ACCESS.parse_access_command(_command(deadline=deadline), NOW)
      self.assertEqual(caught.exception.status, "expired")
      self.assertEqual(caught.exception.command_id, "c1")

  def test_code_shape(self) -> None:
    for code in ("", "x" * 33, "12\n34", 1234):
      with self.assertRaises(ACCESS.AnsweredAccessError) as caught:
        ACCESS.parse_access_command(_command(code=code), NOW)
      self.assertEqual(caught.exception.status, "invalid")

  def test_errors_never_carry_the_code(self) -> None:
    try:
      ACCESS.parse_access_command(_command(code="98765", deadline=NOW - 1), NOW)
    except ACCESS.AccessError as err:
      self.assertNotIn("98765", str(err))
      self.assertNotIn("98765", repr(err.args))


class PlanCallTest(unittest.TestCase):
  LOCK = {"code_format": r"^\d{4}$", "supported_features": 1}

  def plan(self, domain, action, state, attributes, code=None, *, default=False, allowed=False):
    entity = "lock.front_door" if domain == "lock" else "alarm_control_panel.home"
    command = ACCESS.parse_access_command(_command(entity_id=entity, action=action, code=code), NOW)
    return ACCESS.plan_access_call(domain, command, state, attributes,
                                   has_default_code=default, allowed_without_code=allowed)

  def status(self, *args, **kwargs) -> str:
    with self.assertRaises(ACCESS.AccessError) as caught:
      self.plan(*args, **kwargs)
    return caught.exception.status

  def test_lock_actions_with_code(self) -> None:
    self.assertEqual(self.plan("lock", "unlock", "locked", self.LOCK, "1234"), ("unlock", {"code": "1234"}))
    self.assertEqual(self.plan("lock", "lock", "unlocked", self.LOCK, "1234"), ("lock", {"code": "1234"}))
    self.assertEqual(self.plan("lock", "open", "locked", self.LOCK, "1234"), ("open", {"code": "1234"}))
    self.assertEqual(self.status("lock", "unlock", "locked", self.LOCK), "code_required")
    self.assertEqual(self.status("lock", "lock", "unlocked", self.LOCK), "code_required")

  def test_open_needs_the_feature(self) -> None:
    self.assertEqual(self.status("lock", "open", "locked", {"code_format": r"^\d{4}$"}, "1234"),
                     "unsupported")

  def test_lock_without_code(self) -> None:
    self.assertEqual(self.plan("lock", "lock", "unlocked", {}), ("lock", {}))
    self.assertEqual(self.status("lock", "unlock", "locked", {}), "not_allowed")
    # A code sent anyway must not open a device that cannot check it.
    self.assertEqual(self.status("lock", "unlock", "locked", {}, "1234"), "not_allowed")
    self.assertEqual(self.plan("lock", "unlock", "locked", {}, allowed=True), ("unlock", {}))

  def test_default_code(self) -> None:
    self.assertEqual(self.plan("lock", "lock", "unlocked", self.LOCK, default=True), ("lock", {}))
    self.assertEqual(self.status("lock", "unlock", "locked", self.LOCK, default=True), "not_allowed")
    # A typed code is still checked by Home Assistant.
    self.assertEqual(self.plan("lock", "unlock", "locked", self.LOCK, "1234", default=True),
                     ("unlock", {"code": "1234"}))
    self.assertEqual(self.plan("lock", "unlock", "locked", self.LOCK, default=True, allowed=True),
                     ("unlock", {}))

  def test_unavailable_but_unknown_stays_operable(self) -> None:
    self.assertEqual(self.status("lock", "lock", "unavailable", {}), "unavailable")
    self.assertEqual(self.status("lock", "lock", None, {}), "unavailable")
    self.assertEqual(self.plan("lock", "lock", "unknown", {}), ("lock", {}))

  def test_alarm_modes_follow_feature_bits(self) -> None:
    attributes = {"code_format": "number", "code_arm_required": False, "supported_features": 1 | 2}
    self.assertEqual(self.plan("alarm_control_panel", "arm_away", "disarmed", attributes),
                     ("alarm_arm_away", {}))
    for action in ("arm_night", "arm_vacation", "arm_custom_bypass"):
      self.assertEqual(self.status("alarm_control_panel", action, "disarmed", attributes), "unsupported")
    self.assertEqual(self.status("alarm_control_panel", "disarm", "armed_away", attributes), "code_required")
    self.assertEqual(self.plan("alarm_control_panel", "disarm", "armed_away", attributes, "1234"),
                     ("alarm_disarm", {"code": "1234"}))

  def test_alarm_arm_code_required(self) -> None:
    attributes = {"code_format": "number", "supported_features": 2}
    self.assertEqual(self.status("alarm_control_panel", "arm_away", "disarmed", attributes), "code_required")
    self.assertEqual(self.plan("alarm_control_panel", "arm_away", "disarmed", attributes, "1234"),
                     ("alarm_arm_away", {"code": "1234"}))

  def test_alarm_without_code_disarm_needs_permission(self) -> None:
    attributes = {"supported_features": 2}
    self.assertEqual(self.plan("alarm_control_panel", "arm_away", "disarmed", attributes),
                     ("alarm_arm_away", {}))
    self.assertEqual(self.status("alarm_control_panel", "disarm", "armed_away", attributes), "not_allowed")
    self.assertEqual(self.plan("alarm_control_panel", "disarm", "armed_away", attributes, allowed=True),
                     ("alarm_disarm", {}))

  def test_unknown_actions_and_domains(self) -> None:
    self.assertEqual(self.status("lock", "arm_away", "locked", self.LOCK, "1234"), "not_allowed")
    command = ACCESS.parse_access_command(_command(), NOW)
    with self.assertRaises(ACCESS.AccessError) as caught:
      ACCESS.plan_access_call("switch", command, "on", {}, has_default_code=False,
                              allowed_without_code=True)
    self.assertEqual(caught.exception.status, "not_allowed")


class ServiceErrorTest(unittest.TestCase):
  def test_wrong_codes(self) -> None:
    for error in (
      _HAError("", "invalid_code"),
      _HAError("The code for lock.x doesn't match pattern ^\\d{4}$", "add_default_code"),
      _HAError("Invalid alarm code provided"),
      _HAError("Wrong PIN"),
      _HAError("The code is incorrect"),
    ):
      self.assertEqual(ACCESS.classify_service_error(error), "wrong_code", str(error))

  def test_other_errors(self) -> None:
    self.assertEqual(ACCESS.classify_service_error(_HAError("", "code_arm_required")), "code_required")
    self.assertEqual(ACCESS.classify_service_error(_HAError("Device offline")), "failed")
    self.assertEqual(ACCESS.classify_service_error(_HAError("Invalid response code 500")), "failed")
    self.assertEqual(ACCESS.classify_service_error(TimeoutError()), "failed")


class DefaultCodeTest(unittest.TestCase):
  def test_lock_default_code_must_fit_the_code_format(self) -> None:
    self.assertTrue(ACCESS.default_code_usable("lock", r"^\d{4}$", "1234"))
    self.assertFalse(ACCESS.default_code_usable("lock", r"^\d{4}$", "12"))
    self.assertFalse(ACCESS.default_code_usable("lock", "[unclosed", "1234"))
    self.assertTrue(ACCESS.default_code_usable("lock", None, "1234"))
    self.assertTrue(ACCESS.default_code_usable("alarm_control_panel", "number", "12"))
    for value in (None, "", 1234):
      self.assertFalse(ACCESS.default_code_usable("lock", r"^\d{4}$", value))


class CodeGuardTest(unittest.TestCase):
  def setUp(self) -> None:
    self.now = 100.0
    self.guard = ACCESS.CodeGuard(lambda: self.now)

  def wrong(self, key: str = "lock.a") -> int:
    self.assertEqual(self.guard.admit(key), (None, 0))
    return self.guard.finish(key, "wrong_code")

  def test_five_free_attempts_then_doubling_up_to_an_hour(self) -> None:
    for _ in range(4):
      self.assertEqual(self.wrong(), 0)
    self.assertEqual(self.guard.retry_after("lock.a"), 0)
    self.assertEqual(self.wrong(), 30)
    self.assertEqual(self.guard.admit("lock.a"), ("locked_out", 30))
    self.now += 10
    self.assertEqual(self.guard.retry_after("lock.a"), 20)
    self.now += 20
    self.assertEqual(self.guard.retry_after("lock.a"), 0)
    self.assertEqual(self.wrong(), 60)
    self.now += 60
    self.assertEqual(self.wrong(), 120)
    for _ in range(10):
      self.now += 4000
      seconds = self.wrong()
    self.assertEqual(seconds, 3600)

  def test_only_a_successful_code_resets(self) -> None:
    for _ in range(5):
      self.wrong()
    self.assertGreater(self.guard.retry_after("lock.a"), 0)
    self.assertEqual(self.guard.retry_after("lock.b"), 0)
    self.now += 31
    self.assertEqual(self.guard.admit("lock.a"), (None, 0))
    self.assertEqual(self.guard.finish("lock.a", "ok"), 0)
    self.assertEqual(self.wrong(), 0)
    # Other failures neither count nor reset.
    self.assertEqual(self.guard.admit("lock.a"), (None, 0))
    self.assertEqual(self.guard.finish("lock.a", "failed"), 0)
    self.assertEqual(self.guard._failures["lock.a"], 1)

  def test_one_code_at_a_time(self) -> None:
    self.assertEqual(self.guard.admit("lock.a"), (None, 0))
    self.assertEqual(self.guard.admit("lock.a"), ("busy", 0))
    self.assertEqual(self.guard.admit("lock.b"), (None, 0))
    self.guard.finish("lock.a", "wrong_code")
    self.assertEqual(self.guard.admit("lock.a"), (None, 0))
    # A call Home Assistant never answers frees the entity after a minute.
    self.now += 61
    self.assertEqual(self.guard.admit("lock.a"), (None, 0))

  def test_at_most_ten_codes_per_minute(self) -> None:
    for _ in range(10):
      self.assertEqual(self.guard.admit("lock.a"), (None, 0))
      self.guard.finish("lock.a", "ok")
      self.now += 1
    self.assertEqual(self.guard.admit("lock.a"), ("locked_out", 50))
    self.now += 50
    self.assertEqual(self.guard.admit("lock.a"), (None, 0))

  def test_state_is_bounded(self) -> None:
    guard = ACCESS.CodeGuard(lambda: self.now, max_keys=3)
    for index in range(10):
      guard.admit(f"lock.k{index}")
      guard.finish(f"lock.k{index}", "wrong_code")
    keys = set(guard._failures) | set(guard._attempts) | set(guard._in_flight)
    self.assertLessEqual(len(keys), 3)


if __name__ == "__main__":
  unittest.main()
