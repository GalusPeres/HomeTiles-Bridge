"""HOMETILES_ADVERTISE_HOST: the address handed to a panel behind NAT (docker/test-ha)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from test_view_navigation import ROOT, load_module

HOST = load_module("advertise_host")


class AdvertisedHostTest(unittest.TestCase):
  def test_unset_keeps_the_detected_address(self):
    with mock.patch.dict(os.environ, {}, clear=True):
      self.assertEqual(HOST.advertised_host("172.20.0.3"), "172.20.0.3")
      self.assertEqual(HOST.advertised_host(None), "")
    with mock.patch.dict(os.environ, {HOST.ENV_ADVERTISE_HOST: "  "}):
      self.assertEqual(HOST.advertised_host("172.20.0.3"), "172.20.0.3")

  def test_set_replaces_the_detected_address(self):
    with mock.patch.dict(os.environ, {HOST.ENV_ADVERTISE_HOST: " 192.168.20.180 "}):
      self.assertEqual(HOST.advertised_host("172.20.0.3"), "192.168.20.180")
      self.assertEqual(HOST.advertised_host(None), "192.168.20.180")

  def test_every_address_sent_to_a_panel_uses_it(self):
    flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    start = flow.index("def _source_ip_for_target(")
    self.assertIn("override = advertised_host(None)", flow[start:flow.index("\n  try:", start)],
                  "link address and MQTT broker host for the panel")
    bridge = (ROOT / "__init__.py").read_text(encoding="utf-8")
    self.assertEqual(bridge.count("source_ip = advertised_host(source_ip)"), 2, "audio and camera stream URLs")
    camera = (ROOT / "camera.py").read_text(encoding="utf-8")
    self.assertIn("host = advertised_host(host)", camera, "local camera live endpoint")


if __name__ == "__main__":
  unittest.main()
