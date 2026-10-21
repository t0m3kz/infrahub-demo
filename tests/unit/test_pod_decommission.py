"""decommission_pod (generators/decommission/pod.py) beyond tracking ownership.

- a border-spine pod's border-spines are deployed to the DC: they are picked out of
  the DC's border-spines by name and decommissioned with the pod;
- the far end of every removed fabric cable (a super-spine, another pod's spine, a
  border-leaf) is freed and its P2P address deleted, an endpoint's is not;
- the pod's pools and their slices of the DC pools are deleted once nothing is left
  in a slice, kept (warning) otherwise; a re-run without pools does nothing.

The tracking-ownership side (every write untracked) is in test_generator_tracking_ownership.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.decommission.pod import PodDecommissionGenerator
from generators.pod_config import spine_slot_device_names
from generators.protocols import (
    CoreIPAddressPool,
    CoreIPPrefixPool,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    IpamIPAddress,
    IpamPrefix,
)

_DC = {"name": "DC7", "index": 7, "naming_convention": "hierarchical"}


class _Batch:
    """InfrahubBatch stand-in that yields the queued nodes."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []

    def add(self, *, task: Any, node: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.nodes.append(node)

    async def execute(self):  # noqa: ANN201
        for node in self.nodes:
            yield node, None


def _node(node_id: str, *, addresses: int = 0, children: int = 0) -> MagicMock:
    node = MagicMock()
    node.id = node_id
    node.name.value = node_id
    node.prefix.value = node_id
    node.ip_addresses.peers = [MagicMock() for _ in range(addresses)]
    node.children.peers = [MagicMock() for _ in range(children)]
    node.save = AsyncMock()
    node.delete = AsyncMock()
    return node


def _interface(
    interface_id: str, *, ip_id: str, prefix_id: str, far_end: tuple[str, str, str] | None = None
) -> dict[str, Any]:
    """A cabled physical interface; far_end is (interface id, its ip id, its device role)."""
    endpoints = []
    if far_end is not None:
        far_id, far_ip, far_role = far_end
        endpoints.append(
            {
                "node": {
                    "id": far_id,
                    "ip_address": {"node": {"id": far_ip, "ip_prefix": {"node": {"id": prefix_id}}}},
                    "device": {"node": {"role": {"value": far_role}}},
                }
            }
        )
    return {
        "__typename": "DcimPhysicalInterface",
        "id": interface_id,
        "description": {"value": "x"},
        "status": {"value": "active"},
        "ip_address": {"node": {"id": ip_id, "ip_prefix": {"node": {"id": prefix_id, "prefix": {"value": "p"}}}}},
        "cable": {"node": {"id": f"cable-{interface_id}", "endpoints": {"edges": endpoints}}},
    }


def _device(device_id: str, name: str, role: str, interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "node": {
            "id": device_id,
            "name": {"value": name},
            "role": {"value": role},
            "status": {"value": "active"},
            "primary_address": {"node": None},
            "interfaces": {"edges": [{"node": interface} for interface in interfaces]},
        }
    }


def _pool(pool_id: str, slice_id: str) -> dict[str, Any]:
    return {"node": {"id": pool_id, "resources": {"edges": [{"node": {"id": slice_id}}]}}}


def _payload(
    *,
    spine_role: str = "spine",
    devices: list[dict[str, Any]] | None = None,
    dc_border_spines: list[dict[str, Any]] | None = None,
    pools: bool = True,
) -> dict[str, Any]:
    return {
        "TopologyPod": {
            "edges": [
                {
                    "node": {
                        "id": "pod-2",
                        "name": {"value": "DC7-7-POD-2"},
                        "index": {"value": 2},
                        "loopback_pool": _pool("lo-pool", "lo-slice") if pools else {"node": None},
                        "prefix_pool": _pool("p2p-pool", "p2p-slice") if pools else {"node": None},
                        "fabric_templates": {
                            "edges": [{"node": {"role": {"value": spine_role}, "quantity": {"value": 2}}}]
                        },
                        "parent": {
                            "node": {
                                "name": {"value": _DC["name"]},
                                "index": {"value": _DC["index"]},
                                "naming_convention": {"value": _DC["naming_convention"]},
                                "border_spines": {"edges": dc_border_spines or []},
                            }
                        },
                        "devices": {"edges": devices or []},
                    }
                }
            ]
        }
    }


def _gen(*, slices: dict[str, MagicMock] | None = None) -> Any:
    gen: Any = PodDecommissionGenerator.__new__(PodDecommissionGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.create_batch = AsyncMock(side_effect=lambda: _Batch())
    gen.client.delete = AsyncMock()
    gen.filter_calls = []
    known_slices: dict[str, MagicMock] = slices or {}

    async def _filters(*, kind: Any, ids: list[str], **kwargs: Any) -> list[Any]:
        gen.filter_calls.append((kind, ids, kwargs))
        if kind is IpamPrefix and "include" in kwargs:
            return [known_slices[node_id] for node_id in ids if node_id in known_slices]
        return [_node(node_id) for node_id in ids]

    gen.client.filters = AsyncMock(side_effect=_filters)
    return gen


def _ids(gen: Any, kind: Any) -> list[str]:
    return [
        node_id for call_kind, ids, kwargs in gen.filter_calls if call_kind is kind and not kwargs for node_id in ids
    ]


class TestBorderSpines:
    @pytest.mark.asyncio
    async def test_border_spines_deployed_to_the_dc_are_decommissioned_by_name(self) -> None:
        own = spine_slot_device_names(dc=_DC, fabric_name="dc7", pod_index=2, role="border-spine", quantity=2)
        other = spine_slot_device_names(dc=_DC, fabric_name="dc7", pod_index=1, role="border-spine", quantity=2)
        dc_border_spines = [
            _device(f"id-{name}", name, "border-spine", [_interface(f"if-{name}", ip_id=f"ip-{name}", prefix_id="px")])
            for name in own + other
        ]
        gen = _gen()

        await gen.generate(_payload(spine_role="border-spine", dc_border_spines=dc_border_spines, pools=False))

        assert _ids(gen, DcimPhysicalDevice) == sorted(f"id-{name}" for name in own)
        assert _ids(gen, DcimPhysicalInterface) == sorted(f"if-{name}" for name in own)

    @pytest.mark.asyncio
    async def test_a_plain_spine_pod_takes_no_border_spine_of_the_dc(self) -> None:
        name = spine_slot_device_names(dc=_DC, fabric_name="dc7", pod_index=2, role="border-spine", quantity=1)[0]
        dc_border_spines = [_device("id-bs", name, "border-spine", [])]
        gen = _gen()

        await gen.generate(_payload(dc_border_spines=dc_border_spines, pools=False))

        assert _ids(gen, DcimPhysicalDevice) == []


class TestUnusedPorts:
    @pytest.mark.asyncio
    async def test_a_port_without_address_or_cable_is_freed_without_error(self) -> None:
        """The query returns ``{"node": null}`` for an unused port's address and cable (and a
        device without a primary address); reading those as dicts crashed every real run."""
        unused = {
            "__typename": "DcimPhysicalInterface",
            "id": "if-free",
            "description": {"value": ""},
            "status": {"value": "active"},
            "ip_address": {"node": None},
            "cable": {"node": None},
        }
        gen = _gen()

        await gen.generate(_payload(devices=[_device("spine-1", "sp1", "spine", [unused])], pools=False))

        assert _ids(gen, DcimPhysicalInterface) == ["if-free"]
        assert _ids(gen, IpamIPAddress) == []
        gen.logger.error.assert_not_called()


class TestFarEnds:
    @pytest.mark.asyncio
    async def test_fabric_far_ends_are_freed_and_endpoints_left_alone(self) -> None:
        """A mesh link to another pod's spine, a border-leaf uplink and a super-spine port lose
        their address with the cable; a server NIC cabled to a leaf keeps its own."""
        spine = _device(
            "spine-1",
            "sp1",
            "spine",
            [
                _interface("if-1", ip_id="ip-1", prefix_id="px-1", far_end=("sib-if", "sib-ip", "spine")),
                _interface("if-2", ip_id="ip-2", prefix_id="px-2", far_end=("bl-if", "bl-ip", "border-leaf")),
                _interface("if-3", ip_id="ip-3", prefix_id="px-3", far_end=("ss-if", "ss-ip", "super-spine")),
            ],
        )
        leaf = _device(
            "leaf-1",
            "lf1",
            "leaf",
            [_interface("if-4", ip_id="ip-4", prefix_id="px-4", far_end=("srv-if", "srv-ip", "endpoint"))],
        )
        gen = _gen()

        await gen.generate(_payload(devices=[spine, leaf], pools=False))

        assert set(_ids(gen, DcimPhysicalInterface)) == {"if-1", "if-2", "if-3", "if-4", "sib-if", "bl-if", "ss-if"}
        addresses = set(_ids(gen, IpamIPAddress))
        assert {"sib-ip", "bl-ip", "ss-ip"} <= addresses
        assert "srv-ip" not in addresses


class TestPoolRelease:
    @pytest.mark.asyncio
    async def test_empty_slices_are_released_with_their_pools(self) -> None:
        """Pool first, then its slice (freed in the DC pool); each by id."""
        slices = {"lo-slice": _node("lo-slice"), "p2p-slice": _node("p2p-slice")}
        gen = _gen(slices=slices)

        await gen.generate(_payload())

        include_calls = [(ids, kwargs) for kind, ids, kwargs in gen.filter_calls if kind is IpamPrefix and kwargs]
        assert include_calls == [
            (["lo-slice"], {"include": ["ip_addresses", "children"]}),
            (["p2p-slice"], {"include": ["ip_addresses", "children"]}),
        ]
        assert [call.kwargs for call in gen.client.delete.await_args_list] == [
            {"kind": CoreIPAddressPool, "id": "lo-pool"},
            {"kind": CoreIPPrefixPool, "id": "p2p-pool"},
        ]
        slices["lo-slice"].delete.assert_awaited_once()
        slices["p2p-slice"].delete.assert_awaited_once()
        gen.logger.warning.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("addresses", "children"), [(1, 0), (0, 1)])
    async def test_a_slice_with_something_left_in_it_is_kept(self, addresses: int, children: int) -> None:
        """Another device's address or a prefix this run did not remove still sits in the
        technical slice: releasing it would let the DC pool hand the range to another pod."""
        slices = {
            "lo-slice": _node("lo-slice"),
            "p2p-slice": _node("p2p-slice", addresses=addresses, children=children),
        }
        gen = _gen(slices=slices)

        await gen.generate(_payload())

        assert [call.kwargs["id"] for call in gen.client.delete.await_args_list] == ["lo-pool"]
        slices["p2p-slice"].delete.assert_not_awaited()
        gen.logger.warning.assert_called_once()
        assert "keeping prefix_pool p2p-pool" in gen.logger.warning.call_args.args[0]

    @pytest.mark.asyncio
    async def test_a_rerun_without_pools_deletes_nothing(self) -> None:
        gen = _gen()

        await gen.generate(_payload(pools=False))

        gen.client.delete.assert_not_awaited()
        assert not [call for call in gen.filter_calls if call[0] is IpamPrefix and call[2]]
