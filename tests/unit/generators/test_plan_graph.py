"""The write order derived from what the specs reference (generators/plan_graph.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.plan import (
    AddressAllocationSpec,
    Hfid,
    LinkPrefixSpec,
    NodeRef,
    NodeSpec,
    RoutingTagSpec,
    TopologyPlan,
)
from generators.plan_graph import external_hfids, lookup_hfids, render_hfids, schedule
from tests.unit.plan_graph_helpers import HfidClient

DEVICE = Hfid("DcimPhysicalDevice", ("leaf-1",))
PORT = Hfid("DcimPhysicalInterface", ("leaf-1", "Eth1"))
SPINE_PORT = Hfid("DcimPhysicalInterface", ("spine-1", "Eth9"))


def _keys(steps: list[Any]) -> list[list[str]]:
    return [[spec.key for spec in step] for step in steps]


def _device() -> NodeSpec:
    return NodeSpec("device", "DcimPhysicalDevice", {"name": "leaf-1"}, hfid=DEVICE, provides=(PORT,))


def _port() -> NodeSpec:
    return NodeSpec("port", "DcimPhysicalInterface", {"device": DEVICE, "name": "Eth1"}, hfid=PORT, existing=True)


class TestSchedule:
    def test_a_reference_by_hfid_waits_for_the_spec_that_names_it_even_when_listed_first(self) -> None:
        port = NodeSpec("port", "DcimPhysicalInterface", {"device": DEVICE}, hfid=PORT)

        assert _keys(schedule([port, _device()])) == [["device"], ["port"]]

    def test_a_child_a_device_provides_waits_for_the_device_without_naming_it(self) -> None:
        port = NodeSpec("port", "DcimPhysicalInterface", {"name": "Eth1"}, hfid=PORT, existing=True)

        assert _keys(schedule([port, _device()])) == [["device"], ["port"]]

    def test_a_cable_waits_for_both_ports_and_a_port_for_its_address_allocation(self) -> None:
        other = Hfid("DcimPhysicalInterface", ("leaf-1", "Eth2"))
        specs = [
            NodeSpec("cable", "DcimCable", {"endpoints": [PORT, other]}),
            NodeSpec("p1", "DcimPhysicalInterface", {"ip_address": NodeRef("link#0")}, hfid=PORT, existing=True),
            NodeSpec("p2", "DcimPhysicalInterface", {"ip_address": NodeRef("link#1")}, hfid=other, existing=True),
            LinkPrefixSpec("link", (PORT, other), "pool", 31, ownership="owned"),
            NodeSpec("device", "DcimPhysicalDevice", {}, hfid=DEVICE, provides=(PORT, other)),
        ]

        assert _keys(schedule(specs)) == [["device"], ["link"], ["p1", "p2"], ["cable"]]

    def test_a_link_prefix_needs_its_ports_to_exist_not_to_be_updated(self) -> None:
        """The ports take the link's addresses: a prefix that waited for their update would wait for itself."""
        port = NodeSpec("p", "DcimPhysicalInterface", {"ip_address": NodeRef("link#0")}, hfid=PORT)
        created = [port, LinkPrefixSpec("link", (PORT, SPINE_PORT), "pool", 31, ownership="owned")]

        with pytest.raises(ValueError, match="cycle"):
            schedule(created)

    def test_independent_specs_share_a_level_in_plan_order(self) -> None:
        specs = [
            NodeSpec("b", "K", {}),
            NodeSpec("a", "K", {}),
            AddressAllocationSpec("c", "id", 32, "d", ownership="shared", pool_id="p"),
        ]

        assert _keys(schedule(specs)) == [["b", "a", "c"]]

    def test_a_cycle_is_rejected_naming_its_specs(self) -> None:
        specs = [NodeSpec("a", "K", {"x": NodeRef("b")}), NodeSpec("b", "K", {"y": NodeRef("a")})]

        with pytest.raises(ValueError, match="cycle.*NodeSpec 'a'.*NodeSpec 'b'"):
            schedule(specs)

    def test_a_reference_by_hfid_can_close_a_cycle_too(self) -> None:
        a, b = Hfid("K", ("a",)), Hfid("K", ("b",))
        specs = [NodeSpec("a", "K", {"x": b}, hfid=a), NodeSpec("b", "K", {"x": a}, hfid=b)]

        with pytest.raises(ValueError, match="cycle"):
            TopologyPlan(root_id=None, specs=list(specs)).validate()

    def test_a_node_ref_nothing_saves_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="which no spec of the plan saves"):
            schedule([NodeSpec("a", "K", {"x": NodeRef("nowhere")})])

    def test_an_hfid_named_twice_is_rejected(self) -> None:
        specs = [NodeSpec("a", "K", {}, hfid=DEVICE), NodeSpec("b", "K", {}, hfid=DEVICE)]

        with pytest.raises(ValueError, match="twice"):
            schedule(specs)

    def test_a_routing_tag_ends_the_specs_before_it_and_nothing_is_ordered_across_it(self) -> None:
        tag = RoutingTagSpec("K", "node-1", "pool")
        specs = [NodeSpec("a", "K", {}), tag, NodeSpec("b", "K", {"x": NodeRef("a")})]

        assert _keys([step for step in schedule(specs) if isinstance(step, list)]) == [["a"], ["b"]]
        assert schedule(specs)[1] is tag

    def test_a_reference_to_a_spec_after_a_routing_tag_cannot_be_ordered(self) -> None:
        specs = [NodeSpec("a", "K", {"x": NodeRef("b")}), RoutingTagSpec("K", "n", "pool"), NodeSpec("b", "K", {})]

        with pytest.raises(ValueError, match="after a routing tag"):
            schedule(specs)

    def test_an_address_drawn_by_pool_name_waits_for_the_plan_s_own_pool(self) -> None:
        """A management address names its pool only by name (add_dc's service devices): when the plan saves that pool,
        the allocation comes after it, else the reconciler looks the pool up before it exists (NodeNotFoundError).
        """
        pool = NodeSpec(
            "pool:dc6-management-pool",
            "CoreIPAddressPool",
            {"name": "dc6-management-pool"},
            hfid=Hfid("CoreIPAddressPool", ("dc6-management-pool",)),
        )
        address = AddressAllocationSpec(
            "mgmt:fw-1", "fw-1", 24, "fw-1 management", ownership="shared", pool_name="dc6-management-pool"
        )

        assert _keys(schedule([address, pool])) == [["pool:dc6-management-pool"], ["mgmt:fw-1"]]

    def test_an_address_drawn_by_the_name_of_a_pool_the_plan_does_not_save_is_not_ordered(self) -> None:
        """A bootstrap pool, or one another generator creates, is resolved by name when the address is written."""
        address = AddressAllocationSpec(
            "mgmt:fw-1", "fw-1", 24, "fw-1 management", ownership="shared", pool_name="bootstrap-pool"
        )

        assert _keys(schedule([address, _device()])) == [["mgmt:fw-1", "device"]]


class TestExternalReferences:
    def test_what_no_spec_names_or_provides_is_external_once_in_plan_order(self) -> None:
        specs = [
            _device(),
            NodeSpec(
                "p", "DcimPhysicalInterface", {"device": Hfid("DcimPhysicalDevice", ("spine-1",))}, hfid=SPINE_PORT
            ),
            NodeSpec("c", "DcimCable", {"endpoints": [PORT, SPINE_PORT]}),
        ]

        assert external_hfids(specs) == [Hfid("DcimPhysicalDevice", ("spine-1",))]

    def test_an_existing_node_nothing_provides_is_external_itself(self) -> None:
        spine_port = NodeSpec("p", "DcimPhysicalInterface", {"name": "Eth9"}, hfid=SPINE_PORT, existing=True)

        assert external_hfids([spine_port]) == [SPINE_PORT]
        assert external_hfids([_device(), _port()]) == []

    def test_the_ends_of_a_link_prefix_are_references(self) -> None:
        assert external_hfids([LinkPrefixSpec("l", (PORT, SPINE_PORT), "pool", 31, ownership="owned")]) == [
            PORT,
            SPINE_PORT,
        ]


def test_an_hfid_renders_as_the_reference_the_server_resolves() -> None:
    data = {"device": DEVICE, "endpoints": [PORT, SPINE_PORT], "name": "x"}

    assert render_hfids(data) == {
        "device": {"hfid": ["leaf-1"], "kind": "DcimPhysicalDevice"},
        "endpoints": [
            {"hfid": ["leaf-1", "Eth1"], "kind": "DcimPhysicalInterface"},
            {"hfid": ["spine-1", "Eth9"], "kind": "DcimPhysicalInterface"},
        ],
        "name": "x",
    }


@pytest.mark.asyncio
async def test_lookup_reads_one_kind_at_a_time_and_returns_the_ids_that_exist() -> None:
    client = HfidClient()
    device = client.seed("DcimPhysicalDevice", name="leaf-1")
    port = client.seed("DcimPhysicalInterface", name="Eth1", device={"id": device.id})

    found = await lookup_hfids(client, [DEVICE, PORT, SPINE_PORT])

    assert found == {DEVICE: device.id, PORT: port.id}
