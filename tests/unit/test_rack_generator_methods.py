from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.models import (
    DeviceRole,
    Interface,
    LocationSuiteModel,
    PodDesign,
    Pool,
    RackModel,
    RackParent,
    RackPod,
    Template,
)
from generators.topology.rack import RackGenerator


def _build_gen() -> Any:
    parent = RackParent(
        id="dc-1",
        name="DC1",
        index=1,
        naming_convention="standard",
        amount_of_super_spines=2,
        management_pool=Pool(id="mgmt-pool", name="mgmt"),
    )
    design = PodDesign(
        id="design-1",
        name="design",
        rows=2,
        compute_racks_per_row=2,
        network_racks_per_row=1,
        max_tors_per_compute_rack=2,
        max_leafs_per_network_rack=4,
    )
    pod = RackPod(
        id="pod-1",
        name="pod-1",
        index=1,
        parent=parent,
        amount_of_spines=2,
        leaf_interface_sorting_method="top_down",
        spine_interface_sorting_method="bottom_up",
        loopback_pool=Pool(id="lo-pool", name="lo"),
        prefix_pool=Pool(id="p2p-pool", name="p2p"),
        design=design,
        spine_template=Template(id="tmpl-spine", interfaces=[Interface(name="Eth1/10"), Interface(name="Eth1/11")]),
    )
    suite = LocationSuiteModel(index=1)
    leaf_t = Template(id="tmpl-leaf", interfaces=[Interface(name="Eth1/1"), Interface(name="Eth1/2")])
    tor_t = Template(id="tmpl-tor", interfaces=[Interface(name="Eth1/47", role="uplink")])

    rack = RackModel(
        id="rack-1",
        name="RACK-1",
        index=2,
        rack_type="network",
        row_index=1,
        parent=suite,
        pod=pod,
        leafs=[DeviceRole(role="leaf", quantity=2, template=leaf_t)],
        tors=[DeviceRole(role="tor", quantity=1, template=tor_t)],
        l2_leafs=[],
        access_leafs=[],
        border_leafs=[],
    )

    gen = RackGenerator.__new__(RackGenerator)
    gen.data = rack
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []

    gen.fabric_name = "dc1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 1, 1, 1, 2]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._is_ipv6 = False
    gen._spine_device_names = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
    gen._spine_interfaces = ["Eth1/10", "Eth1/11"]
    gen._technical_pool_id = "p2p-pool"
    gen._p2p_prefix_length = 31
    gen._routing_options = {"design": object()}
    gen._created_device_names = set()
    gen._leaf_row_cache = None

    return gen


class TestRackGeneratorMethods:
    def test_has_role_template_helpers(self) -> None:
        gen = _build_gen()
        assert gen._has_role_templates(None) is False
        assert gen._has_role_templates([]) is False
        assert gen._has_role_templates(gen.data.leafs) is True
        assert gen._has_tor_like_templates() is True
        assert gen._has_any_switch_templates() is True

    @pytest.mark.asyncio
    async def test_fetch_rack_devices_with_interfaces_from_context(self) -> None:
        gen = _build_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={
                "LocationRack": {
                    "edges": [
                        {
                            "node": {
                                "devices": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "dev-1",
                                                "name": {"value": "leaf-01"},
                                                "role": {"value": "leaf"},
                                                "interfaces": {
                                                    "edges": [
                                                        {"node": {"name": {"value": "Eth1/1"}}},
                                                        {"node": {"name": {"value": "Eth1/2"}}},
                                                    ]
                                                },
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            }
        )

        rows = await gen.fetch_rack_devices_with_interfaces(role_filter="leaf")

        assert len(rows) == 1
        assert rows[0]["device_name"] == "leaf-01"
        assert rows[0]["interfaces"] == ["Eth1/1", "Eth1/2"]

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_success(self) -> None:
        gen = _build_gen()
        rack_obj = MagicMock(id="rack-a")
        leaf_dev = MagicMock()
        leaf_dev.name = MagicMock(value="leaf-01")
        leaf_if = MagicMock()
        leaf_if.name = MagicMock(value="Eth1/1")

        gen.client.filters = AsyncMock(side_effect=[[rack_obj], [leaf_dev], [leaf_if]])

        devices, interfaces = await gen._get_leaf_devices_in_row("pod-1", 1)

        assert devices == ["leaf-01"]
        assert interfaces == ["Eth1/1"]

    @pytest.mark.asyncio
    async def test_get_leaf_devices_in_row_no_racks_raises(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[])

        with pytest.raises(RuntimeError, match="no racks"):
            await gen._get_leaf_devices_in_row("pod-1", 1)

    @pytest.mark.asyncio
    async def test_resolve_local_leaf_target_with_created_leafs(self) -> None:
        gen = _build_gen()
        iface = MagicMock()
        iface.name = MagicMock(value="Eth1/1")
        gen.client.filters = AsyncMock(return_value=[iface])

        target = await gen._resolve_local_leaf_cabling_target(
            created_leaf_devices=["leaf-01"],
            leaf_row_cache=None,
            devices_per_rack=2,
            role_label="l2-leaf",
        )

        assert target is not None
        devs, ifaces, offset, strategy = target
        assert devs == ["leaf-01"]
        assert ifaces == ["Eth1/1"]
        assert offset == 0
        assert strategy == "intra_rack_middle"

    @pytest.mark.asyncio
    async def test_cable_and_route_with_and_without_design(self) -> None:
        gen = _build_gen()
        gen.create_cabling = AsyncMock(return_value=[("a", "b")])
        gen.create_routing = AsyncMock()

        await gen._cable_and_route(
            bottom_devices=["leaf-01"],
            bottom_interfaces=["Eth1/1"],
            top_devices=["spine-01"],
            top_interfaces=["Eth1/10"],
            strategy="rack",
            offset=0,
            bottom_role="leaf",
            top_role="spine",
        )
        gen.create_routing.assert_awaited_once()

        gen._routing_options = {}
        gen.create_routing = AsyncMock()
        await gen._cable_and_route(
            bottom_devices=["leaf-01"],
            bottom_interfaces=["Eth1/1"],
            top_devices=["spine-01"],
            top_interfaces=["Eth1/10"],
            strategy="rack",
            offset=0,
            bottom_role="leaf",
            top_role="spine",
        )
        gen.create_routing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_leafs_success_and_duplicate_skip(self) -> None:
        gen = _build_gen()
        gen.create_devices = AsyncMock(return_value=["leaf-a", "leaf-b"])
        gen._cable_and_route = AsyncMock()
        gen.calculate_cabling_offsets = MagicMock(return_value=3)

        created: list[str] = []
        ok = await gen._generate_leafs(created)

        assert ok is True
        assert created == ["leaf-a", "leaf-b"]
        gen._cable_and_route.assert_awaited_once()

        # second pass should skip as duplicate
        ok = await gen._generate_leafs(created)
        assert ok is True

    @pytest.mark.asyncio
    async def test_generate_tors_no_spine_devices_fails(self) -> None:
        gen = _build_gen()
        gen.create_devices = AsyncMock(return_value=["tor-a"])
        gen.calculate_cabling_offsets = MagicMock(return_value=1)
        gen.client.filters = AsyncMock(return_value=[])
        gen._spine_device_names = []

        ok = await gen._generate_tors()

        assert ok is False
        gen.logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_generate_border_leafs_no_uplink_skips(self) -> None:
        gen = _build_gen()
        bl_template = Template(id="tmpl-bl-empty", interfaces=[])
        gen.data.border_leafs = [DeviceRole(role="border-leaf", quantity=1, template=bl_template)]
        gen.create_devices = AsyncMock(return_value=["bl-1"])
        gen._cable_and_route = AsyncMock()

        await gen._generate_border_leafs("mixed")

        gen.logger.warning.assert_called()
        gen._cable_and_route.assert_not_awaited()
