"""Unit tests for utils/service_contexts.py: the legs and border ports of a service context derived from the devices
(the nested shape of fragments/device_services.gql)."""

from __future__ import annotations

from tests.unit.service_contexts_helpers import LB, make_context, make_far, make_iface, make_member, make_switch
from utils.service_contexts import (
    DEVICE_SERVICES_ROOT,
    ContextLeg,
    attach_device_services,
    border_contexts,
    context_border_ports,
    context_legs,
    contexts_with_legs,
    device_services,
    exchange_legs,
    interface_contexts,
    leg_contexts,
    leg_interfaces,
    member_border_ports,
)


def test_physical_member_has_one_leg_per_existing_slot_sub_interface() -> None:
    """Context VLAN 3000: ``ethernet1/1.3000`` is the PROD leg, ``.3400`` the INTERNET one; a missing slot is no leg."""
    member = make_member(
        "fw-1", [make_context("ctx", 3000)], subs=("ethernet1/1.3000", "ethernet1/1.3400", "ethernet1/1.3999")
    )
    assert context_legs(member) == [
        ContextLeg("ctx", "ethernet1/1.3000", "PROD"),
        ContextLeg("ctx", "ethernet1/1.3400", "INTERNET"),
    ]


def test_two_contexts_on_one_member_keep_their_own_legs() -> None:
    """A shared context (3000) and a dedicated one (3001) on the same pair: each leg belongs to its context."""
    member = make_member(
        "fw-1",
        [make_context("shared", 3000), make_context("dedicated", 3001)],
        subs=("ethernet1/1.3000", "ethernet1/1.3001"),
    )
    assert [(leg.context_id, leg.interface_name) for leg in context_legs(member)] == [
        ("dedicated", "ethernet1/1.3001"),
        ("shared", "ethernet1/1.3000"),
    ]


def test_non_prod_and_management_slots_map_to_their_zones() -> None:
    """Slots add ``TRANSIT_VLAN_BAND`` each: NON-PROD +200, MANAGEMENT +600."""
    member = make_member("fw-1", [make_context("ctx", 3002)], subs=("ethernet1/1.3202", "ethernet1/1.3602"))
    assert [leg.zone for leg in context_legs(member)] == ["NONPROD", "MANAGEMENT"]


def test_virtual_member_legs_sit_on_its_nic() -> None:
    """A hosted firewall has only virtual interfaces: its uplink is its NIC, and the legs are sub-interfaces of it."""
    member = make_member("vfw", [make_context("ctx", 3001)], virtual=True, uplinks=("eth1/1",), subs=("eth1/1.3001",))
    assert context_legs(member) == [ContextLeg("ctx", "eth1/1.3001", "PROD")]


def test_a_sub_interface_of_the_uplink_with_the_transit_vlan_is_a_leg_whatever_its_name() -> None:
    """The tag is the field: a non-conventional name over ``transit_vlan(3000)`` on the uplink is the PROD leg."""
    member = make_member("fw-1", [make_context("ctx", 3000)], subs=(("uplink-prod", 3000, "ethernet1/1"),))
    assert context_legs(member) == [ContextLeg("ctx", "uplink-prod", "PROD")]


def test_a_conventionally_named_sub_interface_with_another_tag_or_parent_is_no_leg() -> None:
    """``ethernet1/1.3000`` tagged 3999, or tagged 3000 but riding on another port, or untagged, is not a leg."""
    member = make_member(
        "fw-1",
        [make_context("ctx", 3000)],
        uplinks=("ethernet1/1", "ethernet1/2"),
        subs=(
            ("ethernet1/1.3000", 3999, "ethernet1/1"),
            ("ethernet1/2.3000", 3000, "ethernet1/2"),
            ("ethernet1/1.3400", None, "ethernet1/1"),
        ),
    )
    assert context_legs(member) == []


def test_member_without_an_uplink_has_no_legs() -> None:
    """No ``role: uplink`` interface, no trunk, no leg (the generator logs it and skips the member)."""
    member = make_member("fw-1", [make_context("ctx", 3000)], uplinks=(), subs=("ethernet1/1.3000",))
    assert context_legs(member) == []


def test_context_without_vlan_or_of_another_role_has_no_legs() -> None:
    """A context with no VLAN yet, and a load-balancer context on a firewall member, are not its legs."""
    member = make_member(
        "fw-1", [make_context("novlan", None), make_context("lb", 3100, LB)], subs=("ethernet1/1.3100",)
    )
    assert context_legs(member) == []


def test_load_balancer_member_legs() -> None:
    """A load-balancer member holds the legs of its load-balancer context (VLAN band 3100)."""
    member = make_member("lb-1", [make_context("lbctx", 3100, LB)], role="load-balancer", subs=("ethernet1/1.3100",))
    assert context_legs(member) == [ContextLeg("lbctx", "ethernet1/1.3100", "PROD")]


def test_first_uplink_by_name_is_the_trunk() -> None:
    """With two physical uplinks the lowest name is the trunk, as the generator picks it."""
    member = make_member(
        "fw-1", [make_context("ctx", 3000)], uplinks=("ethernet1/2", "ethernet1/1"), subs=("ethernet1/2.3000",)
    )
    assert context_legs(member) == []
    member = make_member(
        "fw-1", [make_context("ctx", 3000)], uplinks=("ethernet1/2", "ethernet1/1"), subs=("ethernet1/1.3000",)
    )
    assert [leg.interface_name for leg in context_legs(member)] == ["ethernet1/1.3000"]


def test_border_port_faces_the_uplink_of_a_member() -> None:
    """The port cabled to a member's uplink is tagged with each context the member belongs to."""
    member = make_member("fw-1", [make_context("b", 3001), make_context("a", 3000)])
    far = make_far(member, make_iface("fw-1:ethernet1/1", "ethernet1/1", "uplink"))
    assert context_border_ports(make_switch({"Ethernet1/25": ("firewall", [far])})) == {"Ethernet1/25": ["a", "b"]}


def test_border_port_of_another_role_is_not_tagged() -> None:
    """A load-balancer role port facing a firewall member is not a firewall context port."""
    member = make_member("fw-1", [make_context("ctx", 3000)])
    far = make_far(member, make_iface("fw-1:ethernet1/1", "ethernet1/1", "uplink"))
    assert context_border_ports(make_switch({"Ethernet1/30": ("load-balancer", [far])})) == {}


def test_border_port_of_load_balancer_role_faces_a_load_balancer_member() -> None:
    """Load-balancer contexts are tagged on load-balancer role ports, and only of load-balancer members."""
    member = make_member("lb-1", [make_context("lbctx", 3100, LB)], role="load-balancer")
    far = make_far(member, make_iface("lb-1:ethernet1/1", "ethernet1/1", "uplink"))
    assert context_border_ports(make_switch({"Ethernet1/30": ("load-balancer", [far])})) == {"Ethernet1/30": ["lbctx"]}


def test_far_end_that_is_not_the_first_uplink_is_not_a_member_port() -> None:
    """Only the member's first uplink carries the legs: a cable to its second uplink tags nothing."""
    member = make_member("fw-1", [make_context("ctx", 3000)], uplinks=("ethernet1/1", "ethernet1/2"))
    far = make_far(member, make_iface("fw-1:ethernet1/2", "ethernet1/2", "uplink"))
    assert context_border_ports(make_switch({"Ethernet1/25": ("firewall", [far])})) == {}


def test_far_end_that_is_not_an_uplink_is_not_a_member_port() -> None:
    """A cable to a management port of a firewall is not a context port."""
    member = make_member("fw-1", [make_context("ctx", 3000)])
    far = make_far(member, make_iface("fw-1:mgmt", "mgmt", "management"))
    assert context_border_ports(make_switch({"Ethernet1/25": ("firewall", [far])})) == {}


def test_uncabled_and_patch_panel_ports_are_skipped() -> None:
    """A member with no cabled uplink leaves its border port untagged; a cable ending on a panel is not followed."""
    switch = make_switch({"Ethernet1/25": ("firewall", [{}])})
    switch["service_ports"].append(make_iface("sw:Ethernet1/26", "Ethernet1/26", "firewall"))
    assert context_border_ports(switch) == {}


def test_virtual_member_is_tagged_through_its_host_port() -> None:
    """A hosted firewall has no cable: its NIC is pinned to a host port, and the border port faces that host port."""
    hosted = make_member("vfw", [make_context("ctx", 3001)], virtual=True, uplinks=("eth1/1",))
    nic = make_far(hosted, make_iface("vfw:eth1/1", "eth1/1", "uplink", "DcimVirtualInterface"))
    nic["typename"] = "DcimVirtualInterface"
    host_port = {
        "typename": "DcimPhysicalInterface",
        "id": "host:p1",
        "name": "p1",
        "role": "uplink",
        "device": {"id": "host", "name": "host", "role": "server", "shared_capabilities": [], "interfaces": []},
        "sub_interfaces": [nic],
    }
    assert context_border_ports(make_switch({"Ethernet1/25": ("firewall", [host_port])})) == {"Ethernet1/25": ["ctx"]}


def test_unpinned_nic_is_not_a_member_port() -> None:
    """A NIC that is not pinned to the host port is not in its ``sub_interfaces``: the border port is untagged."""
    host_port = {
        "typename": "DcimPhysicalInterface",
        "id": "host:p1",
        "name": "p1",
        "role": "uplink",
        "device": {"id": "host", "name": "host", "role": "server", "shared_capabilities": [], "interfaces": []},
        "sub_interfaces": [],
    }
    assert context_border_ports(make_switch({"Ethernet1/25": ("firewall", [host_port])})) == {}


def _leg_iface(member: dict, name: str, address: str = "100.66.0.5/29") -> None:
    """Give the sub-interface ``name`` of ``member`` an address and a VRF."""
    leg = next(i for i in member["interfaces"] if i["name"] == name)
    leg["ip_address"] = {"address": address}
    leg["vrf"] = {"security_zone": {"name": "PROD", "vrf_type": "prod"}}


def test_leg_interfaces_carry_their_member_as_device() -> None:
    """A leg is the member's own interface dict plus the ``device`` (name, role) the transforms filter on."""
    member = make_member("fw-1", [make_context("ctx", 3000)], subs=("ethernet1/1.3000",))
    _leg_iface(member, "ethernet1/1.3000")
    (leg,) = leg_interfaces(member)["ctx"]
    assert leg["name"] == "ethernet1/1.3000"
    assert leg["ip_address"] == {"address": "100.66.0.5/29"}
    assert leg["device"] == {"id": "dev-fw-1", "name": "fw-1", "role": "internal-firewall"}


def test_leg_contexts_names_the_context_of_each_leg() -> None:
    member = make_member("fw-1", [make_context("a", 3000), make_context("b", 3001)], subs=("ethernet1/1.3001",))
    assert {name: ctx["id"] for name, ctx in leg_contexts(member).items()} == {"ethernet1/1.3001": "b"}


def test_contexts_with_legs_lists_a_context_once_with_the_legs_of_every_member() -> None:
    """Both members of a pair list the context; it comes back once, with both legs and its served footprints."""
    served = (("fp-1", "p"),)
    members = [
        make_member(name, [make_context("ctx", 3000, served=served)], subs=("ethernet1/1.3000",))
        for name in ("fw-1", "fw-2")
    ]
    for member in members:
        _leg_iface(member, "ethernet1/1.3000")
    (context,) = contexts_with_legs(members)
    assert context["id"] == "ctx"
    assert [leg["device"]["name"] for leg in context["legs"]] == ["fw-1", "fw-2"]
    assert context["served_deployments"] == [{"id": "fp-1", "environment": "p"}]


def _in_vrf(member: dict, name: str, vrf_id: str) -> None:
    """Put the sub-interface ``name`` of ``member`` in the VRF instance ``vrf_id`` (its ManagedVRF capability)."""
    leg = next(i for i in member["interfaces"] if i["name"] == name)
    leg["zone_capabilities"] = [{"typename": "ManagedVRF", "id": vrf_id}]


def test_exchange_legs_are_the_context_legs_in_its_two_vrfs() -> None:
    """A routed exchange names its context and two VRFs: its legs on a member are the context's legs in either VRF, by
    name; a leg of the context in a third VRF, a leg of another context or an interface that is no leg is not one."""
    member = make_member(
        "fw-1",
        [make_context("ctx", 3000), make_context("other", 3001)],
        subs=("ethernet1/1.3000", "ethernet1/1.3400", "ethernet1/1.3600", "ethernet1/1.3001", "ethernet1/1.web"),
    )
    for name, vrf in (
        ("ethernet1/1.3000", "vrf-prod"),
        ("ethernet1/1.3400", "vrf-internet"),
        ("ethernet1/1.3600", "vrf-management"),
        ("ethernet1/1.3001", "vrf-prod"),
        ("ethernet1/1.web", "vrf-internet"),
    ):
        _in_vrf(member, name, vrf)
    exchange = {"id": "x", "vrf_a": {"id": "vrf-prod"}, "vrf_z": {"id": "vrf-internet"}}
    assert [leg["name"] for leg in exchange_legs(member, "ctx", exchange)] == ["ethernet1/1.3000", "ethernet1/1.3400"]
    assert [leg["name"] for leg in exchange_legs(member, "other", exchange)] == ["ethernet1/1.3001"]
    assert exchange_legs(member, "ctx", {"id": "x", "vrf_a": None, "vrf_z": None}) == []


def test_border_contexts_follow_the_cables_of_the_service_ports() -> None:
    """The contexts behind the ports of a switch, once each, with the legs of the members behind them."""
    one = make_member("fw-1", [make_context("ctx", 3000)], subs=("ethernet1/1.3000",))
    two = make_member("fw-2", [make_context("ctx", 3000)], subs=("ethernet1/1.3000",))
    _leg_iface(one, "ethernet1/1.3000")
    _leg_iface(two, "ethernet1/1.3000", "100.66.0.6/29")
    ports = {
        "Ethernet1/25": ("firewall", [make_far(one, make_iface("fw-1:ethernet1/1", "ethernet1/1", "uplink"))]),
        "Ethernet1/26": ("firewall", [make_far(two, make_iface("fw-2:ethernet1/1", "ethernet1/1", "uplink"))]),
    }
    (context,) = border_contexts(make_switch(ports))
    assert context["id"] == "ctx"
    assert sorted(leg["device"]["name"] for leg in context["legs"]) == ["fw-1", "fw-2"]


def test_border_contexts_of_a_switch_without_service_ports_is_empty() -> None:
    assert border_contexts({"name": "leaf"}) == []


def test_member_border_ports_are_the_far_end_of_the_first_uplinks_cable() -> None:
    """The member's side of ``port_members``: a physical uplink's cable far end, never the uplink itself."""
    member = make_member("fw-1", [make_context("ctx", 3000)])
    uplink = member["interfaces"][0]
    border = {"id": "border:p1", "typename": "DcimPhysicalInterface", "name": "p1", "role": "firewall"}
    uplink["cable"] = {"id": "c", "endpoints": [{"id": uplink["id"]}, border]}
    assert member_border_ports(member) == [border]


def test_a_hosted_member_reaches_its_border_port_through_the_host_port() -> None:
    """A virtual uplink has no cable: the far end is that of the host port it is pinned to."""
    member = make_member("vfw", [make_context("ctx", 3001)], virtual=True, uplinks=("eth1/1",))
    border = {"id": "border:p1", "typename": "DcimPhysicalInterface", "name": "p1", "role": "firewall"}
    host_port = {"id": "host:p1", "cable": {"id": "c", "endpoints": [{"id": "host:p1"}, border]}}
    member["interfaces"][0]["parent_interface"] = host_port
    assert member_border_ports(member) == [border]


def test_a_member_without_an_uplink_or_a_cable_has_no_border_port() -> None:
    assert member_border_ports(make_member("fw-1", [make_context("ctx", 3000)], uplinks=())) == []
    assert member_border_ports(make_member("fw-1", [make_context("ctx", 3000)])) == []


def test_interface_contexts_tag_ports_and_legs() -> None:
    """A service port carries the contexts behind it; a leg sub-interface of the device is a leg of its context."""
    member = make_member("fw-1", [make_context("ctx", 3000)], subs=("ethernet1/1.3000",))
    far = make_far(member, make_iface("fw-1:ethernet1/1", "ethernet1/1", "uplink"))
    switch = make_switch({"Ethernet1/25": ("firewall", [far])})
    assert {name: [c["id"] for c in ctxs] for name, ctxs in interface_contexts({}, switch).items()} == {
        "Ethernet1/25": ["ctx"]
    }
    assert {name: [c["id"] for c in ctxs] for name, ctxs in interface_contexts(member, member).items()} == {
        "ethernet1/1.3000": ["ctx"]
    }


def test_device_services_is_the_services_root_or_the_device_itself() -> None:
    device: dict = {"name": "fw"}
    assert device_services(device) is device
    attach_device_services(device, {})
    assert device_services(device) is device
    attach_device_services(device, {DEVICE_SERVICES_ROOT: [{"name": "fw", "service_ports": []}]})
    assert device_services(device) == {"name": "fw", "service_ports": []}


def test_a_context_behind_two_ports_is_listed_once() -> None:
    """Both load-balancer members' ports lead to the same context: one entry."""
    contexts = [make_context("lbctx", 3100, LB)]
    ports = {}
    for name, port in (("lb-1", "Ethernet21"), ("lb-2", "Ethernet22")):
        member = make_member(name, contexts, role="load-balancer", subs=("ethernet1/1.3100",))
        ports[port] = ("load-balancer", [make_far(member, make_iface(f"{name}:ethernet1/1", "ethernet1/1", "uplink"))])
    assert [c["id"] for c in border_contexts(make_switch(ports))] == ["lbctx"]
