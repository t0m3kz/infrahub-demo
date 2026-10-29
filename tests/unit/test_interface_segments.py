"""Unit tests for the readers of an interface's VXLAN segments and a device's VRF memberships.

A switch port or LAG carries its segments on ``segments`` (DcimSwitchedInterface); a sub-interface keeps the inline L3
termination of a segment on ``capabilities``; a device's VRF set is ``vrfs`` (its memberships), never ``capabilities``.
"""

from __future__ import annotations

from typing import Any

from tests.smoke.fixture_builders.common import make_transform
from transforms.config.leaf import Leaf
from transforms.helpers.addressing import sub_interface_parent
from transforms.helpers.segments import interface_segments
from transforms.helpers.vrf import device_vrfs

_SEG = {"id": "seg-1", "name": "web", "typename": "ManagedVxlanSegment"}
_SEG_INLINE = {"id": "seg-2", "name": "app", "typename": "ManagedVxlanSegment"}
_OTHER = {"id": "ospf-1", "name": "ospf-eth1", "typename": "RoutingOSPFInterface"}


def test_a_switch_port_reads_its_segments_edge() -> None:
    """The port's L2 membership is ``segments``."""
    assert interface_segments({"name": "Ethernet10", "segments": [_SEG], "capabilities": [_OTHER]}) == [_SEG]


def test_a_sub_interface_reads_the_segment_among_its_capabilities() -> None:
    """The inline termination stays on ``capabilities``; other capabilities are not segments."""
    assert interface_segments({"name": "eth1.210", "capabilities": [_OTHER, _SEG_INLINE]}) == [_SEG_INLINE]


def test_an_interface_without_either_edge_has_no_segment() -> None:
    """Missing or empty edges are an empty list, never an error."""
    assert interface_segments({"name": "Loopback0"}) == []
    assert interface_segments({"name": "Ethernet1", "segments": None, "capabilities": None}) == []


def test_device_vrfs_are_the_membership_namespaces() -> None:
    """The namespace dicts come from ``vrfs``; a ManagedVRF-looking capability is ignored."""
    prod = {"name": "PROD", "l3_vni": 50000, "namespace_type": "prod"}
    device: dict[str, Any] = {
        "shared_capabilities": [
            {"typename": "ManagedVRF", "namespace": prod},
            {"typename": "ManagedVRF", "namespace": None},
        ],
        "capabilities": [{"typename": "ManagedVRF", "namespace": {"name": "OTHER"}}],
    }

    assert device_vrfs(device) == [prod]
    assert device_vrfs({"name": "no-vrfs"}) == []


def test_sub_interface_parent_only_for_a_name_under_the_parent() -> None:
    """A NIC riding on a host port is not a VLAN sub-interface of it."""
    parent = {"name": "Ethernet5"}

    assert sub_interface_parent({"name": "Ethernet5.100", "parent_interface": parent}) == parent
    assert sub_interface_parent({"name": "eth0", "parent_interface": parent}) is None
    assert sub_interface_parent({"name": "Ethernet5.100", "parent_interface": None}) is None


def test_a_port_carrying_a_segment_on_both_edges_lists_it_twice_and_the_activations_count_it_once() -> None:
    """``interface_segments`` concatenates the two edges (no dedupe); the activation collector dedupes by segment id.

    A switch port keeps its segments on ``segments``, so a segment-typed ``capabilities`` entry on the same port is
    not produced by the generators; if one appears the reader returns it twice and only the activations are safe.
    """
    iface = {"name": "Ethernet10", "segments": [_SEG], "capabilities": [_SEG]}

    assert interface_segments(iface) == [_SEG, _SEG]

    seg = {
        **_SEG,
        "segment_deployments": [{"vni": 10100, "vlan_domain_segments": [{"vlan_domain": {"id": "d"}, "vlan_id": 100}]}],
    }
    transform = make_transform(Leaf)
    activations = transform._collect_activations_from_interfaces(
        [{"name": "Ethernet10", "segments": [seg], "capabilities": [seg]}], device_id="d", device_capabilities=[]
    )

    assert [a["vlan_id"] for a in activations] == [100]


def test_device_vrfs_renders_a_membership_of_another_deployment_as_given() -> None:
    """``device_vrfs`` reads every membership of ``vrfs`` whatever its deployment; vrf_instances reports the stray one."""
    here = {"name": "PROD", "l3_vni": 50000}
    elsewhere = {"name": "OTHER-DC", "l3_vni": 50009}
    device: dict[str, Any] = {
        "shared_capabilities": [
            {"typename": "ManagedVRF", "namespace": here, "deployment": {"name": "dc1"}},
            {"typename": "ManagedVRF", "namespace": elsewhere, "deployment": {"name": "dc9"}},
        ],
    }

    assert device_vrfs(device) == [here, elsewhere]
