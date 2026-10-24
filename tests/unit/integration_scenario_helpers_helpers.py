"""Shared builders and the autouse fixture for the ``test_integration_scenario_helpers_*`` modules."""

from typing import Any

import pytest

from tests.integration import circuit_helpers, dc_topology_helpers, service_inventory_helpers


@pytest.fixture(autouse=True)
def _no_propagation_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the DATA_PROPAGATION_DELAY sleep of the fetchers, which each read it in their own module."""
    for module in (dc_topology_helpers, circuit_helpers, service_inventory_helpers):
        monkeypatch.setattr(module, "DATA_PROPAGATION_DELAY", 0)


def _deployment(name: str, kind: str) -> dict[str, Any]:
    """A raw deployment node with its typename."""
    return {"node": {"__typename": kind, "name": {"value": name}}}
