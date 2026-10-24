"""The address the Bridge hands a panel (link, camera and audio stream URLs).

The Bridge sends the address Home Assistant uses towards the panel. Behind NAT,
such as the Docker test instance (docker/test-ha), that is an address the panel
cannot reach; HOMETILES_ADVERTISE_HOST then names the one it can. Unset (the
default) nothing changes.
"""

from __future__ import annotations

import os

ENV_ADVERTISE_HOST = "HOMETILES_ADVERTISE_HOST"


def advertised_host(detected: str | None) -> str:
  """HOMETILES_ADVERTISE_HOST when set, else the detected address."""
  override = os.environ.get(ENV_ADVERTISE_HOST, "").strip()
  return override or (detected or "")
