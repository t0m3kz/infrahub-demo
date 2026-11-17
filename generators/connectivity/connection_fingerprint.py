"""The identity of a server-to-switch connection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionFingerprint:
    """Unique identifier for a server-to-switch connection, so cabling is idempotent across runs."""

    server_name: str
    server_interface: str
    switch_name: str
    switch_interface: str

    def __repr__(self) -> str:
        return f"{self.server_name}:{self.server_interface} → {self.switch_name}:{self.switch_interface}"
