"""Unit tests for utils/vrf_demand.py: the one derivation of a device's VRF instances (AC10) and its gatherer."""

from __future__ import annotations

from tests.unit.vrf_instances_helpers import (
    address,
    facts_of,
    raw_context,
    raw_device,
    raw_instance,
    raw_interface,
    raw_mlag,
    raw_row,
    raw_svi,
)
from utils.vrf_demand import (
    VrfDemandInputs,
    carries_vrfs,
    instance_name,
    svi_name,
    vrf_demand,
)


def test_l2_only_segment_needs_no_instance() -> None:
    """A row whose segment has no gateway is pure L2: no namespace, no routed row."""
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, namespace=None)]))
    assert facts.demand == frozenset()
    assert facts.routed_rows == ()
    assert facts.unrouted_row_ids == frozenset({"r1"})


def test_inline_terminated_segment_needs_no_instance() -> None:
    """terminate_inline keeps its gateway but the fabric does not route it."""
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, inline=True)]))
    assert facts.demand == frozenset()
    assert facts.desired_svis() == {}


def test_routed_row_needs_its_gateway_namespace() -> None:
    """A routed row demands its namespace and an SVI named Vlan<id>."""
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "PROD")]))
    assert facts.demand == frozenset({"PROD"})
    assert list(facts.desired_svis()) == ["Vlan100"]


def test_internet_exit_address_on_border_gateway_edge_is_included() -> None:
    """An internet-namespace address on an edge's own interface demands the INTERNET instance."""
    iface = raw_interface("i1", "Ethernet1", "wan", address("203.0.113.2/31", "INTERNET", "internet"))
    facts = facts_of(raw_device("d1", "edge-1", role="edge", interfaces=[iface]))
    assert facts.demand == frozenset({"INTERNET"})


def test_default_namespace_is_never_demanded() -> None:
    """The underlay is the global table: rows, SVIs and addresses in default demand nothing."""
    iface = raw_interface("i1", "Ethernet1", "fabric", address("10.9.0.1/31", "default"))
    svi = raw_svi("s1", 100, "r1")
    device = raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "default")], interfaces=[iface, svi])
    assert facts_of(device).demand == frozenset()


def test_svi_vrf_counts_as_demand() -> None:
    """An instance is never dropped while an SVI still routes in it."""
    svi = raw_svi("s1", 100, "r1", vrf_id="v1", vrf_namespace="NON-PROD")
    assert facts_of(raw_device("d1", "leaf-1", interfaces=[svi])).demand == frozenset({"NON-PROD"})


def test_leg_namespace_of_context_on_service_port_is_included() -> None:
    """A firewall context tagged on a role-firewall port brings its transit leg namespace."""
    context = raw_context("ManagedFirewallContext", 3000, ("firewall", "PROD", "prod"))
    port = raw_interface("i1", "Ethernet5", "firewall", capabilities=[context])
    assert facts_of(raw_device("d1", "bl-1", role="border-leaf", interfaces=[port])).demand == frozenset({"PROD"})


def test_leg_namespace_from_port_of_another_role_is_ignored() -> None:
    """A context tagged on a port that is not its service port gives no demand."""
    context = raw_context("ManagedFirewallContext", 3000, ("firewall", "PROD", "prod"))
    port = raw_interface("i1", "Ethernet5", "load-balancer", capabilities=[context])
    assert facts_of(raw_device("d1", "bl-1", role="border-leaf", interfaces=[port])).demand == frozenset()


def test_leg_of_a_non_member_role_or_default_namespace_is_ignored() -> None:
    """Only legs held by the HA members (firewall / load-balancer role) in a slotted namespace count."""
    context = raw_context("ManagedFirewallContext", 3000, ("leaf", "PROD", "prod"), ("firewall", "default", "prod"))
    port = raw_interface("i1", "Ethernet5", "firewall", capabilities=[context])
    assert facts_of(raw_device("d1", "bl-1", role="border-leaf", interfaces=[port])).demand == frozenset()


def test_mlag_domain_rows_are_used_for_a_paired_device() -> None:
    """A paired device reads its MLAG's rows, not its own."""
    mlag = raw_mlag("m1", raw_row("r-mlag", 200, "NON-PROD"))
    device = raw_device("d1", "leaf-1", rows=[raw_row("r-own", 100, "PROD")], capabilities=[mlag])
    facts = facts_of(device)
    assert facts.demand == frozenset({"NON-PROD"})
    assert facts.domain_row_ids == frozenset({"r-mlag"})


def test_own_rows_are_used_for_a_standalone_device() -> None:
    """A device with no MLAG is its own domain."""
    vrf = raw_instance("v1", "leaf-1", "PROD")
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r-own", 100, "PROD")], capabilities=[vrf]))
    assert facts.domain_row_ids == frozenset({"r-own"})
    assert [instance.namespace for instance in facts.instances] == ["PROD"]


def test_party_and_excluded_devices_get_no_demand() -> None:
    """Spines, firewalls, provider stubs, party deployments and other kinds carry no instance."""
    rows = [raw_row("r1", 100, "PROD")]
    cases = [
        raw_device("d1", "spine-1", role="spine", rows=rows),
        raw_device("d2", "fw-1", role="firewall", rows=rows),
        raw_device("d3", "stub", role="edge", rows=rows, groups=("provider_stubs",)),
        raw_device("d4", "pe", role="edge", rows=rows, deployment_kind="TopologyExternalProviders"),
        raw_device("d5", "rtr", role="edge", rows=rows, deployment_kind="TopologyCloudRegion"),
        raw_device("d6", "ctl", role="leaf", rows=rows, typename="DcimVirtualDevice"),
    ]
    for device in cases:
        facts = facts_of(device)
        assert facts.demand == frozenset(), device["name"]
        assert facts.desired_svis() == {}, device["name"]


def test_hosted_cloud_appliance_can_carry_vrfs() -> None:
    """CloudNetworkAppliance is the one non-physical kind that carries instances."""
    assert carries_vrfs("CloudNetworkAppliance", "edge")
    assert not carries_vrfs("DcimPhysicalDevice", "spine")


def test_vrf_demand_pure_function_unions_sources_and_drops_default() -> None:
    """vrf_demand unions every source and removes default and empty names."""
    inputs = VrfDemandInputs(
        kind="DcimPhysicalDevice",
        role="leaf",
        row_namespaces=frozenset({"PROD"}),
        svi_namespaces=frozenset({"default"}),
        address_namespaces=frozenset({"INTERNET", ""}),
        leg_namespaces=frozenset({"NON-PROD"}),
    )
    assert vrf_demand(inputs) == frozenset({"PROD", "INTERNET", "NON-PROD"})


def test_instance_and_svi_names() -> None:
    """The derived names."""
    assert instance_name("leaf-1", "NON-PROD") == "leaf-1-vrf-non-prod"
    assert svi_name(321) == "Vlan321"
