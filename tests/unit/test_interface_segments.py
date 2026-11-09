"""Unit tests for the readers of an interface's VXLAN segments and a device's VRF memberships.

A switch port or LAG carries a segment as a member of the segment's deployment, a ManagedSegmentDeployment among its
``capabilities``; a device's VRF set is ``vrfs`` (its memberships), never ``capabilities``.
"""

from __future__ import annotations

from typing import Any

from tests.smoke.fixture_builders.common import make_transform
from tests.unit.segment_port_helpers import deployment_caps
from transforms.config.leaf import Leaf
from transforms.helpers.addressing import sub_interface_parent
from transforms.helpers.segments import interface_segments
from transforms.helpers.vrf import device_vrfs

_SEG = {"id": "seg-1", "name": "web", "typename": "ManagedVxlanSegment"}
_SEG_DIRECT = {"id": "seg-2", "name": "app", "typename": "ManagedVxlanSegment"}
_OTHER = {"id": "ospf-1", "name": "ospf-eth1", "typename": "RoutingOSPFInterface"}


def test_a_switch_port_reads_the_segment_of_its_deployment() -> None:
    """The port's L2 membership is the deployment among its capabilities; the other capabilities are not segments."""
    assert interface_segments({"name": "Ethernet10", "capabilities": [_OTHER, *deployment_caps(_SEG)]}) == [_SEG]


def test_a_segment_directly_among_the_capabilities_is_ignored() -> None:
    """A segment is never on the interface edge: only a deployment's segment is read."""
    assert interface_segments({"name": "eth1.210", "capabilities": [_OTHER, _SEG_DIRECT]}) == []
    assert interface_segments({"name": "Ethernet10", "capabilities": [_SEG_DIRECT, *deployment_caps(_SEG)]}) == [_SEG]


def test_an_interface_without_the_edge_has_no_segment() -> None:
    """A missing or empty edge is an empty list, never an error; a deployment without its segment adds none."""
    assert interface_segments({"name": "Loopback0"}) == []
    assert interface_segments({"name": "Ethernet1", "capabilities": None}) == []
    assert interface_segments({"name": "Ethernet1", "capabilities": [{"typename": "ManagedSegmentDeployment"}]}) == []


def test_device_vrfs_are_the_membership_namespaces() -> None:
    """The namespace dicts come from ``vrfs``; a ManagedVRF-looking capability is ignored."""
    prod = {"name": "PROD", "l3_vni": 50000, "vrf_type": "prod"}
    device: dict[str, Any] = {
        "shared_capabilities": [
            {"typename": "ManagedVRF", "security_zone": prod},
            {"typename": "ManagedVRF", "security_zone": None},
        ],
        "capabilities": [{"typename": "ManagedVRF", "security_zone": {"name": "OTHER"}}],
    }

    assert device_vrfs(device) == [prod]
    assert device_vrfs({"name": "no-vrfs"}) == []


def test_sub_interface_parent_only_for_a_parent_on_the_own_device() -> None:
    """A NIC riding on the host port of ANOTHER device is not a sub-interface of it; the name plays no part."""
    parent = {"name": "Ethernet5", "device": {"id": "dev-1"}}
    own = {"id": "dev-1"}

    assert sub_interface_parent({"name": "Ethernet5.100", "device": own, "parent_interface": parent}) == parent
    # the name says nothing: a Junos unit, an IOS sub-interface tagged otherwise, an untagged one
    assert sub_interface_parent({"name": "unit-7", "device": own, "parent_interface": parent}) == parent
    assert sub_interface_parent({"name": "Ethernet5.100", "device": {"id": "vm-1"}, "parent_interface": parent}) is None
    assert sub_interface_parent({"name": "eth0", "device": {"id": "vm-1"}, "parent_interface": parent}) is None
    assert sub_interface_parent({"name": "Ethernet5.100", "device": own, "parent_interface": None}) is None
    assert sub_interface_parent({"name": "Ethernet5.100", "parent_interface": parent}) is None


def test_a_segment_in_the_capabilities_does_not_become_an_activation() -> None:
    """The activation collector reads the ports' deployments only."""
    seg = {
        **_SEG,
        "segment_deployments": [{"vni": 10100, "vlans": [{"device": {"id": "d"}, "vlan_id": 100}]}],
    }
    other = {**seg, "id": "seg-9", "name": "legacy"}
    transform = make_transform(Leaf)
    activations = transform._collect_activations_from_interfaces(
        [{"name": "Ethernet10", "capabilities": [other, *deployment_caps(seg)]}], device_id="d"
    )

    assert [a["segment"]["id"] for a in activations] == ["seg-1"]


def test_device_vrfs_renders_a_membership_of_another_deployment_as_given() -> None:
    """``device_vrfs`` reads every membership of ``vrfs`` whatever its deployment; vrf_instances reports the stray one."""
    here = {"name": "PROD", "l3_vni": 50000}
    elsewhere = {"name": "OTHER-DC", "l3_vni": 50009}
    device: dict[str, Any] = {
        "shared_capabilities": [
            {"typename": "ManagedVRF", "security_zone": here, "deployment": {"name": "dc1"}},
            {"typename": "ManagedVRF", "security_zone": elsewhere, "deployment": {"name": "dc9"}},
        ],
    }

    assert device_vrfs(device) == [here, elsewhere]
