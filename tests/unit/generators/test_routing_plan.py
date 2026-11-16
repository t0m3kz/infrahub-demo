"""The routing of a layer pair as plan specs (generators/helpers/routing_plan.py): a pure function of the devices, their
links and what exists before the run. Built as data; written through the reconciler in the last test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.bgp import (
    RoutingDevice,
    RoutingPlanError,
    RoutingReads,
    RoutingShared,
    bgp_processes_ready,
    mlag_pairs_of,
    plan_routing,
    read_routing_reads,
)
from generators.dcim.cabling_plan import Link, PortFacts
from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan, TrackSpec

_LOOPBACK = "DcimVirtualInterface"


def _device(name: str, role: str, *, router_id: Any = "unset") -> RoutingDevice:
    return RoutingDevice(
        name, role, Hfid(_LOOPBACK, (name, "Loopback0")), f"ip-{name}" if router_id == "unset" else router_id
    )


def _link(bottom: str, top: str, bottom_port: str = "Eth1/1", top_port: str = "Eth1/49") -> Link:
    cable = f"{bottom}-{bottom_port}__{top}-{top_port}"
    return Link(PortFacts(bottom, bottom_port, None), PortFacts(top, top_port, None), cable, f"cable:{cable}")


def _devices(*pairs: tuple[str, str]) -> dict[str, RoutingDevice]:
    return {name: _device(name, role) for name, role in pairs}


_LEAFS = _devices(("leaf-a", "leaf"), ("leaf-b", "leaf"), ("spine-1", "spine"))


def _route(
    strategy: str = "ebgp-ebgp",
    *,
    devices: dict[str, RoutingDevice] | None = None,
    bottom: tuple[str, ...] = ("leaf-a", "leaf-b"),
    top: tuple[str, ...] = ("spine-1",),
    links: tuple[Link, ...] | None = None,
    reads: RoutingReads | None = None,
    mlag_pairs: dict[str, str] | None = None,
    plan: TopologyPlan | None = None,
    **shared: Any,
) -> TopologyPlan:
    plan = plan or TopologyPlan(root_id="rack-1")
    cabled = tuple(_link(name, top[0]) for name in bottom) if links is None else links
    options: dict[str, Any] = {
        "asn_pool": "asn-pool",
        "overlay_as": "overlay-as",
        "evpn_address_family": "af-evpn",
        **shared,
    }
    plan_routing(
        plan,
        shared=RoutingShared(strategy=strategy, **options),
        devices=devices or _LEAFS,
        bottom_devices=bottom,
        top_devices=top,
        links=cabled,
        reads=reads or RoutingReads(),
        mlag_pairs=mlag_pairs or {},
        logger=MagicMock(),
    )
    return plan


def _nodes(plan: TopologyPlan, kind: str) -> dict[str, NodeSpec]:
    return {spec.data["name"]: spec for spec in plan.of(NodeSpec) if spec.kind == kind}


def _autonomous_systems(plan: TopologyPlan) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if spec.kind == "RoutingAutonomousSystem"]


class TestEbgpUnderlay:
    def test_every_bottom_device_gets_a_process_on_its_own_as_drawn_from_the_pool(self) -> None:
        plan = _route()

        systems = _autonomous_systems(plan)
        assert [spec.key for spec in systems] == ["as:leaf-a", "as:leaf-b"]
        assert all(spec.sequential and spec.ownership == "owned" for spec in systems)
        assert systems[0].data["asn"] == {"from_pool": {"id": "asn-pool"}}
        processes = _nodes(plan, "ManagedBGP")
        underlay = processes["leaf-a-bgp-underlay"]
        assert underlay.data["local_as"] == {"id": NodeRef("as:leaf-a")}
        assert underlay.data["router_id"] == {"id": "ip-leaf-a"}
        assert underlay.data["capabilities"] == [Hfid("DcimPhysicalDevice", ("leaf-a",))]
        assert underlay.data["process_role"] == "underlay" and underlay.data["multipath"] is True
        assert underlay.ownership == "owned" and underlay.hfid == Hfid("ManagedBGP", ("leaf-a-bgp-underlay",))

    def test_an_mlag_pair_shares_one_underlay_as(self) -> None:
        plan = _route(mlag_pairs={"leaf-a": "pair-mlag", "leaf-b": "pair-mlag"})

        assert [spec.key for spec in _autonomous_systems(plan)] == ["as:pair-mlag"]
        processes = _nodes(plan, "ManagedBGP")
        assert [processes[name].data["local_as"] for name in ("leaf-a-bgp-underlay", "leaf-b-bgp-underlay")] == [
            {"id": NodeRef("as:pair-mlag")}
        ] * 2

    def test_an_as_a_device_has_already_is_kept_and_tracked_and_a_pair_converges_on_the_lowest(self) -> None:
        reads = RoutingReads(underlay_as={"leaf-a": "as-9", "leaf-b": "as-2"})

        plan = _route(reads=reads, mlag_pairs={"leaf-a": "p", "leaf-b": "p"})

        assert _autonomous_systems(plan) == []
        assert _nodes(plan, "ManagedBGP")["leaf-a-bgp-underlay"].data["local_as"] == {"id": "as-2"}
        assert plan.of(TrackSpec) == [TrackSpec(("as-2",))]

    def test_a_shared_underlay_as_covers_every_bottom_device_and_draws_nothing(self) -> None:
        plan = _route(shared_underlay_as=NodeRef("shared-as"))

        assert _autonomous_systems(plan) == []
        assert [spec.data["local_as"] for spec in _nodes(plan, "ManagedBGP").values() if "underlay" in spec.key] == [
            {"id": NodeRef("shared-as")}
        ] * 2

    def test_a_device_without_an_as_pool_and_without_an_as_has_no_process(self) -> None:
        logger = MagicMock()
        plan = TopologyPlan(root_id="r")

        plan_routing(
            plan,
            shared=RoutingShared(strategy="ebgp-ebgp", overlay_as=None),
            devices=_LEAFS,
            bottom_devices=("leaf-a",),
            top_devices=("spine-1",),
            links=(_link("leaf-a", "spine-1"),),
            reads=RoutingReads(),
            mlag_pairs={},
            logger=logger,
        )

        logger.error.assert_called_once()
        assert "No ASN pool for leaf-a" in logger.error.call_args.args[0]
        assert not [spec for spec in plan.of(NodeSpec) if "underlay" in spec.key and spec.kind == "ManagedBGP"]

    def test_a_cable_is_a_peering_between_the_processes_with_both_ports_and_the_underlay_key(self) -> None:
        plan = _route(underlay_password="pw-1")

        peering = _nodes(plan, "ManagedBGPPeering")["underlay--leaf-a--Eth1_1--spine-1--Eth1_49"]
        assert peering.data["session_type"] == "EBGP" and peering.data["ttl"] == 1 and peering.data["bfd_enabled"]
        assert peering.data["bgp_processes"] == [
            Hfid("ManagedBGP", ("leaf-a-bgp-underlay",)),
            Hfid("ManagedBGP", ("spine-1-bgp-underlay",)),
        ]
        assert peering.data["capabilities"] == [
            Hfid("DcimPhysicalInterface", ("leaf-a", "Eth1/1")),
            Hfid("DcimPhysicalInterface", ("spine-1", "Eth1/49")),
        ]
        assert peering.data["password"] == {"id": "pw-1"} and peering.retry_on_conflict and peering.ownership == "owned"

    def test_the_top_device_has_no_process_of_its_own_here(self) -> None:
        plan = _route()

        assert "spine-1-bgp-underlay" not in _nodes(plan, "ManagedBGP")

    def test_parallel_cables_keep_the_first_and_a_self_loop_is_dropped(self) -> None:
        logger = MagicMock()
        plan = TopologyPlan(root_id="r")
        links = (
            _link("leaf-a", "spine-1", "Eth1/1", "Eth1/49"),
            _link("leaf-a", "spine-1", "Eth1/2", "Eth1/50"),
            _link("leaf-a", "leaf-a", "Eth1/3", "Eth1/4"),
        )

        plan_routing(
            plan,
            shared=RoutingShared(strategy="ebgp-ebgp", asn_pool="p"),
            devices=_LEAFS,
            bottom_devices=("leaf-a",),
            top_devices=("spine-1",),
            links=links,
            reads=RoutingReads(),
            mlag_pairs={},
            logger=logger,
        )

        assert list(_nodes(plan, "ManagedBGPPeering")) == ["underlay--leaf-a--Eth1_1--spine-1--Eth1_49"]
        logger.warning.assert_called_once()

    def test_a_cable_to_a_device_the_routing_does_not_know_is_not_a_peering(self) -> None:
        plan = _route(links=(_link("leaf-a", "spine-9"),), bottom=("leaf-a",))

        assert _nodes(plan, "ManagedBGPPeering") == {}

    def test_a_device_without_a_router_id_is_refused(self) -> None:
        devices = {**_LEAFS, "leaf-a": _device("leaf-a", "leaf", router_id=None)}

        with pytest.raises(RoutingPlanError, match="No router-id for leaf-a"):
            _route(devices=devices)

    def test_a_device_without_a_loopback_is_left_out(self) -> None:
        devices = {name: device for name, device in _LEAFS.items() if name != "leaf-b"}

        plan = _route(devices=devices)

        assert "leaf-b-bgp-underlay" not in _nodes(plan, "ManagedBGP")


class TestOverlay:
    def test_ibgp_runs_on_the_shared_as_and_clients_peer_with_the_route_reflector(self) -> None:
        plan = _route(
            "ebgp-ibgp",
            underlay_password="u",
            overlay_password="o",
            reads=RoutingReads(overlay=frozenset({"spine-1"})),
        )

        overlay = _nodes(plan, "ManagedBGP")["leaf-a-bgp-overlay"]
        assert overlay.data["local_as"] == {"id": "overlay-as"} and overlay.data["process_role"] == "overlay"
        peering = _nodes(plan, "ManagedBGPPeering")["overlay-evpn--leaf-a--spine-1"]
        assert peering.data["session_type"] == "IBGP" and peering.data["ttl"] == 255
        assert peering.data["route_reflector_client"] is True
        assert peering.data["address_families"] == [{"id": "af-evpn"}] and peering.data["password"] == {"id": "o"}
        assert peering.data["bgp_processes"] == [
            Hfid("ManagedBGP", ("leaf-a-bgp-overlay",)),
            Hfid("ManagedBGP", ("spine-1-bgp-overlay",)),
        ]
        assert peering.data["capabilities"] == [
            Hfid(_LOOPBACK, ("leaf-a", "Loopback0")),
            Hfid(_LOOPBACK, ("spine-1", "Loopback0")),
        ]

    def test_ebgp_overlay_uses_the_underlay_as_of_the_group(self) -> None:
        plan = _route(
            "ebgp-ebgp",
            mlag_pairs={"leaf-a": "p", "leaf-b": "p"},
            reads=RoutingReads(overlay=frozenset({"spine-1"})),
        )

        overlay = _nodes(plan, "ManagedBGP")["leaf-a-bgp-overlay"]
        assert overlay.data["local_as"] == {"id": NodeRef("as:p")}
        assert (
            _nodes(plan, "ManagedBGPPeering")["overlay-evpn--leaf-a--spine-1"].data["session_type"] == "EBGP_MULTIHOP"
        )

    def test_the_top_device_peers_with_the_overlay_process_it_already_has(self) -> None:
        plan = _route("ebgp-ibgp", reads=RoutingReads(overlay=frozenset({"spine-1"})))

        assert "spine-1-bgp-overlay" not in _nodes(plan, "ManagedBGP")
        assert "overlay-evpn--leaf-a--spine-1" in _nodes(plan, "ManagedBGPPeering")

    def test_a_top_device_without_an_overlay_process_is_not_peered_with(self) -> None:
        plan = _route("ebgp-ibgp")

        assert _nodes(plan, "ManagedBGPPeering").keys() == {
            name for name in _nodes(plan, "ManagedBGPPeering") if name.startswith("underlay")
        }

    def test_the_overlay_needs_the_shared_overlay_as(self) -> None:
        with pytest.raises(RoutingPlanError, match="shared overlay AS"):
            _route("ebgp-ibgp", overlay_as=None)

    def test_overlay_only_skips_the_underlay(self) -> None:
        plan = _route("ebgp-ibgp", skip_underlay=True, reads=RoutingReads(overlay=frozenset({"spine-1"})))

        assert {spec.data["process_role"] for spec in _nodes(plan, "ManagedBGP").values()} == {"overlay"}
        assert [name for name in _nodes(plan, "ManagedBGPPeering") if name.startswith("underlay")] == []

    def test_no_overlay_plans_the_underlay_only(self) -> None:
        plan = _route("ebgp-ibgp", no_overlay=True, overlay_as=None, evpn_address_family=None)

        assert {spec.data["process_role"] for spec in _nodes(plan, "ManagedBGP").values()} == {"underlay"}

    def test_no_overlay_and_no_underlay_plans_nothing(self) -> None:
        assert _route(no_overlay=True, skip_underlay=True).specs == []

    def test_a_route_reflector_tier_peers_among_itself_when_it_has_no_clients(self) -> None:
        devices = _devices(("super-1", "super-spine"), ("super-2", "super-spine"))

        plan = _route("ebgp-ibgp", devices=devices, bottom=("super-1", "super-2"), top=(), links=(), skip_underlay=True)

        peering = _nodes(plan, "ManagedBGPPeering")["overlay-evpn--super-1--super-2"]
        assert peering.data["route_reflector_client"] is False

    def test_an_unsupported_strategy_plans_nothing(self) -> None:
        logger = MagicMock()
        plan = TopologyPlan(root_id="r")

        plan_routing(
            plan,
            shared=RoutingShared(strategy="isis-ibgp"),
            devices=_LEAFS,
            bottom_devices=("leaf-a",),
            top_devices=(),
            links=(),
            reads=RoutingReads(),
            mlag_pairs={},
            logger=logger,
        )

        assert plan.specs == [] and logger.warning.call_count == 1


class TestOspfUnderlay:
    def test_every_device_has_a_process_every_cabled_port_an_interface_and_every_cable_a_peering(self) -> None:
        plan = _route("ospf-ibgp", ospf_area="area-0", underlay_password="u")

        processes = _nodes(plan, "ManagedOSPF")
        assert set(processes) == {"leaf-a-ospf-underlay", "leaf-b-ospf-underlay", "spine-1-ospf-underlay"}
        assert processes["leaf-a-ospf-underlay"].data["router_id"] == {"id": "ip-leaf-a"}
        interfaces = _nodes(plan, "RoutingOSPFInterface")
        assert set(interfaces) == {
            "leaf-a-Eth1/1-ospf-underlay",
            "spine-1-Eth1/49-ospf-underlay",
            "leaf-b-Eth1/1-ospf-underlay",
        } and interfaces["leaf-a-Eth1/1-ospf-underlay"].data["capabilities"] == [
            Hfid("DcimPhysicalInterface", ("leaf-a", "Eth1/1"))
        ]
        peering = _nodes(plan, "ManagedOSPFPeering")["leaf-a--Eth1_1--spine-1--Eth1_49-ospf-peering"]
        assert peering.data["ospf_area"] == {"id": "area-0"}
        assert peering.data["ospf_process"] == [
            Hfid("ManagedOSPF", ("leaf-a-ospf-underlay",)),
            Hfid("ManagedOSPF", ("spine-1-ospf-underlay",)),
        ]
        assert peering.data["ospf_interface"] == [
            Hfid("RoutingOSPFInterface", ("leaf-a-Eth1/1-ospf-underlay",)),
            Hfid("RoutingOSPFInterface", ("spine-1-Eth1/49-ospf-underlay",)),
        ]

    def test_the_ospf_area_is_required(self) -> None:
        with pytest.raises(RoutingPlanError, match="OSPF area"):
            _route("ospf-ibgp")


class TestOneCallAfterAnother:
    def test_a_process_planned_by_an_earlier_call_is_not_planned_again_and_keeps_its_as(self) -> None:
        """The DC pre-seeds its super-spines, then cables and routes them to the hyper-spines."""
        devices = _devices(("super-1", "super-spine"), ("hyper-1", "hyper-spine"))
        plan = _route(
            "ebgp-ebgp",
            devices=devices,
            bottom=("super-1",),
            top=(),
            links=(),
            shared_underlay_as=NodeRef("ss-as"),
        )
        _route(
            "ebgp-ebgp",
            plan=plan,
            devices=devices,
            bottom=("super-1",),
            top=("hyper-1",),
            links=(_link("super-1", "hyper-1"),),
        )

        keys = [spec.key for spec in plan.of(NodeSpec)]
        assert keys.count("bgp:super-1-bgp-underlay") == 1 and keys.count("bgp:super-1-bgp-overlay") == 1
        assert _nodes(plan, "ManagedBGP")["super-1-bgp-underlay"].data["local_as"] == {"id": NodeRef("ss-as")}
        assert _autonomous_systems(plan) == []
        assert "underlay--hyper-1--Eth1_49--super-1--Eth1_1" in _nodes(plan, "ManagedBGPPeering")


class TestMlagPairs:
    def test_devices_pair_two_at_a_time_in_name_order_under_the_domain_name(self) -> None:
        assert mlag_pairs_of([("b", "a", "c"), ("x", "y")]) == {
            "a": "a-b-mlag",
            "b": "a-b-mlag",
            "x": "x-y-mlag",
            "y": "x-y-mlag",
        }


@pytest.mark.asyncio
class TestReads:
    async def test_the_loopback_with_an_address_the_as_of_the_underlay_and_the_pairs_are_read(self) -> None:
        loopback = MagicMock(id="lb-1")
        loopback.name.value, loopback.device.peer.name.value, loopback.ip_address.id = "Loopback0", "leaf-a", "ip-1"
        underlay = MagicMock()
        underlay.capabilities.peers = [MagicMock(display_label="leaf-a")]
        underlay.process_role.value, underlay.local_as.id = "underlay", "as-1"
        overlay = MagicMock()
        overlay.capabilities.peers = [MagicMock(display_label="spine-1")]
        overlay.process_role.value = "overlay"
        domain = MagicMock()
        domain.name.value = "a-b-mlag"
        domain.capabilities.peers = [MagicMock(display_label="leaf-a"), MagicMock(display_label="leaf-b")]
        client = MagicMock()
        client.filters = AsyncMock(side_effect=[[loopback], [underlay, overlay], [domain]])

        reads = await read_routing_reads(client, ["leaf-a", "spine-1"])

        assert reads.loopbacks == {"leaf-a": (Hfid(_LOOPBACK, ("leaf-a", "Loopback0")), "ip-1")}
        assert reads.underlay_as == {"leaf-a": "as-1"} and reads.overlay == frozenset({"spine-1"})
        assert reads.mlag_pairs == {"leaf-a": "a-b-mlag", "leaf-b": "a-b-mlag"}

    async def test_no_device_reads_nothing(self) -> None:
        client = MagicMock()
        client.filters = AsyncMock(side_effect=AssertionError("nothing to read"))

        assert await read_routing_reads(client, []) == RoutingReads()

    async def test_a_device_is_ready_when_it_has_its_own_process_of_the_role(self) -> None:
        def process(device: str, role: str) -> MagicMock:
            item = MagicMock()
            item.process_role.value = role
            item.capabilities.peers = [MagicMock(display_label=device)]
            return item

        client = MagicMock()
        client.filters = AsyncMock(return_value=[process("sp1", "underlay"), process("sp2", "overlay")])

        assert await bgp_processes_ready(client, ["sp1"], "underlay")
        assert not await bgp_processes_ready(client, ["sp1", "sp2"], "underlay")
