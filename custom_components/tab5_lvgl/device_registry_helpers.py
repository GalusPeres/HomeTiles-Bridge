"""Pure helpers for HomeTiles device-registry policies."""

from __future__ import annotations

from collections.abc import Collection


DeviceIdentifier = tuple[str, str]


def is_stale_device_entry(
    device_identifiers: Collection[DeviceIdentifier],
    current_identifier: DeviceIdentifier,
) -> bool:
    """Return whether a registry entry is not the config entry's active device."""
    return current_identifier not in device_identifiers
