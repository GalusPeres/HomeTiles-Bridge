"""Pure helpers for HomeTiles device-registry policies."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any


DeviceIdentifier = tuple[str, str]


def is_stale_device_entry(
    device_identifiers: Collection[DeviceIdentifier],
    current_identifier: DeviceIdentifier,
) -> bool:
    """Return whether a registry entry is not the config entry's active device."""
    return current_identifier not in device_identifiers


def find_entry_device(
    device_registry: Any,
    identifier: DeviceIdentifier,
    config_entry_id: str,
) -> Any | None:
    """Return the config entry's registry device for an identifier.

    Home Assistant 2026.8 scopes device identifiers to config entries and
    deprecates the unscoped async_get_device lookup, which stops working in
    2027.8. Older supported versions only provide async_get_device, where
    identifiers are still unique across config entries.
    """
    scoped_lookup = getattr(device_registry, "async_get_device_by_identifier", None)
    if callable(scoped_lookup):
        return scoped_lookup(identifier, config_entry_id)
    return device_registry.async_get_device(identifiers={identifier})
