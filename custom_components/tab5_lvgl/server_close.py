"""Close the Bridge's TCP servers without holding up Home Assistant's stop."""

from __future__ import annotations

import asyncio
import logging
from typing import Final

_LOGGER = logging.getLogger(__name__)

# Longest wait for a server's connections to end while Home Assistant stops.
SERVER_CLOSE_TIMEOUT_S: Final = 3.0


async def async_close_server(server: asyncio.AbstractServer, name: str) -> None:
  """Stop listening, drop the connections still open and wait for them, bounded.

  Since Python 3.12.1 Server.wait_closed() waits until every accepted
  connection is closed, so a panel that stayed connected held up Home
  Assistant's restart (issue #15). The caller closes the connections it knows
  first; close_clients() (Python 3.13) drops the rest, and the wait ends after
  SERVER_CLOSE_TIMEOUT_S either way.
  """
  server.close()
  close_clients = getattr(server, "close_clients", None)
  if close_clients is not None:
    close_clients()
  try:
    await asyncio.wait_for(server.wait_closed(), SERVER_CLOSE_TIMEOUT_S)
  except asyncio.TimeoutError:
    _LOGGER.warning(
      "HomeTiles %s server: connections still open after %.0f s, stopping anyway",
      name,
      SERVER_CLOSE_TIMEOUT_S,
    )
