from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.helpers.rack import RackRolesHelper
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


def _design_for(deployment_type: str) -> PodDesign:
    return PodDesign(
        id="design-1",
        name="test-design",
        rows=2,
        compute_racks_per_row=2,
        network_racks_per_row=0 if deployment_type == "tor" else 1,
        max_tors_per_compute_rack=0 if deployment_type == "middle_rack" else 2,
        max_leafs_per_network_rack=4,
    )


def _build_gen(*, deployment_type: str = "mixed", rack_type: str = "network") -> Any:
    parent = RackParent(
        id="dc-1",
        name="DC1",
        index=1,
        naming_convention="standard",
        amount_of_super_spines=2,
        management_pool=Pool(id="mgmt-pool", name="mgmt"),
        super_spine_template=Template(
            id="tmpl-ss",
            interfaces=[Interface(name="Eth1/1", role="uplink"), Interface(name="Eth1/2", role="uplink")],
        ),
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
        asn_pool=Pool(id="asn-pool", name="asn"),
        design=_design_for(deployment_type),
        spine_template=Template(
            id="tmpl-spine",
            interfaces=[Interface(name="Eth1/10"), Interface(name="Eth1/11")],
        ),
    )
    suite = LocationSuiteModel(index=1)
    leaf_template = Template(id="tmpl-leaf", interfaces=[Interface(name="Eth1/1"), Interface(name="Eth1/2")])
    tor_template = Template(
        id="tmpl-tor",
        interfaces=[Interface(name="Eth1/47", role="uplink"), Interface(name="Eth1/48", role="uplink")],
    )
    bl_template = Template(
        id="tmpl-bl",
        interfaces=[
            Interface(name="Eth1/1", role="uplink"),
            Interface(name="Eth1/2", role="uplink"),
            Interface(name="Eth1/3", role="uplink"),
            Interface(name="Eth1/4", role="uplink"),
        ],
    )
    rack = RackModel(
        id="rack-1",
        name="RACK-1",
        index=2,
        rack_type=rack_type,
        row_index=2,
        parent=suite,
        pod=pod,
        leafs=[DeviceRole(role="leaf", quantity=2, template=leaf_template)],
        tors=[DeviceRole(role="tor", quantity=2, template=tor_template)],
        border_leafs=[DeviceRole(role="border-leaf", quantity=1, template=bl_template)],
    )

    gen = RackGenerator.__new__(RackGenerator)
    gen.data = rack
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []

    # Fields normally prepared by _prepare_generation_context
    gen.fabric_name = "dc1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 1, 1, 2, 2]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._is_ipv6 = False
    gen._spine_device_names = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
    gen._spine_interfaces = ["Eth1/10", "Eth1/11"]
    gen._technical_pool_id = "p2p-pool"
    gen._p2p_prefix_length = 31
    gen._routing_options = {"design": object(), "asn_pool": "asn-pool"}
    gen._created_device_names = set()
    gen._leaf_row_cache = None

    return gen


class TestRackRolesHelper:
    def test_expected_names_deterministic(self) -> None:
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        names_a = helper.expected_names(role="leaf", quantity=2)
        names_b = helper.expected_names(role="leaf", quantity=2)

        assert names_a == names_b
        assert len(names_a) == 2

    def test_build_device_options_loopback_toggle(self) -> None:
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        with_loopback = helper.build_device_options(allocate_loopback=True)
        without_loopback = helper.build_device_options(allocate_loopback=False)

        assert with_loopback["allocate_loopback"] is True
        assert with_loopback["loopback_pool"] == "lo-pool"
        assert with_loopback["loopback_prefix_length"] == 32
        assert without_loopback["allocate_loopback"] is False
        assert "loopback_pool" not in without_loopback

    def test_template_interfaces_filters_by_role(self) -> None:
        template = Template(
            id="tmpl",
            interfaces=[
                Interface(name="Eth1/1", role="uplink"),
                Interface(name="Eth1/2", role="downlink"),
                Interface(name="Eth1/3", role="uplink"),
            ],
        )

        all_ifaces = RackRolesHelper.template_interfaces(template)
        uplinks = RackRolesHelper.template_interfaces(template, role="uplink")

        assert all_ifaces == ["Eth1/1", "Eth1/2", "Eth1/3"]
        assert uplinks == ["Eth1/1", "Eth1/3"]

    def test_border_leafs_per_rack_and_overlay_options(self) -> None:
        gen = _build_gen()
        helper = RackRolesHelper(gen)

        assert helper.border_leafs_per_rack() == 4

        overlay = helper.overlay_only_routing_options()
        assert overlay["skip_underlay"] is True
        assert "skip_underlay" not in gen._routing_options


class TestRackMixinAdditional:
    def test_prepare_generation_context_missing_pools(self) -> None:
        gen = _build_gen()
        gen.data.pod.loopback_pool = None

        ok = gen._prepare_generation_context()

        assert ok is False
        gen.logger.error.assert_called_once()

    def test_prepare_generation_context_success_sets_fields(self) -> None:
        gen = _build_gen()

        ok = gen._prepare_generation_context()

        assert ok is True
        assert gen.deployment_id == "dc-1"
        assert gen.pod_name == "pod-1"
        assert gen.fabric_name == "dc1"
        assert gen._technical_pool_id == "p2p-pool"
        assert gen._p2p_prefix_length == 31
        assert gen._routing_options["asn_pool"] == "asn-pool"

    def test_derive_super_spine_info_success(self) -> None:
        gen = _build_gen()
        gen.fabric_name = "dc1"

        device_names, interface_names = gen._derive_super_spine_info()

        assert len(device_names) == 2
        assert interface_names == ["Eth1/1", "Eth1/2"]

    def test_derive_super_spine_info_missing_template_raises(self) -> None:
        gen = _build_gen()
        gen.data.pod.parent.super_spine_template = None

        with pytest.raises(RuntimeError, match="Cannot derive super-spine info"):
            gen._derive_super_spine_info()
