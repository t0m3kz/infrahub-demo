"""Unit tests for the readers of an interface's VXLAN segments and a device's VRF memberships.

A switch port or LAG carries its segments on ``segments`` (DcimSwitchedInterface); a sub-interface carries the inline L3
termination of one segment on ``inline_segment``; nothing is read from ``capabilities``; a device's VRF set is ``vrfs`` (its memberships), never ``capabilities``.
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


def test_a_sub_interface_reads_its_inline_segment() -> None:
    """The inline termination is the single ``inline_segment`` node (a dict)."""
    assert interface_segments({"name": "eth1.210", "inline_segment": _SEG_INLINE, "capabilities": [_OTHER]}) == [
        _SEG_INLINE
    ]


def test_a_segment_among_the_capabilities_is_ignored() -> None:
    """An old-style segment entry on ``capabilities`` is not read, on a sub-interface or on a port."""
    assert interface_segments({"name": "eth1.210", "capabilities": [_OTHER, _SEG_INLINE]}) == []
    assert interface_segments({"name": "Ethernet10", "segments": [_SEG], "capabilities": [_SEG_INLINE]}) == [_SEG]


def test_an_interface_without_either_edge_has_no_segment() -> None:
    """Missing or empty edges are an empty list, never an error."""
    assert interface_segments({"name": "Loopback0"}) == []
    assert (
        interface_segments({"name": "Ethernet1", "segments": None, "inline_segment": None, "capabilities": None}) == []
    )


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


def test_a_segment_in_the_capabilities_does_not_become_an_activation() -> None:
    """The activation collector reads ``segments`` and ``inline_segment`` only."""
    seg = {
        **_SEG,
        "segment_deployments": [{"vni": 10100, "vlan_domain_segments": [{"device": {"id": "d"}, "vlan_id": 100}]}],
    }
    other = {**seg, "id": "seg-9", "name": "legacy"}
    transform = make_transform(Leaf)
    activations = transform._collect_activations_from_interfaces(
        [{"name": "Ethernet10", "segments": [seg], "capabilities": [other]}], device_id="d"
    )

    assert [a["segment"]["id"] for a in activations] == ["seg-1"]


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
