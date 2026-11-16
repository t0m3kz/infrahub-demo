"""The MLAG pairing and wiring as plan data (generators/capabilities/mlag.py): what a plan holds for the pairs of the
devices a rack creates and for a domain that exists, with no client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.mlag import (
    MlagMember,
    MlagReads,
    mlag_domain_name,
    peer_ports,
    plan_mlag_domains,
    plan_mlag_wiring,
    read_mlag_reads,
)
from generators.dcim.cabling_plan import PortFacts
from generators.dcim.template_interfaces import TemplatePort
from generators.helpers.cable_attributes import RackPosition
from generators.plan import EdgesSpec, Hfid, NodeRef, NodeSpec, PrefixAllocationSpec, RetireSpec, TopologyPlan
from utils.limits import MLAG_CONTROL_IPV4_POOL, MLAG_CONTROL_IPV6_POOL

_TYPE = "100gbase-x-qsfp28"
_TEMPLATE: dict[str, Any] = {"id": "tmpl", "interfaces": [{"name": "Eth1/49", "role": "mlag-peer"}]}
_BOTH_POOLS = MlagReads(control_pools=frozenset({MLAG_CONTROL_IPV4_POOL, MLAG_CONTROL_IPV6_POOL}))


def _member(name: str, platform: str = "arista_eos", ports: tuple[str, ...] = ("Eth1/49", "Eth1/50")) -> MlagMember:
    return MlagMember(name, platform, tuple(PortFacts(name, port, _TYPE) for port in ports))


def _domains(
    names: list[str],
    *,
    mlag_create: str = "back-to-back",
    supports_virtual: bool = True,
    platform: str = "arista_eos",
    reads: MlagReads = _BOTH_POOLS,
    template: dict[str, Any] | None = None,
    problems: list[str] | None = None,
    ports: tuple[str, ...] = ("Eth1/49", "Eth1/50"),
) -> TopologyPlan:
    plan = TopologyPlan(root_id="rack-1")
    plan_mlag_domains(
        plan,
        names=names,
        role="leaf",
        template=template or _TEMPLATE,
        mlag_create=mlag_create,
        supports_virtual=supports_virtual,
        members={name: _member(name, platform, ports) for name in names},
        cable_deployment_id="dc-1",
        reads=reads,
        positions={},
        logger=MagicMock(),
        problems=problems if problems is not None else [],
    )
    return plan


def _node(plan: TopologyPlan, kind: str, *parts: str) -> NodeSpec:
    (spec,) = [s for s in plan.of(NodeSpec) if s.hfid == Hfid(kind, parts)]
    return spec


def _kinds(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if spec.kind == kind]


def _retired(plan: TopologyPlan) -> set[Hfid]:
    return {hfid for spec in plan.of(RetireSpec) for hfid in spec.hfids}


class TestTheDomain:
    def test_a_pair_is_one_domain_named_after_its_members_with_both_in_one_save(self) -> None:
        plan = _domains(["leaf-02", "leaf-01"])

        domain = _node(plan, "ManagedMLAG", "leaf-01-leaf-02-mlag")
        assert mlag_domain_name("leaf-01", "leaf-02") == "leaf-01-leaf-02-mlag"
        assert domain.data == {
            "name": "leaf-01-leaf-02-mlag",
            "peer_devices": "leaf-01,leaf-02",
            "domain_id": 1,
            "virtual_peer_link": False,
            "status": "active",
            "capabilities": [
                Hfid("DcimPhysicalDevice", ("leaf-01",)),
                Hfid("DcimPhysicalDevice", ("leaf-02",)),
            ],
            "member_of_groups": [Hfid("CoreStandardGroup", ("mlag_domains",))],
        }
        assert domain.tracked

    def test_pairs_are_taken_two_at_a_time_and_the_odd_device_is_unpaired(self) -> None:
        plan = _domains(["leaf-01", "leaf-02", "leaf-03", "leaf-04", "leaf-05"])

        domains = _kinds(plan, "ManagedMLAG")
        assert [(d.data["name"], d.data["domain_id"]) for d in domains] == [
            ("leaf-01-leaf-02-mlag", 1),
            ("leaf-03-leaf-04-mlag", 2),
        ]

    def test_one_device_is_nothing_to_pair(self) -> None:
        assert _domains(["leaf-01"]).specs == []

    def test_a_virtual_peer_link_marks_the_domain(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"], mlag_create="virtual")

        assert _node(plan, "ManagedMLAG", "leaf-01-leaf-02-mlag").data["virtual_peer_link"] is True

    def test_a_role_without_routing_pairs_back_to_back_whatever_the_pod_says(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"], mlag_create="virtual", supports_virtual=False)

        assert _node(plan, "ManagedMLAG", "leaf-01-leaf-02-mlag").data["virtual_peer_link"] is False
        assert len(_kinds(plan, "DcimLAGInterface")) == 2

    @pytest.mark.parametrize("mlag_create", ["back-to-back", "virtual"])
    def test_back_to_back_needs_an_mlag_peer_port_in_the_template_even_after_the_virtual_fallback(
        self, mlag_create: str
    ) -> None:
        problems: list[str] = []

        plan = _domains(
            ["leaf-01", "leaf-02"],
            mlag_create=mlag_create,
            supports_virtual=False,
            template={"id": "tmpl-x", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]},
            problems=problems,
        )

        assert plan.specs == []
        assert problems == ["template tmpl-x has no mlag-peer interface — cannot create back-to-back MLAG for leafs."]

    def test_a_virtual_pair_needs_no_port_in_the_template(self) -> None:
        problems: list[str] = []

        plan = _domains(
            ["leaf-01", "leaf-02"],
            mlag_create="virtual",
            template={"id": "tmpl-x", "interfaces": []},
            problems=problems,
        )

        assert problems == [] and len(_kinds(plan, "ManagedMLAG")) == 1


class TestBackToBack:
    def test_each_member_gets_a_lag_of_all_its_peer_ports_and_the_ports_go_active_untracked(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"])

        lag = _node(plan, "DcimLAGInterface", "leaf-01", "Port-Channel100")
        assert lag.data == {
            "name": "Port-Channel100",
            "device": Hfid("DcimPhysicalDevice", ("leaf-01",)),
            "status": "active",
            "role": "mlag-peer",
            "lag_id": 100,
            "lacp_mode": "active",
            "mtu": 9000,
            "minimum_links": 1,
            "mlag_domain": Hfid("ManagedMLAG", ("leaf-01-leaf-02-mlag",)),
            "member_interfaces": [
                Hfid("DcimPhysicalInterface", ("leaf-01", "Eth1/49")),
                Hfid("DcimPhysicalInterface", ("leaf-01", "Eth1/50")),
            ],
        }
        assert lag.tracked
        ports = _kinds(plan, "DcimPhysicalInterface")
        assert len(ports) == 4 and all(
            p.existing and p.ownership == "template" and p.data["status"] == "active" for p in ports
        )

    def test_the_lag_is_named_by_the_platform_of_its_device(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"], platform="cisco_nxos")

        assert {spec.data["name"] for spec in _kinds(plan, "DcimLAGInterface")} == {"port-channel100"}

    def test_the_ports_are_cabled_pairwise_in_sorted_order_filed_under_the_given_deployment(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"])

        cables = sorted(_kinds(plan, "DcimCable"), key=lambda spec: spec.data["name"])
        assert [(c.data["name"], c.data["endpoints"], c.data["deployment"]) for c in cables] == [
            (
                f"CBL-leaf-01-leaf-02-mlag-PL{index}",
                [
                    Hfid("DcimPhysicalInterface", ("leaf-01", f"Eth1/{48 + index}")),
                    Hfid("DcimPhysicalInterface", ("leaf-02", f"Eth1/{48 + index}")),
                ],
                {"id": "dc-1"},
            )
            for index in (1, 2)
        ]
        assert {c.data["type"] for c in cables} == {"smf"} and all(c.tracked for c in cables)

    def test_a_cable_is_as_long_as_the_racks_of_its_ends_say(self) -> None:
        position = RackPosition("suite-1", 1, 1, 2, 8, 60, 120)
        plan = TopologyPlan(root_id="rack-1")
        plan_mlag_wiring(
            plan,
            domain=Hfid("ManagedMLAG", ("a-b-mlag",)),
            domain_node=NodeRef("mlag:a-b-mlag"),
            members=(_member("a", ports=("Eth1/49",)), _member("b", ports=("Eth1/49",))),
            virtual_peer_link=False,
            cable_deployment_id=None,
            reads=_BOTH_POOLS,
            positions={"a": position, "b": position},
            ownership="owned",
            logger=MagicMock(),
            problems=[],
        )

        (cable,) = _kinds(plan, "DcimCable")
        assert "length_cm" in cable.data and "deployment" not in cable.data

    def test_a_cable_the_ports_already_carry_under_another_name_is_adopted_by_id(self) -> None:
        reads = MlagReads(
            control_pools=_BOTH_POOLS.control_pools,
            cables={"CBL-leaf-01-leaf-02-mlag-PL1": "cable-1"},
            port_cables={("leaf-02", "Eth1/50"): "cable-orphan"},
        )

        plan = _domains(["leaf-01", "leaf-02"], reads=reads)

        assert _node(plan, "DcimCable", "CBL-leaf-01-leaf-02-mlag-PL1").data["id"] == "cable-1"
        assert _node(plan, "DcimCable", "CBL-leaf-01-leaf-02-mlag-PL2").data["id"] == "cable-orphan"

    def test_a_member_without_peer_ports_fails_the_plan_and_no_cable_is_planned(self) -> None:
        problems: list[str] = []

        plan = _domains(["leaf-01", "leaf-02"], ports=(), problems=problems)

        assert problems == [
            "[leaf-01] No physical interfaces with role=mlag-peer found — cannot create peer-link LAG",
            "[leaf-02] No physical interfaces with role=mlag-peer found — cannot create peer-link LAG",
        ]
        assert _kinds(plan, "DcimCable") == []

    def test_the_other_modes_leftovers_are_retired(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"])

        assert _retired(plan) == {
            Hfid("DcimVirtualInterface", ("leaf-01", "Loopback100")),
            Hfid("DcimVirtualInterface", ("leaf-02", "Loopback100")),
        }

    def test_the_domain_lists_the_ports_the_lags_and_the_control_svis(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"])

        (edges,) = plan.of(EdgesSpec)
        assert edges.node == NodeRef("mlag:leaf-01-leaf-02-mlag") and edges.relation == "interfaces"
        assert set(edges.peers) == {
            *(Hfid("DcimPhysicalInterface", (d, p)) for d in ("leaf-01", "leaf-02") for p in ("Eth1/49", "Eth1/50")),
            *(Hfid("DcimLAGInterface", (d, "Port-Channel100")) for d in ("leaf-01", "leaf-02")),
            *(Hfid("DcimVirtualInterface", (d, "Vlan4094")) for d in ("leaf-01", "leaf-02")),
        }


class TestControlSession:
    def test_the_control_prefix_is_keyed_by_the_domains_id_and_the_lower_address_goes_to_the_first_member(self) -> None:
        plan = _domains(["leaf-02", "leaf-01"], platform="sonic")

        (prefix,) = plan.of(PrefixAllocationSpec)
        assert (prefix.identifier, prefix.identifier_ref) == (
            "mlag-control",
            Hfid("ManagedMLAG", ("leaf-01-leaf-02-mlag",)),
        )
        assert (prefix.pool_name, prefix.prefix_length, prefix.address_length) == (MLAG_CONTROL_IPV4_POOL, 31, 31)
        assert (prefix.role, prefix.is_pool, prefix.member_type) == ("technical", True, "address")
        assert prefix.address_offsets == (0, 1) and prefix.address_ownership == "owned"
        assert _node(plan, "DcimVirtualInterface", "leaf-01", "Vlan4094").data["ip_address"] == NodeRef(
            prefix.address_key(0)
        )
        assert _node(plan, "DcimVirtualInterface", "leaf-02", "Vlan4094").data["ip_address"] == NodeRef(
            prefix.address_key(1)
        )

    def test_other_platforms_get_an_ipv6_session(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"])

        (prefix,) = plan.of(PrefixAllocationSpec)
        assert (prefix.pool_name, prefix.prefix_length, prefix.address_length) == (MLAG_CONTROL_IPV6_POOL, 127, 127)

    def test_a_virtual_peer_link_carries_the_address_on_its_loopback_as_a_host_route(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"], mlag_create="virtual")

        (prefix,) = plan.of(PrefixAllocationSpec)
        assert (prefix.prefix_length, prefix.address_length) == (127, 128)
        loopback = _node(plan, "DcimVirtualInterface", "leaf-01", "Loopback100")
        assert loopback.data["role"] == "mlag-peer" and loopback.data["ip_address"] == NodeRef(prefix.address_key(0))
        assert _kinds(plan, "DcimLAGInterface") == [] and _kinds(plan, "DcimCable") == []

    def test_vpc_has_no_control_session_and_no_control_svi_is_kept(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"], platform="cisco_nxos")

        assert plan.of(PrefixAllocationSpec) == []
        assert Hfid("DcimVirtualInterface", ("leaf-01", "Vlan4094")) in _retired(plan)
        assert [s.data["name"] for s in _kinds(plan, "DcimVirtualInterface")] == []

    def test_a_pool_that_is_not_loaded_skips_the_addressing_and_leaves_the_svis_of_a_virtual_pair_alone(self) -> None:
        logger = MagicMock()
        plan = TopologyPlan(root_id="rack-1")
        plan_mlag_wiring(
            plan,
            domain=Hfid("ManagedMLAG", ("a-b-mlag",)),
            domain_node=NodeRef("mlag:a-b-mlag"),
            members=(_member("a"), _member("b")),
            virtual_peer_link=True,
            cable_deployment_id=None,
            reads=MlagReads(),
            positions={},
            ownership="owned",
            logger=logger,
            problems=[],
        )

        assert plan.of(PrefixAllocationSpec) == []
        assert Hfid("DcimVirtualInterface", ("a", "Vlan4094")) not in _retired(plan)
        assert "skipping MLAG control addressing" in logger.warning.call_args.args[0]
        assert "ip_address" not in _node(plan, "DcimVirtualInterface", "a", "Loopback100").data

    def test_a_virtual_pair_retires_the_svis_the_cables_and_the_lags_of_back_to_back(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"], mlag_create="virtual")

        assert _retired(plan) == {
            *(Hfid("DcimLAGInterface", (d, "Port-Channel100")) for d in ("leaf-01", "leaf-02")),
            *(Hfid("DcimVirtualInterface", (d, "Vlan4094")) for d in ("leaf-01", "leaf-02")),
            *(Hfid("DcimCable", (f"CBL-leaf-01-leaf-02-mlag-PL{n}",)) for n in (1, 2)),
        }


class TestOwnership:
    def test_the_wiring_of_a_domain_that_exists_is_never_the_runs_output(self) -> None:
        plan = TopologyPlan(root_id=None)
        plan_mlag_wiring(
            plan,
            domain=Hfid("ManagedMLAG", ("a-b-mlag",)),
            domain_node="mlag-id",
            members=(_member("a"), _member("b")),
            virtual_peer_link=False,
            cable_deployment_id=None,
            reads=_BOTH_POOLS,
            positions={},
            ownership="shared",
            logger=MagicMock(),
            problems=[],
        )

        written = [spec for spec in plan.of(NodeSpec)]
        assert written and not any(spec.tracked for spec in written)
        (prefix,) = plan.of(PrefixAllocationSpec)
        assert prefix.address_ownership == "shared"
        (edges,) = plan.of(EdgesSpec)
        assert edges.node == "mlag-id"

    def test_a_domain_the_run_creates_is_the_runs_output_with_all_its_wiring(self) -> None:
        plan = _domains(["leaf-01", "leaf-02"])

        owned = [spec for spec in plan.of(NodeSpec) if spec.ownership == "owned"]
        assert {spec.kind for spec in owned} == {"ManagedMLAG", "DcimLAGInterface", "DcimVirtualInterface", "DcimCable"}


class TestPeerPorts:
    def test_the_ports_of_a_device_that_exists_win_over_its_templates(self) -> None:
        live = (PortFacts("leaf-01", "Eth1/60", _TYPE),)
        template_ports = [TemplatePort("Eth1/49", "mlag-peer", _TYPE)]

        assert peer_ports("leaf-01", MlagReads(ports={"leaf-01": live}), template_ports) == live

    def test_a_new_device_has_the_sorted_mlag_peer_ports_of_its_template(self) -> None:
        template_ports = [
            TemplatePort("Eth1/50", "mlag-peer", _TYPE),
            TemplatePort("Eth1/1", "uplink", _TYPE),
            TemplatePort("Eth1/49", "mlag-peer", _TYPE),
        ]

        assert [port.port_name for port in peer_ports("leaf-01", MlagReads(), template_ports)] == ["Eth1/49", "Eth1/50"]


class TestReads:
    @pytest.mark.asyncio
    async def test_the_pools_the_ports_the_cable_on_each_and_the_peer_link_cables_that_exist(self) -> None:
        def port(device: str, name: str, cable: str | None) -> MagicMock:
            node = MagicMock()
            node.name = MagicMock(value=name)
            node.interface_type = MagicMock(value=_TYPE)
            node.device.peer.name.value = device
            node.cable = MagicMock(id=cable, initialized=cable is not None)
            return node

        existing = MagicMock(id="cable-1")
        existing.name = MagicMock(value="CBL-a-b-mlag-PL1")
        pool = MagicMock()
        pool.name = MagicMock(value=MLAG_CONTROL_IPV4_POOL)
        seen: dict[str, Any] = {}

        async def filters(kind: Any, **kwargs: Any) -> list[Any]:
            kind_name = kind if isinstance(kind, str) else kind.__name__
            seen[kind_name] = kwargs
            return {
                "CoreIPPrefixPool": [pool],
                "DcimPhysicalInterface": [
                    port("b", "Eth1/50", "c7"),
                    port("a", "Eth1/49", None),
                    port("a", "Eth1/50", "c8"),
                ],
                "DcimCable": [existing],
            }[kind_name]

        client = MagicMock()
        client.filters = filters

        reads = await read_mlag_reads(client, device_names=["a", "b", "a"], domain_names=["a-b-mlag"])

        assert reads.control_pools == {MLAG_CONTROL_IPV4_POOL}
        assert reads.ports == {
            "a": (PortFacts("a", "Eth1/49", _TYPE), PortFacts("a", "Eth1/50", _TYPE)),
            "b": (PortFacts("b", "Eth1/50", _TYPE),),
        }
        assert reads.port_cables == {("b", "Eth1/50"): "c7", ("a", "Eth1/50"): "c8"}
        assert reads.cables == {"CBL-a-b-mlag-PL1": "cable-1"}
        # no more cable names than the most ports of one member
        assert seen["DcimCable"]["name__values"] == ["CBL-a-b-mlag-PL1", "CBL-a-b-mlag-PL2"]
        assert seen["DcimPhysicalInterface"]["device__name__values"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_no_device_is_no_read(self) -> None:
        client = MagicMock()
        client.filters = AsyncMock()

        assert await read_mlag_reads(client, device_names=[], domain_names=[]) == MlagReads()
        client.filters.assert_not_awaited()
