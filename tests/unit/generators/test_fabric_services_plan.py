"""The HA-paired service devices of a DC or pod and their cables to the border tier, as plan data and applied on the HFID
fake (generators/capabilities/ha.py, generators/dcim/cabling_plan.py): firewalls, load balancers, the Internet-edge
pair, their HA domains (group id, sync ports and cable) and the chain cabling."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.ha import (
    FIREWALL_CONTEXT,
    LOADBALANCER_CONTEXT,
    HaDomain,
    HaReads,
    ServiceReads,
    context_vlan_pool_lock,
    ha_lock_keys,
    plan_context_vlan_pool,
    plan_fabric_services,
    plan_ha_pairs,
    read_ha_reads,
    read_service_reads,
    service_entries,
    service_lock_keys,
    service_names,
)
from generators.dcim.cabling_plan import (
    ELSEWHERE,
    ChainPort,
    chain_ports_of,
    plan_chain_legs,
    raise_problems,
    read_chain_ports,
)
from generators.dcim.device_plan import DeviceOptions, DeviceReads, plan_physical_devices
from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from generators.helpers.cable_attributes import RackPosition
from generators.model.errors import GeneratorError
from generators.plan import Hfid, NodeSpec, TopologyPlan, TrackSpec
from tests.unit.plan_graph_helpers import HfidClient, wire_hfid_generator
from tests.unit.plan_reconciler_helpers import PlanGenerator, make_generator_of

_TYPE = "10gbase-x-sfpp"
_FW_TEMPLATE = {"id": "tmpl-fw", "platform": {"id": "plat-fw"}, "device_type": {"id": "dt-fw"}}
_LB_TEMPLATE = {"id": "tmpl-lb", "platform": {"id": "plat-lb"}, "device_type": {"id": "dt-lb"}}
_BL_TEMPLATE = {"id": "tmpl-bl", "platform": {"id": "plat-bl"}, "device_type": {"id": "dt-bl"}}


def _entry(role: str, quantity: int, template: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "quantity": quantity, "template": template}


def _ports(*specs: tuple[str, str]) -> list[TemplatePort]:
    return [TemplatePort(name, role, _TYPE) for name, role in specs]


_FW_PORTS = _ports(("eth1", "uplink"), ("eth2", "uplink"), ("eth3", "downlink"), ("ha1", "ha"))
_LB_PORTS = _ports(("1.1", "uplink"), ("1.2", "uplink"), ("1.3", "downlink"), ("ha1", "ha"))
_BL_PORTS = _ports(
    *[(f"Eth1/{n}", "firewall") for n in range(1, 9)], *[(f"Eth1/{n}", "load-balancer") for n in range(9, 13)]
)
_TEMPLATES = TemplateReads(
    ports={"tmpl-fw": _FW_PORTS, "tmpl-lb": _LB_PORTS, "tmpl-bl": _BL_PORTS},
    owners={},
)


def _reads(**ha: Any) -> ServiceReads:
    return ServiceReads(ha=HaReads(group_pool_id="pool-ha", **ha), positions={}, chain_ports={})


def _plan(
    entries: list[tuple[str, dict[str, Any]]],
    *,
    reads: ServiceReads | None = None,
    mode: Any = "pbr",
    problems: list[str] | None = None,
    border_ports: list[TemplatePort] | None = None,
) -> tuple[TopologyPlan, Any]:
    plan = TopologyPlan(root_id="dc-1")
    # The border-leafs of the DC are in the same plan
    plan_physical_devices(
        plan,
        reads=DeviceReads(),
        fabric_name="dc1",
        pod_name="",
        role="border-leaf",
        quantity=1,
        deployment_id="dc-1",
        template=_BL_TEMPLATE,
        template_ports=_BL_PORTS,
        template_owner_id=None,
        naming_convention="standard",
        options=DeviceOptions(indexes=[1]),
        controller=None,
    )
    services = plan_fabric_services(
        plan,
        entries=entries,
        fabric_name="dc1",
        deployment_id="dc-1",
        cable_deployment_id="dc-1",
        naming_convention="standard",
        indexes=[1],
        connectivity_mode=mode,
        border_names=_borders_of(plan),
        border_ports=dict.fromkeys(_borders_of(plan), _BL_PORTS if border_ports is None else border_ports),
        devices=DeviceReads(),
        templates=_TEMPLATES,
        reads=reads or _reads(),
        controllers=[],
        logger=MagicMock(),
        problems=problems if problems is not None else [],
    )
    return plan, services


def _borders_of(plan: TopologyPlan) -> list[str]:
    return sorted(spec.data["name"] for spec in plan.of(NodeSpec) if spec.kind == "DcimPhysicalDevice")


def _of_kind(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if spec.kind == kind]


def _cables(plan: TopologyPlan) -> list[str]:
    return sorted(spec.data["name"] for spec in _of_kind(plan, "DcimCable"))


def _entries(*roles: str, quantity: int = 2) -> list[tuple[str, dict[str, Any]]]:
    templates = {"internal-firewall": _FW_TEMPLATE, "external-firewall": _FW_TEMPLATE, "load-balancer": _LB_TEMPLATE}
    return [(role, _entry(role, quantity, templates[role])) for role in roles]


class TestEntriesAndNames:
    def test_firewalls_then_load_balancers_and_the_external_pair_only_when_asked(self) -> None:
        templates = [
            _entry("load-balancer", 1, _LB_TEMPLATE),
            _entry("internal-firewall", 2, _FW_TEMPLATE),
            _entry("external-firewall", 2, _FW_TEMPLATE),
            _entry("leaf", 4, {}),
        ]

        assert [role for role, _ in service_entries(templates)] == ["internal-firewall", "load-balancer"]
        assert [role for role, _ in service_entries(templates, external=True)] == [
            "internal-firewall",
            "load-balancer",
            "external-firewall",
        ]

    def test_two_entries_of_a_role_are_two_groups(self) -> None:
        entries = service_entries([_entry("load-balancer", 1, _LB_TEMPLATE), _entry("load-balancer", 1, _LB_TEMPLATE)])

        assert len(entries) == 2

    def test_the_names_are_the_ones_the_device_plan_gives(self) -> None:
        entries = _entries("internal-firewall")

        (names,) = service_names(entries, fabric_name="dc1", naming_convention="standard", indexes=[1])
        plan, services = _plan(entries)

        assert services.firewalls == names and len(names) == 2

    def test_the_locks_are_the_domains_and_the_group_id_pool(self) -> None:
        entries = _entries("internal-firewall", "load-balancer")
        names = service_names(entries, fabric_name="dc1", naming_convention="standard", indexes=[1])

        keys = service_lock_keys(entries, names)

        assert keys == [
            f"ha-domain-{names[0][0]}-{names[0][1]}",
            "firewall-ha-group-pool",
            f"ha-domain-{names[1][0]}-{names[1][1]}",
        ]
        assert ha_lock_keys(["a"], firewall=True) == []


class TestServiceDevices:
    def test_each_entry_is_a_tracked_group_without_loopbacks_deployed_to_the_fabric(self) -> None:
        plan, services = _plan(_entries("internal-firewall", "load-balancer"))

        devices = [s for s in _of_kind(plan, "DcimPhysicalDevice") if s.data["role"] != "border-leaf"]
        assert [s.data["role"] for s in devices] == ["internal-firewall"] * 2 + ["load-balancer"] * 2
        assert all(s.tracked and s.data["deployment"] == {"id": "dc-1"} for s in devices)
        assert not [s for s in _of_kind(plan, "DcimVirtualInterface")]
        assert len(services.firewalls) == 2 and len(services.load_balancers) == 2 and services.external_firewalls == []

    def test_the_external_pair_is_its_own_role_and_disjoint(self) -> None:
        _, services = _plan(_entries("internal-firewall", "external-firewall"))

        assert set(services.external_firewalls).isdisjoint(services.firewalls) and len(services.external_firewalls) == 2


class TestHaDomains:
    def test_a_pair_is_one_domain_with_both_members_the_ha_group_and_a_group_id_drawn_from_the_pool(self) -> None:
        plan, services = _plan(_entries("internal-firewall"))

        (domain,) = _of_kind(plan, "ManagedFirewallHA")
        first, second = services.firewalls
        assert domain.data == {
            "name": f"{first}-{second}",
            "status": "active",
            "capabilities": [Hfid("DcimPhysicalDevice", (first,)), Hfid("DcimPhysicalDevice", (second,))],
            "member_of_groups": [Hfid("CoreStandardGroup", ("ha_domains",))],
        }
        assert domain.create_only == {"group_id": {"from_pool": {"id": "pool-ha"}}}
        assert domain.sequential and domain.tracked

    def test_a_load_balancer_pair_has_no_group_id(self) -> None:
        plan, _ = _plan(_entries("load-balancer"))

        (domain,) = _of_kind(plan, "ManagedLoadbalancerHA")
        assert domain.create_only == {}

    def test_an_odd_device_out_is_unpaired(self) -> None:
        plan, services = _plan(_entries("internal-firewall", quantity=3))

        assert len(services.firewalls) == 3 and len(_of_kind(plan, "ManagedFirewallHA")) == 1

    def test_a_domain_that_exists_is_only_kept_in_the_runs_group(self) -> None:
        names = service_names(
            _entries("internal-firewall"), fabric_name="dc1", naming_convention="standard", indexes=[1]
        )[0]
        existing = HaDomain(id="ha-1", group_id=7)

        plan, _ = _plan(_entries("internal-firewall"), reads=_reads(domains={f"{names[0]}-{names[1]}": existing}))

        assert _of_kind(plan, "ManagedFirewallHA") == [] and TrackSpec(("ha-1",)) in plan.specs

    def test_an_older_firewall_pair_without_a_group_id_is_given_one_by_id(self) -> None:
        names = service_names(
            _entries("internal-firewall"), fabric_name="dc1", naming_convention="standard", indexes=[1]
        )[0]
        existing = HaDomain(id="ha-1", group_id=None)

        plan, _ = _plan(_entries("internal-firewall"), reads=_reads(domains={f"{names[0]}-{names[1]}": existing}))

        (domain,) = _of_kind(plan, "ManagedFirewallHA")
        assert domain.data == {"id": "ha-1", "group_id": {"from_pool": {"id": "pool-ha"}}}

    def test_a_group_id_pool_that_is_not_loaded_is_created_once_untracked(self) -> None:
        reads = ServiceReads(ha=HaReads(), positions={}, chain_ports={})

        plan, _ = _plan(_entries("internal-firewall"), reads=reads)

        (pool,) = _of_kind(plan, "CoreNumberPool")
        assert (
            pool.data["name"] == "Firewall-HA-Group-ID"
            and pool.data["node"] == "ManagedFirewallHA"
            and not pool.tracked
        )
        (domain,) = _of_kind(plan, "ManagedFirewallHA")
        assert domain.create_only["group_id"]["from_pool"]["id"].key == pool.key

    def test_two_firewall_entries_share_the_one_pool_that_is_created(self) -> None:
        reads = ServiceReads(ha=HaReads(), positions={}, chain_ports={})

        plan, _ = _plan(_entries("internal-firewall", "external-firewall"), reads=reads)

        assert len(_of_kind(plan, "CoreNumberPool")) == 1 and len(_of_kind(plan, "ManagedFirewallHA")) == 2

    def test_each_member_has_its_first_ha_port_active_untracked_and_the_pair_one_sync_cable(self) -> None:
        plan, services = _plan(_entries("internal-firewall"))

        ports = [s for s in _of_kind(plan, "DcimPhysicalInterface") if s.key.startswith("sync-port:")]
        assert [(s.data["device"].parts[0], s.data["name"], s.data["status"]) for s in ports] == [
            (services.firewalls[0], "ha1", "active"),
            (services.firewalls[1], "ha1", "active"),
        ]
        assert all(s.existing and s.ownership == "template" for s in ports)
        first, second = services.firewalls
        cable = next(s for s in _of_kind(plan, "DcimCable") if s.data["name"].endswith("-SYNC"))
        assert cable.data["name"] == f"CBL-{first}-{second}-SYNC" and cable.data["type"] == "smf"
        assert cable.data["endpoints"] == [
            Hfid("DcimPhysicalInterface", (first, "ha1")),
            Hfid("DcimPhysicalInterface", (second, "ha1")),
        ]
        assert cable.data["deployment"] == {"id": "dc-1"} and cable.tracked

    def test_a_sync_cable_that_exists_is_kept_not_rewritten(self) -> None:
        names = service_names(
            _entries("internal-firewall"), fabric_name="dc1", naming_convention="standard", indexes=[1]
        )[0]
        pair = f"{names[0]}-{names[1]}"

        plan, _ = _plan(_entries("internal-firewall"), reads=_reads(sync_cables={f"CBL-{pair}-SYNC": "cable-1"}))

        assert not [s for s in _of_kind(plan, "DcimCable") if s.data["name"].endswith("-SYNC")]
        assert TrackSpec(("cable-1",)) in plan.specs

    def test_sync_ports_cabled_under_another_name_are_left_alone(self) -> None:
        names = service_names(
            _entries("internal-firewall"), fabric_name="dc1", naming_convention="standard", indexes=[1]
        )[0]

        plan, _ = _plan(
            _entries("internal-firewall"),
            reads=_reads(sync_ports={names[0]: "ha1", names[1]: "ha1"}, cabled_sync=frozenset({names[0]})),
        )

        assert not [s for s in _of_kind(plan, "DcimCable") if s.data["name"].endswith("-SYNC")]

    def test_a_member_without_a_sync_port_fails_the_plan_naming_it(self) -> None:
        problems: list[str] = []
        plan = TopologyPlan(root_id="dc-1")

        plan_ha_pairs(
            plan,
            names=["fw-01", "fw-02"],
            role="internal-firewall",
            reads=HaReads(group_pool_id="pool-ha"),
            template_ports={"fw-01": _ports(("eth1", "uplink")), "fw-02": _FW_PORTS},
            positions={},
            deployment_id="dc-1",
            problems=problems,
        )

        assert problems == ["[fw-01] No HA sync interface found (expected role=ha)"]
        assert [s for s in _of_kind(plan, "DcimCable")] == []

    def test_the_sync_cable_is_as_long_as_the_racks_say(self) -> None:
        position = RackPosition("suite-1", 1, 1, 2, 8, 60, 120)
        plan = TopologyPlan(root_id="dc-1")

        plan_ha_pairs(
            plan,
            names=["fw-01", "fw-02"],
            role="internal-firewall",
            reads=HaReads(group_pool_id="pool-ha"),
            template_ports={"fw-01": _FW_PORTS, "fw-02": _FW_PORTS},
            positions={"fw-01": position, "fw-02": position},
            deployment_id="dc-1",
            problems=[],
        )

        (cable,) = _of_kind(plan, "DcimCable")
        assert cable.data["length_cm"] == 100


def _border(plan: TopologyPlan) -> str:
    """The name of the DC's one border-leaf."""
    (spec,) = [s for s in _of_kind(plan, "DcimPhysicalDevice") if s.data["role"] == "border-leaf"]
    return spec.data["name"]


def _links(plan: TopologyPlan) -> set[tuple[str, str, str, str]]:
    """(border, border port, service, service port) of every border cable of the plan."""
    border = _border(plan)
    links = set()
    for cable in _of_kind(plan, "DcimCable"):
        ends = sorted((end.parts for end in cable.data["endpoints"]), key=lambda parts: parts[0] != border)
        if ends[0][0] == border:
            links.add((*ends[0], *ends[1]))
    return links


class TestChainCabling:
    def test_pbr_cables_each_service_to_the_border_tier_on_its_own_ports(self) -> None:
        plan, services = _plan(_entries("internal-firewall", "load-balancer", quantity=1))

        border, fw, lb = _border(plan), services.firewalls[0], services.load_balancers[0]
        assert _links(plan) == {
            (border, "Eth1/1", fw, "eth1"),
            (border, "Eth1/2", fw, "eth2"),
            (border, "Eth1/9", lb, "1.1"),
            (border, "Eth1/10", lb, "1.2"),
        }

    def test_a_cable_has_no_p2p_address_and_both_ports_go_active_untracked(self) -> None:
        plan, _ = _plan(_entries("internal-firewall", quantity=1))

        assert not [s for s in plan.specs if type(s).__name__ == "LinkPrefixSpec"]
        ports = [s for s in _of_kind(plan, "DcimPhysicalInterface") if s.key.startswith("port:")]
        assert ports and all(s.ownership == "template" and s.data["status"] == "active" for s in ports)
        assert all("ip_address" not in s.data for s in ports)
        assert all(s.data["deployment"] == {"id": "dc-1"} for s in _of_kind(plan, "DcimCable"))

    def test_inline_chains_the_border_through_the_firewall_and_the_load_balancer_and_back(self) -> None:
        plan, services = _plan(_entries("internal-firewall", "load-balancer", quantity=1), mode="inline")

        border, fw, lb = _border(plan), services.firewalls[0], services.load_balancers[0]
        assert _links(plan) == {
            (border, "Eth1/1", fw, "eth1"),
            (border, "Eth1/2", fw, "eth2"),
            # the load balancer's return leg to the border uses its downlink port (the uplinks face the firewall)
            (border, "Eth1/9", lb, "1.3"),
        }
        assert f"CBL-{fw}-{lb}" not in _cables(plan)
        assert any(fw in name and lb in name for name in _cables(plan))

    def test_the_external_pair_takes_the_next_free_firewall_ports_of_the_border_tier(self) -> None:
        plan, services = _plan(_entries("internal-firewall", "external-firewall", quantity=2))

        links = _links(plan)
        border_ports = [(border, port) for border, port, *_ in links]
        # no border port is cabled twice, and each pair has its own
        assert len(border_ports) == len(set(border_ports)) == 8
        assert {link[2] for link in links} == {*services.firewalls, *services.external_firewalls}

    def test_a_port_cabled_elsewhere_is_not_free_but_one_cabled_to_the_peer_is(self) -> None:
        live = {
            "b1": [
                ChainPort("e1", "firewall", _TYPE, cabled_to="other"),
                ChainPort("e2", "firewall", _TYPE, cabled_to="f1"),
                ChainPort("e3", "firewall", _TYPE),
            ],
            "f1": [ChainPort("u1", "uplink", _TYPE), ChainPort("u2", "uplink", _TYPE)],
        }
        plan = TopologyPlan(root_id="dc-1")
        problems: list[str] = []

        links = plan_chain_legs(
            plan,
            hops=[{"devices": ["b1"], "down_role": "firewall"}, {"devices": ["f1"], "up_role": "uplink"}],
            ports=chain_ports_of(live, {}),
            deployment_id="dc-1",
            positions={},
            problems=problems,
        )

        assert problems == []
        assert sorted(link.top.port_name for link in links) == ["e2", "e3"]

    def test_a_leg_without_a_port_of_the_role_is_a_problem_raised_before_anything_is_written(self) -> None:
        problems: list[str] = []

        plan, _ = _plan(
            _entries("internal-firewall", quantity=1),
            problems=problems,
            border_ports=_ports(("Eth1/1", "uplink")),
        )

        assert problems and "firewall_ports=0" in problems[0]
        assert not _links(plan)

    def test_a_hop_without_devices_is_nothing_to_cable(self) -> None:
        plan = TopologyPlan(root_id="dc-1")

        links = plan_chain_legs(
            plan,
            hops=[{"devices": [], "down_role": "firewall"}, {"devices": ["f1"], "up_role": "uplink"}],
            ports=chain_ports_of({}, {}),
            deployment_id="dc-1",
            positions={},
            problems=[],
        )

        assert links == [] and plan.specs == []

    def test_the_ports_of_a_device_that_exists_win_over_its_templates(self) -> None:
        ports = chain_ports_of(
            {"b1": [ChainPort("live", "firewall", _TYPE)]},
            {"b1": _ports(("tmpl", "firewall")), "b2": _ports(("t2", "x"))},
        )

        assert [p.port_name for p in ports.with_role(["b1", "b2"], "firewall")] == ["live"]


class TestContextVlanPools:
    def test_the_pool_is_a_number_pool_of_the_kind_owned_by_the_run(self) -> None:
        plan = TopologyPlan(root_id="dc-1")

        ref = plan_context_vlan_pool(plan, name="dc1", spec=FIREWALL_CONTEXT)

        (pool,) = plan.of(NodeSpec)
        assert ref.key == pool.key and pool.tracked and pool.kind == "CoreNumberPool"
        assert pool.data["name"] == "dc1-fw-context-vlan-pool"
        assert (pool.data["start_range"], pool.data["end_range"]) == FIREWALL_CONTEXT.vlan_range
        assert (pool.data["node"], pool.data["node_attribute"]) == ("ManagedFirewallContext", "vlan_id")

    def test_the_two_kinds_keep_apart_bands_and_locks(self) -> None:
        assert context_vlan_pool_lock(name="dc1", spec=FIREWALL_CONTEXT) == "fw-context-pools-dc1"
        assert context_vlan_pool_lock(name="dc1", spec=FIREWALL_CONTEXT) != context_vlan_pool_lock(
            name="dc1", spec=LOADBALANCER_CONTEXT
        )


def _client(names: list[str]) -> HfidClient:
    """The bootstrap groups and the ports each device's template brings."""
    client = HfidClient()
    client.seed("CoreStandardGroup", name="ha_domains")
    client.seed("CoreStandardGroup", name="internal-firewalls")
    for name in names:
        client.template_ports[name] = [
            {"name": port.name, "role": port.role, "interface_type": _TYPE} for port in _BL_PORTS
        ]
    return client


def _snapshot(client: HfidClient) -> dict[str, dict[str, Any]]:
    """What is stored, without the ``id`` a second run sends to update what it found."""
    return {
        node_id: {key: value for key, value in node.data.items() if key != "id"}
        for node_id, node in client.store.items()
    }


class TestApplied:
    @pytest.mark.asyncio
    async def test_the_plan_writes_devices_then_domains_then_cables_and_a_rerun_changes_nothing(self) -> None:
        entries = _entries("internal-firewall", quantity=2)
        plan, services = _plan(entries)
        client = _client([_border(plan), *services.firewalls])
        for name in services.firewalls:
            client.template_ports[name] = [
                {"name": port.name, "role": port.role, "interface_type": _TYPE} for port in _FW_PORTS
            ]
        gen: Any = wire_hfid_generator(make_generator_of(PlanGenerator, client), client)
        gen.fabric_name = "dc1"

        result = await gen.apply_plan(plan)

        (domain,) = [n for n in client.store.values() if n.kind == "ManagedFirewallHA"]
        assert [m["id"] for m in domain.data["capabilities"]] == [
            result.ids[Hfid("DcimPhysicalDevice", (name,))] for name in services.firewalls
        ]
        assert domain.data["group_id"] == {"from_pool": {"id": "pool-ha"}}
        cables = sorted(n.data["name"] for n in client.store.values() if n.kind == "DcimCable")
        # the pair's sync cable and the firewalls' two uplinks each to the border-leaf
        assert len(cables) == 5 and sum(c.endswith("-SYNC") for c in cables) == 1
        created = list(client.created_ids)
        snapshot = _snapshot(client)

        await gen.apply_plan(plan)

        assert client.created_ids == created
        assert _snapshot(client) == snapshot

    def test_a_plan_with_a_missing_port_raises_before_the_first_write(self) -> None:
        problems: list[str] = []
        _plan(_entries("internal-firewall", quantity=1), problems=problems, border_ports=[])

        with pytest.raises(GeneratorError, match="firewall_ports=0"):
            raise_problems(problems)


def _port(device: str, name: str, role: str, *, cable: str | None = None) -> MagicMock:
    """A physical interface as ``filters(include=["device", "cable"])`` returns it."""
    port = MagicMock(id=f"{device}/{name}")
    port.name = MagicMock(value=name)
    port.role = MagicMock(value=role)
    port.interface_type = MagicMock(value=_TYPE)
    port.device.peer.name.value = device
    port.cable = MagicMock(id=cable, initialized=cable is not None)
    return port


class TestReads:
    @pytest.mark.asyncio
    async def test_the_live_ports_say_where_each_cable_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        far = MagicMock()
        far.device.peer.name.value = "fw-01"
        ports = [
            _port("bl-01", "e1", "firewall", cable="c1"),
            _port("bl-01", "e2", "firewall"),
            _port("bl-01", "e3", "x", cable="c2"),
        ]
        client = MagicMock()
        client.filters = AsyncMock(return_value=ports)
        monkeypatch.setattr(
            "generators.dcim.cabling_plan.far_end_interfaces", AsyncMock(return_value={"bl-01/e1": far})
        )

        live = await read_chain_ports(client, ["bl-01", "bl-01"])

        assert live == {
            "bl-01": [
                ChainPort("e1", "firewall", _TYPE, "fw-01"),
                ChainPort("e2", "firewall", _TYPE, None),
                # cabled to something that is not a physical port: taken whatever the leg
                ChainPort("e3", "x", _TYPE, ELSEWHERE),
            ]
        }
        assert client.filters.await_args is not None
        assert client.filters.await_args.kwargs["device__name__values"] == ["bl-01"]

    @pytest.mark.asyncio
    async def test_no_device_is_no_read(self) -> None:
        client = MagicMock()
        client.filters = AsyncMock()

        assert await read_chain_ports(client, []) == {}
        client.filters.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_ha_state_is_the_domains_the_first_sync_port_the_cabled_ones_the_cables_and_the_pool(
        self,
    ) -> None:
        domain = MagicMock(id="ha-1")
        domain.name = MagicMock(value="fw-01-fw-02")
        domain.group_id = MagicMock(value=4)
        cable = MagicMock(id="cable-1")
        cable.name = MagicMock(value="CBL-fw-01-fw-02-SYNC")
        pool = MagicMock(id="pool-1")

        async def filters(kind: Any, **kwargs: Any) -> list[Any]:
            kind_name = getattr(kind, "__name__", kind)
            assert kind_name != "DcimPhysicalInterface" or kwargs["role__value"] == "ha"
            return {
                "ManagedFirewallHA": [domain],
                "DcimPhysicalInterface": [
                    _port("fw-01", "ha2", "ha"),
                    _port("fw-01", "ha1", "ha", cable="c9"),
                    _port("fw-02", "ha1", "ha"),
                ],
                "DcimCable": [cable],
                "CoreNumberPool": [pool],
            }[kind_name]

        client = MagicMock()
        client.filters = filters

        reads = await read_ha_reads(
            client, domains={"fw-01-fw-02": "ManagedFirewallHA"}, device_names=["fw-01", "fw-02"], firewall=True
        )

        assert reads == HaReads(
            domains={"fw-01-fw-02": HaDomain("ha-1", 4)},
            sync_ports={"fw-01": "ha1", "fw-02": "ha1"},
            cabled_sync=frozenset({"fw-01"}),
            sync_cables={"CBL-fw-01-fw-02-SYNC": "cable-1"},
            group_pool_id="pool-1",
        )

    @pytest.mark.asyncio
    async def test_a_dc_without_service_entries_reads_nothing(self) -> None:
        client = MagicMock()
        client.filters = AsyncMock()

        reads = await read_service_reads(client, entries=[], names=[], border_names=["bl-01"])

        assert reads.ha == HaReads() and reads.chain_ports == {}
        client.filters.assert_not_awaited()
