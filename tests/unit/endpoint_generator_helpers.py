"""Shared builders of the EndpointConnectivityGenerator tests (test_endpoint_generator*.py).

The rack and endpoint rows, the generator factory and the mocked endpoint devices.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.endpoint import EndpointConnectivityGenerator
from tests.unit.simulators.fabric_designs import pod_design


def _rack(*, rack_id: str = "rack-1", row_index: int = 1, rack_type: str = "compute") -> dict:
    return {
        "id": rack_id,
        "name": "RACK-1",
        "index": 1,
        "row_index": row_index,
        "rack_type": rack_type,
        "pod": {
            "id": "pod-1",
            "name": "POD1",
            "index": 1,
            "design": pod_design("S_MIXED"),
            "parent": {"id": "dc-1", "name": "DC1"},
        },
    }


_UNSET = object()


def _endpoint_data(*, endpoint_id: str = "ep-1", name: str = "server-1", rack: Any = _UNSET) -> dict:
    return {
        "DcimDevice": [
            {
                "id": endpoint_id,
                "name": name,
                "role": "endpoint",
                "rack": _rack() if rack is _UNSET else rack,
                "interfaces": [],
            }
        ]
    }


def _make_generator() -> Any:
    gen = EndpointConnectivityGenerator.__new__(EndpointConnectivityGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.planned_connections = set()
    gen._free_interfaces = []
    gen._already_connected = False
    gen._existing_switch_names = set()

    gen.client.get = AsyncMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.acquire_resource_lock = AsyncMock(return_value="lock-1")
    gen.release_resource_lock = AsyncMock()
    gen._process_endpoint_connections = AsyncMock()
    gen._process_lag_endpoint_connections = AsyncMock()
    # Covered on its own in TestFanOutComponentSegments; mocked here so the
    # generate() tests' filters side_effect lists stay the cabling queries only.
    gen._fan_out_component_segments = AsyncMock()
    gen.run_generator = AsyncMock()
    gen.create_cabling = AsyncMock(return_value=[("server-port", "switch-port")])
    return gen


def _iface(
    name: str,
    *,
    device: str,
    cabled: bool = False,
    interface_type: str | None = None,
) -> MagicMock:
    """A DcimPhysicalInterface as client.filters returns it with include=["device", "interface_type", "cable"]."""
    intf = MagicMock()
    intf.name.value = name
    intf.interface_type.value = interface_type
    intf.device.peer.name.value = device
    if cabled:
        intf.cable = MagicMock()
        intf.cable.id = "cable-1"
    else:
        intf.cable = None
    intf.lag = None
    return intf


def _endpoint_device() -> MagicMock:
    endpoint_device = MagicMock()
    endpoint_device.deployment = MagicMock(id="pod-1")
    endpoint_device.save = AsyncMock()
    return endpoint_device


def _bond(name: str, members: list[MagicMock]) -> MagicMock:
    """A server DcimLAGInterface whose member_interfaces peers point at `members` by id."""
    bond = MagicMock()
    bond.name = MagicMock(value=name)
    bond.member_interfaces = MagicMock()
    bond.member_interfaces.peers = [MagicMock(id=m.id) for m in members]
    return bond


def _nic(name: str) -> MagicMock:
    nic = _iface(name, device="server-1")
    nic.id = f"id-{name}"
    return nic
