"""CheckProviderDecommission: PC14 (a decommissioned connection is empty) and PC15 (orphan sweep).

Payload builders: tests/unit/provider_connections_check_helpers.py (``dc_*``).
"""

from __future__ import annotations

from typing import Any

from tests.unit.provider_connections_check_helpers import (
    dc_child,
    dc_connection,
    dc_interface,
    dc_peering,
    dc_physical,
    dc_pool,
    dc_session,
    run_decommission,
)


def test_clean_decommissioned_connection_has_no_finding() -> None:
    """A decommissioned connection with no session, a decommissioned adopted carrier and nothing left is silent."""
    errors, infos = run_decommission(
        connections=[dc_connection(carriers=[("PC-AWS-FR2-XC", "decommissioned")])],
        sessions=[dc_session("other", connection_name="PC-OTHER")],
    )
    assert errors == []
    assert infos == []


def test_decommissioned_connection_with_session_is_pc14_error() -> None:
    """Any session circuit left on a decommissioned connection is an error naming it."""
    errors, _ = run_decommission(
        connections=[dc_connection()], sessions=[dc_session("PC-AWS-FR2-L1"), dc_session("PC-AWS-FR2-L2")]
    )
    assert len(errors) == 1
    assert "PC14" in errors[0]
    assert "PC-AWS-FR2-L1" in errors[0] and "PC-AWS-FR2-L2" in errors[0]


def test_live_connection_session_is_not_pc14() -> None:
    """PC14 judges decommissioned connections only."""
    errors, infos = run_decommission(connections=[dc_connection(status="active")], sessions=[dc_session("S")])
    assert errors == []
    assert infos == []


def test_live_generated_carrier_still_listed_is_pc14_warning() -> None:
    """The generated carrier <connection>-XC left live is a warning; a carrier of another name is a person's."""
    _, infos = run_decommission(
        connections=[dc_connection(carriers=[("PC-AWS-FR2-XC", "provisioning"), ("XC-FR2-EUC1", "active")])]
    )
    assert len(infos) == 1
    assert infos[0].startswith("WARNING: PC14") and "PC-AWS-FR2-XC" in infos[0]


def test_orphan_near_subinterface_is_pc15_warning() -> None:
    """A sub-interface on a cloud-role port that ends no session circuit is reported."""
    errors, infos = run_decommission(interfaces=[dc_interface("i1", "Ethernet1/13.3101")])
    assert errors == []
    assert len(infos) == 1 and "PC15" in infos[0] and "Ethernet1/13.3101" in infos[0]


def test_session_end_subinterface_is_not_reported() -> None:
    """A sub-interface that ends a session circuit is live."""
    _, infos = run_decommission(
        interfaces=[dc_interface("i1", "Ethernet1/13.3101")], sessions=[dc_session("S", ends=("i1",))]
    )
    assert infos == []


def test_shared_stub_interface_is_not_reported() -> None:
    """A sub-interface of a stub device (plain device, parent not a provider-edge role) is shared."""
    _, infos = run_decommission(
        interfaces=[
            dc_interface(
                "i1", "et-0/0/0.3101", device="EXT-AWS-DX-FR2", device_role="provider", parent=("et-0/0/0", None)
            )
        ]
    )
    assert infos == []


def test_orphan_service_edge_subinterface_tunnel_and_nic() -> None:
    """On a provider service-edge appliance a sub-interface, a Tunnel and a bare vnic- NIC are reported."""
    kwargs: dict[str, Any] = {"device": "ne-1", "device_kind": "CloudNetworkAppliance", "device_role": "provider"}
    _, infos = run_decommission(
        interfaces=[
            dc_interface("i1", "vnic-PC1.3101", parent=("vnic-PC1", None), **kwargs),
            dc_interface("i2", "Tunnel1", parent=None, **kwargs),
            dc_interface("i3", "vnic-PC1", parent=None, **kwargs),
            dc_interface("i4", "vnic-PC2", parent=None, capabilities=("TopologyCarrierCircuit",), **kwargs),
        ]
    )
    assert len(infos) == 3
    assert not any("vnic-PC2" in message for message in infos)


def test_non_leg_names_are_not_reported() -> None:
    """A Vlan SVI or a Loopback on a cloud-role port's device is no leg interface."""
    _, infos = run_decommission(interfaces=[dc_interface("i1", "Vlan100"), dc_interface("i2", "Loopback0")])
    assert infos == []


def test_link_prefix_on_no_interface_is_reported() -> None:
    """A /31 of a Handoff pool whose addresses sit on no interface is reported once, with its addresses."""
    _, infos = run_decommission(
        pools=[dc_pool([dc_child("10.255.128.0/31", ("a1", "a2")), dc_child("10.255.128.2/31")])],
        interfaces=[dc_physical("p1")],
    )
    assert len(infos) == 2
    assert all("PC15" in message and "link prefix" in message for message in infos)


def test_live_link_prefix_is_not_reported() -> None:
    """A /31 whose addresses are held by interfaces is live."""
    _, infos = run_decommission(
        pools=[dc_pool([dc_child("10.255.128.0/31", ("a1", "a2"))])],
        interfaces=[dc_physical("p1", address="a1"), dc_interface("i1", "Ethernet1/1.1", address="a2")],
        sessions=[dc_session("S", ends=("p1", "i1"))],
    )
    assert infos == []


def test_half_held_prefix_reports_the_loose_address() -> None:
    """One end still addressed, the other address on no interface: the address is reported, not the prefix."""
    _, infos = run_decommission(
        pools=[dc_pool([dc_child("10.255.128.0/31", ("a1", "a2"))])], interfaces=[dc_physical("p1", address="a1")]
    )
    assert len(infos) == 1 and "address a2" in infos[0]


def test_other_pools_are_not_swept() -> None:
    """Only Handoff-* pools are link pools."""
    _, infos = run_decommission(pools=[dc_pool([dc_child("10.9.0.0/24")], name="Tenant-PROD-IPv4")])
    assert infos == []


def test_orphan_peering_without_interfaces_is_reported() -> None:
    """A -bgp peering whose session is gone and that rides on nothing is reported."""
    _, infos = run_decommission(peerings=[dc_peering("PC1-L1-bgp")])
    assert len(infos) == 1 and "PC1-L1-bgp" in infos[0]


def test_peering_of_a_live_session_is_not_reported() -> None:
    """The peering name minus its suffix is a session circuit."""
    _, infos = run_decommission(peerings=[dc_peering("PC1-L1-bgp")], sessions=[dc_session("PC1-L1")])
    assert infos == []


def test_peering_on_orphan_subinterfaces_is_reported_and_on_other_interfaces_is_not() -> None:
    """A peering is orphan when all its interfaces are orphan legs; one fabric port keeps it alive."""
    interfaces = [dc_interface("i1", "Ethernet1/13.3101"), dc_interface("i2", "Ethernet1/1", parent=None)]
    _, infos = run_decommission(
        interfaces=interfaces,
        peerings=[dc_peering("PC1-L1-bgp", ends=("i1",)), dc_peering("fab-1-bgp", ends=("i1", "i2"))],
    )
    assert sum("BGP peering" in message for message in infos) == 1
    assert any("'PC1-L1-bgp'" in message for message in infos)


def test_fabric_and_other_role_peerings_are_not_reported() -> None:
    """A peering of role underlay or a name without a session suffix is not ours to judge."""
    _, infos = run_decommission(peerings=[dc_peering("x-bgp", role="underlay"), dc_peering("plain")])
    assert infos == []
