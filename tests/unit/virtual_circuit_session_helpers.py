"""Shared constants and runners for the ``test_virtual_circuit_session_*`` modules (add_virtual_circuit unit tests)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.logger import GeneratorError
from tests.unit.simulators.circuit_fakes import (
    FakeClient,
    session_generator,
)

HANDOFF = "VC-C001-EQXFR-AWS"
DCI = "VC-DCI-DC10-FR2"
DCI_KEY = f"RoutingPassword:{DCI}-dci-key"
UNTRACKED = {"update_group_context": False}


async def _run(client: FakeClient, payload: dict[str, Any]) -> Any:
    """Run add_virtual_circuit once over the fake client; returns the generator."""
    gen = session_generator(client)
    await gen.generate(payload)
    return gen


async def _assert_refused(payload: dict[str, Any], message: str, client: FakeClient | None = None) -> None:
    """The run fails with ``message`` and has written, allocated and created nothing."""
    client = client or FakeClient()
    with pytest.raises(GeneratorError, match=message):
        await _run(client, payload)
    assert client.writes == []
    assert client.allocations == []
    assert client.created == []


def _peering(client: FakeClient, suffix: str) -> dict[str, Any]:
    """The data the run sent for the peering named <vc>-<suffix>."""
    (data,) = [data for data, _ in client.created_of("ManagedBGPPeering") if data["name"].endswith(f"-{suffix}")]
    return data


def _interface_saves(client: FakeClient) -> list[str]:
    """The labels of the interface saves a run made (the writes whose label starts with if-)."""
    return [label for action, label, _ in client.writes if action == "save" and label.startswith("if-")]
