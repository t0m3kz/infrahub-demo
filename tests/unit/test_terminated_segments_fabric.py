"""Unit tests for segments terminated on a firewall context on the fabric (leaf / ToR / border-leaf).

A segment with ``firewall_gateway`` is gatewayed by that context's firewall pair (its
`gateway` is the pair's floating VIP), so every fabric switch carries it as pure L2:

  - gateway suppression — _get_segment_gateways / routed_segment_namespace hide
    the gateway, so no SVI, no VRF, no L3 VNI (get_vlans, _l2_from_activations, _l3_instances,
    get_vxlan_config's anycast gateway)
  - PBR / SVI-ACL skip — BaseDeviceTransform._extra_config and BorderLeaf feed
    their PBR and ACL helpers routed_activations() only
  - trunk mode — get_interfaces marks a firewall/load-balancer service port
    carrying segment VLANs as mode="trunk"
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.segment_port_helpers import deployment_caps
from transforms.common import BaseDeviceTransform
from transforms.helpers.origination import get_origination
from transforms.helpers.overlay import _l3_instances
from transforms.helpers.segments import (
    _get_segment_gateways,
    _vlans_from_activations,
    routed_activations,
)
from transforms.helpers.vrf import vrf_requirements
from transforms.helpers.vxlan import _l2_from_activations, get_interfaces, get_vxlan_config
from utils.exchange_transit import is_routed_tenant_segment
from utils.segment_addressing import is_terminated, routed_segment_namespace, segment_namespace

# ---------------------------------------------------------------------------
# Helpers (cleaned-data shapes, as clean_data() leaves them)
# ---------------------------------------------------------------------------


def _segment(name: str, *, terminated: bool, gateway: str, vrf: str = "VRF_A", l3_vni: int = 50001) -> dict:
    """A ManagedVxlanSegment with a gateway in `vrf`."""
    return {
        "security_zone": {"name": vrf, "l3_vni": l3_vni},
        "id": f"seg-{name}",
        "typename": "ManagedVxlanSegment",
        "name": name,
        "customer_name": name,
        "firewall_gateway": terminated,
        "gateway": {
            "address": gateway,
            "ip_prefix": {
                "security_zone": {"name": vrf, "l3_vni": l3_vni},
                "prefix": "unused",
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            },
        },
    }


def _terminated_segment() -> dict:
    """A segment terminated on a firewall context: its gateway is the pair's VIP."""
    return _segment("terminated", terminated=True, gateway="10.150.0.1/24", vrf="VRF_TERMINATED", l3_vni=50002)


def _routed_segment() -> dict:
    """Ordinary fabric-routed segment (anycast SVI in VRF_A)."""
    return _segment("routed", terminated=False, gateway="10.100.0.1/24")


def _activations() -> list[dict[str, Any]]:
    """One routed and one terminated activation, as _collect_activations_from_interfaces builds them."""
    return [
        {"vlan_id": 100, "vni": 10100, "segment": _routed_segment()},
        {"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()},
    ]


# ---------------------------------------------------------------------------
# Gateway suppression
# ---------------------------------------------------------------------------


class TestGatewaySuppression:
    def test_is_terminated_reads_firewall_gateway(self) -> None:
        """Only a segment with ``firewall_gateway`` set is terminated; missing, None or false is routed."""
        assert is_terminated(_terminated_segment()) is True
        assert is_terminated(_routed_segment()) is False
        assert is_terminated({"firewall_gateway": None}) is False
        assert is_terminated({"firewall_gateway": False}) is False
        assert is_terminated({}) is False

    def test_segment_gateways_are_hidden_for_terminated_segment(self) -> None:
        """The HA pair's VIP never becomes a fabric gateway, VRF or L3 VNI."""
        assert _get_segment_gateways(_terminated_segment()) == (None, None, None, None)
        assert routed_segment_namespace(_terminated_segment()) == {}

    def test_routed_segment_keeps_its_gateway(self) -> None:
        """A normal segment still resolves gateway, VRF and L3 VNI."""
        assert _get_segment_gateways(_routed_segment()) == ("10.100.0.1/24", None, "VRF_A", 50001)
        assert routed_segment_namespace(_routed_segment())["name"] == "VRF_A"

    def test_vlan_list_keeps_terminated_vlan_without_gateway(self) -> None:
        """The VLAN stays (it is still bridged) but has no gateway/VRF and is flagged."""
        vlans = {v["vlan_id"]: v for v in _vlans_from_activations(_activations(), {100: "VRF_A"})}

        assert vlans[150]["gateway_ip"] is None
        assert vlans[150]["vrf"] is None
        assert vlans[150]["terminated"] is True
        assert vlans[100]["gateway_ip"] == "10.100.0.1/24"
        assert vlans[100]["vrf"] == "VRF_A"
        assert vlans[100]["terminated"] is False

    def test_l2_vni_mapping_stays_without_vrf(self) -> None:
        """The L2 VNI is kept for the terminated segment, with no VRF / L3 VNI binding."""
        mappings = {m["vlan_id"]: m for m in _l2_from_activations(_activations())}

        assert mappings[150]["vni"] == 10150
        assert mappings[150]["gateway_ip"] is None
        assert mappings[150]["vrf"] is None
        assert mappings[150]["l3_vni"] is None

    def test_terminated_segment_contributes_no_l3_vni(self) -> None:
        """Only the routed segment's VRF becomes an L3 VNI mapping."""
        # the terminated segment's own namespace has an instance too (e.g. a stale one): never carried as an L3 instance
        vrfs = [{"name": "VRF_A", "l3_vni": 50001}, {"name": "VRF_TERMINATED", "l3_vni": 50002}]
        assert [i.vrf for i in _l3_instances(vrfs, _activations(), frozenset())] == ["VRF_A"]

    def test_vxlan_config_for_terminated_only_device_is_pure_l2(self) -> None:
        """A VTEP carrying only an terminated segment has an L2 VNI and no anycast gateway or VRF."""
        data = {"name": "leaf-01", "interfaces": [], "capabilities": []}
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()}]

        config = get_vxlan_config(data, "arista_eos", device_role="leaf", activations=activations)

        assert config is not None
        assert [m["vni"] for m in config["l2_vni_mappings"]] == [10150]
        assert config["l3_vni_mappings"] == []
        assert config["anycast_gateway"]["enabled"] is False


def _tagged(segment: dict, *, segment_tag: int) -> dict:
    """The segment with a routing tag, its owner's tag and the prefix its gateway sits in."""
    gateway = {**segment["gateway"], "ip_prefix": {**segment["gateway"]["ip_prefix"], "prefix": "10.1.0.0/24"}}
    return {**segment, "gateway": gateway, "routing_tag": segment_tag, "owner": {"routing_tag": 3}}


class TestEveryFabricConsumerAgrees:
    """The gate is one derivation (utils/segment_addressing.py): each reader of a segment's L3 side honours it."""

    def test_the_firewall_still_sees_the_gateway_namespace(self) -> None:
        """segment_namespace is the gateway prefix's namespace whoever routes it: the firewall and its PBR next hops
        read that one, only the fabric readers go through routed_segment_namespace."""
        assert segment_namespace(_terminated_segment())["name"] == "VRF_TERMINATED"
        assert routed_segment_namespace(_terminated_segment()) == {}

    def test_a_terminated_segment_is_no_tenant_svi(self) -> None:
        """No SVI, so no group tag, no routing tag and no SVI protection to demand (is_routed_tenant_segment)."""

        def tenant(terminated: bool) -> dict:
            return {
                "typename": "ManagedVxlanSegment",
                "firewall_gateway": terminated,
                "gateway": {"ip_prefix": {"ip_namespace": {"name": "default"}}},
                "security_zone": {"name": "PROD", "vrf_type": "prod", "l3_vni": 60000},
            }

        assert is_routed_tenant_segment(tenant(False)) is True
        assert is_routed_tenant_segment(tenant(True)) is False

    def test_a_terminated_segment_originates_nothing_into_evpn(self) -> None:
        """The leaf has no SVI for it, so no prefix is redistributed with the routing-identity communities."""
        terminated = _tagged(_terminated_segment(), segment_tag=7)
        routed = _tagged(_routed_segment(), segment_tag=8)
        activations = [{"vlan_id": 100, "segment": routed}, {"vlan_id": 150, "segment": terminated}]

        entries = get_origination(activations, {"VRF_A", "VRF_TERMINATED"})

        assert [entry["name"] for entry in entries] == ["routed"]

    def test_the_transform_demands_no_vrf_for_a_terminated_segment(self) -> None:
        """The config's VRF demand (its routed activations) holds only the routed segment's namespace."""
        device = {"typename": "DcimPhysicalDevice", "role": "leaf", "segment_deployments": _activations()}

        requirements = vrf_requirements(device)

        assert requirements.namespaces == frozenset({"VRF_A"})
        assert dict(requirements.svis) == {100: "VRF_A"}


# ---------------------------------------------------------------------------
# PBR / SVI ACL skip
# ---------------------------------------------------------------------------


def _make_transform(cls: type[BaseDeviceTransform], device_role: str) -> BaseDeviceTransform:
    """Instantiate a transform bypassing InfrahubTransform.__init__."""
    transform = cls.__new__(cls)
    transform.device_role = device_role
    transform.root_directory = "/fake/root"
    return transform


def _device_data(deployment: dict | None = None) -> dict:
    """A leaf/border-leaf device carrying both activations."""
    return {
        "name": "dev-01",
        "role": "leaf",
        "interfaces": [{"name": "Vlan100", "role": "svi", "vrf": {"namespace": {"name": "VRF_A"}}}],
        "shared_capabilities": [
            {"typename": "ManagedVRF", "security_zone": {"name": "VRF_A", "l3_vni": 50001, "vrf_type": "prod"}}
        ],
        "segment_deployments": _activations(),
        "deployment": deployment,
    }


class TestPbrAndAclSkip:
    def test_routed_activations_drops_terminated_segments(self) -> None:
        """routed_activations keeps only segments the fabric routes."""
        assert [a["vlan_id"] for a in routed_activations(_activations())] == [100]
        assert routed_activations(None) == []

    @pytest.mark.parametrize("role", ["leaf", "tor", "border-leaf"])
    def test_pbr_and_acl_helpers_never_see_terminated_segment(self, role: str) -> None:
        """Customer PBR and SVI ACLs are built from routed activations only."""
        transform = _make_transform(BaseDeviceTransform, role)
        customer_pbr = MagicMock(return_value=[])
        acls = MagicMock(return_value=[])

        with (
            patch("transforms.common.get_customer_pbr_rules", customer_pbr),
            patch("transforms.common.get_acls", acls),
        ):
            transform._extra_config(_device_data(), "arista_eos")

        assert [a["vlan_id"] for a in customer_pbr.call_args.args[0]] == [100]
        assert [a["vlan_id"] for a in acls.call_args.kwargs["activations"]] == [100]

    def test_vlans_and_vxlan_still_carry_terminated_segment(self) -> None:
        """Skipping PBR does not drop the terminated segment's VLAN or L2 VNI."""
        config = _make_transform(BaseDeviceTransform, "leaf")._extra_config(_device_data(), "arista_eos")

        assert [v["vlan_id"] for v in config["vlans"]] == [100, 150]
        assert [m["vni"] for m in config["vxlan"]["l2_vni_mappings"]] == [10100, 10150]
        assert [m["vrf_name"] for m in config["vxlan"]["l3_vni_mappings"]] == ["VRF_A"]


# ---------------------------------------------------------------------------
# Trunk mode in get_interfaces
# ---------------------------------------------------------------------------


def _interface(name: str, role: str, segments: list[dict]) -> dict:
    """A physical interface carrying `segments` (a deployment of each among its capabilities)."""
    return {"name": name, "role": role, "typename": "DcimPhysicalInterface", "capabilities": deployment_caps(*segments)}


class TestTrunkMode:
    @pytest.mark.parametrize("role", ["firewall", "load-balancer"])
    def test_service_port_with_segment_vlans_is_trunk(self, role: str) -> None:
        """A service port facing an HA member trunks the segments it carries."""
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()}]

        (iface,) = get_interfaces([_interface("Ethernet20", role, [_terminated_segment()])], activations=activations)

        assert iface["mode"] == "trunk"
        assert iface["vlans"] == [150]

    def test_service_port_without_segments_is_not_trunk(self) -> None:
        """A routed service port (e.g. the parent of pbr-mode context sub-interfaces) is unchanged."""
        (iface,) = get_interfaces([_interface("Ethernet20", "firewall", [])])

        assert iface["mode"] is None

    def test_customer_port_keeps_access_rendering(self) -> None:
        """A server-facing port carrying a segment is not turned into a trunk."""
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()}]

        (iface,) = get_interfaces(
            [_interface("Ethernet10", "server", [_terminated_segment()])], activations=activations
        )

        assert iface["mode"] is None
        assert iface["vlans"] == [150]

    def test_host_port_with_two_segments_is_trunk_of_both(self) -> None:
        """A server-facing port in two segments trunks exactly those VLANs (not access on the first)."""
        activations = [
            {"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()},
            {"vlan_id": 100, "vni": 10100, "segment": _routed_segment()},
        ]

        (iface,) = get_interfaces(
            [_interface("Ethernet10", "server", [_terminated_segment(), _routed_segment()])], activations=activations
        )

        assert iface["mode"] == "trunk"
        assert iface["vlans"] == [100, 150]

    def test_trunk_vlan_order_does_not_depend_on_segment_order(self) -> None:
        """The same two segments in either query order give the same trunk list (no churn between leaves)."""
        activations = [
            {"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()},
            {"vlan_id": 100, "vni": 10100, "segment": _routed_segment()},
        ]
        forward = [_terminated_segment(), _routed_segment()]

        (a,) = get_interfaces([_interface("Ethernet10", "server", forward)], activations=activations)
        (b,) = get_interfaces([_interface("Ethernet10", "server", forward[::-1])], activations=activations)

        assert a["vlans"] == b["vlans"] == [100, 150]

    def test_host_port_with_unactivated_second_segment_stays_access(self) -> None:
        """A second segment with no activation on this device adds no VLAN, so the port stays access."""
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _terminated_segment()}]

        (iface,) = get_interfaces(
            [_interface("Ethernet10", "server", [_terminated_segment(), _routed_segment()])], activations=activations
        )

        assert iface["mode"] is None
        assert iface["vlans"] == [150]
