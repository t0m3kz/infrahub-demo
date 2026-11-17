"""The plans of a session circuit as data (generators/connectivity/virtual_circuit.py) and the BGP builders they use
(generators/capabilities/bgp.py): what is written, owned, shared, and by which reference."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.capabilities.bgp import (
    address_family_lock_key,
    dci_key_name,
    plan_address_family,
    read_address_families,
    read_password_id,
)
from generators.connectivity.virtual_circuit import SessionEnd, SessionPlan, plan_addressing, plan_peerings
from generators.plan import EdgesSpec, NodeRef, NodeSpec, PrefixAllocationSpec, TopologyPlan
from tests.unit.simulators.circuit_fakes import (
    FakeClient,
    handoff_payload,
    interconnect_payload,
    session_generator,
)
from utils.data_cleaning import clean_data


def _session(payload: dict[str, Any]) -> SessionPlan:
    """The session plan the kind's planner builds for a payload (pure: no client call)."""
    gen: Any = session_generator(FakeClient())
    (circuit,) = clean_data(payload)["TopologySessionCircuit"]
    ends = [end for port in circuit["capabilities"] if (end := SessionEnd.parse(port)) is not None]
    return gen._PLANNERS[circuit["circuit_type"]](gen, circuit, circuit["name"], ends)


def _ports(plan: SessionPlan, *, vrf_peers: list[Any] | None = None) -> dict[str, Any]:
    """The end interfaces as read with their capabilities."""
    return {
        end.port_id: SimpleNamespace(id=end.port_id, capabilities=SimpleNamespace(peers=vrf_peers or []))
        for end in (plan.first, plan.second)
    }


def _of_kind(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    """The nodes of ``kind`` the plan saves."""
    return [spec for spec in plan.of(NodeSpec) if spec.kind == kind]


class TestAddressing:
    def test_the_link_prefix_is_keyed_by_the_circuit_and_owned_with_its_addresses(self) -> None:
        plan = _session(interconnect_payload())

        addressing = plan_addressing("vc-1", "VC-DCI", "active", plan, _ports(plan), {})

        (prefix,) = addressing.of(PrefixAllocationSpec)
        assert (prefix.identifier, prefix.pool_name, prefix.prefix_length) == ("vc-p2p__vc-1", plan.pool_name, 127)
        assert prefix.track_prefix and prefix.address_ownership == "owned" and prefix.is_pool
        assert prefix.address_offsets == (0, 1)
        assert addressing.root_id == "vc-1"

    def test_each_end_interface_is_updated_by_id_with_its_address_and_never_tracked(self) -> None:
        plan = _session(interconnect_payload())

        addressing = plan_addressing("vc-1", "VC-DCI", "active", plan, _ports(plan), {})

        (prefix,) = addressing.of(PrefixAllocationSpec)
        updates = {spec.data["id"]: spec for spec in addressing.of(NodeSpec)}
        assert set(updates) == {plan.first.port_id, plan.second.port_id}
        assert updates[plan.first.port_id].data["ip_address"] == NodeRef(prefix.address_key(0))
        assert updates[plan.second.port_id].data["ip_address"] == NodeRef(prefix.address_key(1))
        assert {spec.data["mtu"] for spec in updates.values()} == {9214}
        assert {spec.data["status"] for spec in updates.values()} == {"active"}
        assert not any(spec.tracked for spec in updates.values())

    def test_a_handoff_leaves_the_mtu_alone_and_the_interface_status_follows_the_circuit(self) -> None:
        plan = _session(handoff_payload())

        addressing = plan_addressing("vc-1", "VC-C001", "provisioning", plan, _ports(plan), {})

        updates = addressing.of(NodeSpec)
        assert all("mtu" not in spec.data for spec in updates)
        assert {spec.data["status"] for spec in updates} == {"provisioning"}

    def test_the_end_interfaces_join_the_instance_of_the_namespace(self) -> None:
        plan = _session(handoff_payload())
        local = {(end.device_id, "PROD"): f"vrf-{end.device_name}" for end in (plan.first, plan.second)}

        addressing = plan_addressing("vc-1", "VC-C001", "active", plan, _ports(plan), local)

        assert sorted((edge.node, edge.peers) for edge in addressing.of(EdgesSpec)) == sorted(
            (end.port_id, (f"vrf-{end.device_name}",)) for end in (plan.first, plan.second)
        )


class TestPeerings:
    def test_a_dci_creates_its_key_once_shared_and_points_both_peerings_at_it(self) -> None:
        plan = _session(interconnect_payload())

        peerings = plan_peerings("VC-DCI", "active", plan, {})

        (key,) = _of_kind(peerings, "RoutingPassword")
        assert key.data["name"] == dci_key_name("VC-DCI-DC10-FR2") and not key.tracked and "password" in key.create_only
        sessions = _of_kind(peerings, "ManagedBGPPeering")
        assert [spec.data["name"] for spec in sessions] == ["VC-DCI-underlay", "VC-DCI-evpn"]
        assert all(spec.tracked and spec.data["password"] == {"id": NodeRef(key.key)} for spec in sessions)

    def test_an_existing_key_is_referenced_and_never_written(self) -> None:
        plan = _session(interconnect_payload())

        peerings = plan_peerings("VC-DCI", "active", plan, {}, key_id="key-1")

        assert not _of_kind(peerings, "RoutingPassword")
        assert {spec.data["password"]["id"] for spec in _of_kind(peerings, "ManagedBGPPeering")} == {"key-1"}

    def test_a_handoff_uses_the_password_of_the_circuit_or_none(self) -> None:
        plan = _session(handoff_payload(password="pw-1"))
        no_password = _session(handoff_payload())

        (peering,) = _of_kind(plan_peerings("VC-C001", "active", plan, {}), "ManagedBGPPeering")
        (bare,) = _of_kind(plan_peerings("VC-C001", "active", no_password, {}), "ManagedBGPPeering")

        assert peering.data["password"] == {"id": "pw-1"}
        assert "password" not in bare.data

    def test_a_missing_address_family_is_created_shared_and_an_existing_one_is_referenced(self) -> None:
        plan = _session(interconnect_payload())

        created = plan_peerings("VC-DCI", "active", plan, {})
        existing = plan_peerings("VC-DCI", "active", plan, {("ipv6", "unicast"): "af-6", ("l2vpn", "evpn"): "af-e"})

        families = _of_kind(created, "RoutingBGPAddressFamily")
        assert sorted(spec.data["safi"] for spec in families) == ["evpn", "unicast"]
        assert not any(spec.tracked for spec in families)
        assert not _of_kind(existing, "RoutingBGPAddressFamily")

    def test_a_circuit_that_is_not_active_has_provisioning_peerings(self) -> None:
        plan = _session(handoff_payload())

        (peering,) = _of_kind(plan_peerings("VC-C001", "maintenance", plan, {}), "ManagedBGPPeering")

        assert peering.data["status"] == "provisioning"


class TestAddressFamilies:
    def test_the_same_missing_family_is_one_node_of_the_plan(self) -> None:
        plan = TopologyPlan(root_id=None)

        first = plan_address_family(plan, ("ipv4", "unicast"), {})
        second = plan_address_family(plan, ("ipv4", "unicast"), {})

        assert first == second == NodeRef("address-family:ipv4/unicast")
        assert len(plan.of(NodeSpec)) == 1

    @pytest.mark.asyncio
    async def test_the_families_that_exist_are_read_in_one_query(self) -> None:
        client = FakeClient()

        found = await read_address_families(client, {("ipv4", "unicast"), ("l2vpn", "evpn"), ("ipv6", "evpn")})

        assert found == {("ipv4", "unicast"): "af-ipv4-unicast", ("l2vpn", "evpn"): "af-l2vpn-evpn"}
        assert [name for name, _ in client.filter_calls] == ["RoutingBGPAddressFamily"]

    @pytest.mark.asyncio
    async def test_a_password_is_found_by_name(self) -> None:
        client = FakeClient(key=True)

        assert await read_password_id(client, "VC-DCI-DC10-FR2-dci-key") == "key-existing"
        assert await read_password_id(client, "nope") is None

    def test_the_lock_of_a_family_is_its_own(self) -> None:
        assert address_family_lock_key("l2vpn", "evpn") == "bgp-af-l2vpn-evpn"
