"""A switch renders every segment it has a VLAN row of, and a border leaf carries a stretched segment L2 only.

The rows are the device's ``vlans`` (queries/fragments/network_segment.gql DeviceSegmentRowsFields);
transforms/common.py turns each into an activation after the ports' segments. A stretched segment on a border leaf gets the L2-only view
(utils/segment_addressing.py ``l2_only_on``): VLAN and VNI, no SVI, VRF, L3 VNI, ACL or PBR.
"""

from __future__ import annotations

from typing import Any

from tests.unit.base_device_transform_helpers import _make_transform
from transforms.helpers.segments import _get_segment_gateways, get_vlans, routed_activations
from utils.segment_addressing import is_fabric_l2, l2_only_on, l2_only_view, routed_segment_namespace


def _segment(seg_id: str, stretch_scope: str = "local", gateway: str = "10.9.0.1/24") -> dict[str, Any]:
    """A cleaned routed VXLAN segment in PROD."""
    return {
        "typename": "ManagedVxlanSegment",
        "id": seg_id,
        "name": seg_id,
        "customer_name": seg_id,
        "stretch_scope": stretch_scope,
        "security_zone": {"id": "z-prod", "name": "PROD", "vrf_type": "prod", "l3_vni": 60000},
        "gateway": {"address": gateway, "ip_prefix": {"prefix": "10.9.0.0/24", "ip_namespace": {"name": "default"}}},
    }


def _row(segment: dict[str, Any], vlan_id: int | None = 2500, vni: int | None = 40000) -> dict[str, Any]:
    """A cleaned row of the device (one of its ``vlans``)."""
    return {
        "vlan_id": vlan_id,
        "segment_deployment": {"vni": vni, "segment": segment},
    }


def _device(role: str, rows: list[dict[str, Any]], interfaces: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A cleaned switch with its rows."""
    return {
        "id": "dev-1",
        "name": "sw-1",
        "role": role,
        "interfaces": interfaces or [],
        "vlans": rows,
    }


class TestRowActivations:
    def test_a_row_without_a_port_becomes_an_activation(self) -> None:
        """A stretched segment is realized on every VTEP with or without a port: the row alone renders it."""
        activations = _make_transform("leaf")._device_activations(_device("leaf", [_row(_segment("sync", "dc_pair"))]))
        assert [(act["vlan_id"], act["vni"], act["segment"]["id"]) for act in activations] == [(2500, 40000, "sync")]

    def test_a_device_without_rows_has_no_row_activation(self) -> None:
        """No row, no port: nothing to render."""
        assert _make_transform("leaf")._device_activations(_device("leaf", [])) == []

    def test_a_port_activation_comes_first_and_the_row_of_its_segment_adds_nothing(self) -> None:
        """Ports first: a segment a port carries keeps the activation (and flags) the port produced."""
        segment = _segment("web")
        port_segment = {**segment, "segment_deployments": [{"vni": 10100, "vlans": [_port_row(100)]}]}
        port = {
            "name": "Ethernet1",
            "capabilities": [{"typename": "ManagedSegmentVlan", "segment_deployment": {"segment": port_segment}}],
        }
        device = _device("leaf", [_row(segment, 100, 10100)], [port])
        activations = _make_transform("leaf")._device_activations(device)
        assert len(activations) == 1 and activations[0]["segment"] is port_segment

    def test_an_unconverged_row_is_skipped(self) -> None:
        """No VLAN id yet, or a deployment without a VNI: not converged, as on a port."""
        rows = [_row(_segment("a"), vlan_id=None), _row(_segment("b"), vni=None)]
        assert _make_transform("leaf")._device_activations(_device("leaf", rows)) == []


def _port_row(vlan_id: int) -> dict[str, Any]:
    """The row of ``dev-1`` under a port segment's deployment."""
    return {"vlan_id": vlan_id, "device": {"id": "dev-1"}}


class TestBorderLeafCarriesAStretchedSegmentL2Only:
    def test_the_rule_is_a_border_leaf_and_a_stretched_segment(self) -> None:
        """Only a border leaf, only a stretched segment; an unselected stretch_scope reads as local."""
        assert l2_only_on("border-leaf", {"stretch_scope": "dc_pair"})
        assert not l2_only_on("border-leaf", {"stretch_scope": "local"})
        assert not l2_only_on("border-leaf", {})
        assert not l2_only_on("leaf", {"stretch_scope": "dc_pair"})
        assert not l2_only_on("edge", {"stretch_scope": "dc_pair"})

    def test_the_view_hides_the_gateway_from_every_fabric_reader(self) -> None:
        """The same gate as a terminated segment: no gateway, VRF or L3 VNI; no ACL or PBR input."""
        view = l2_only_view(_segment("sync", "dc_pair"))
        assert is_fabric_l2(view)
        assert routed_segment_namespace(view) == {}
        assert _get_segment_gateways(view) == (None, None, None, None)
        assert routed_activations([{"vlan_id": 2500, "segment": view}]) == []
        vlan = get_vlans([{"vlan_id": 2500, "vni": 40000, "segment": view}])[0]
        assert (vlan["gateway_ip"], vlan["vrf"], vlan["terminated"]) == (None, None, True)

    def test_a_border_leaf_activation_of_a_stretched_segment_is_l2_only(self) -> None:
        """The border leaf's activation carries the L2-only view; a local segment stays routed."""
        rows = [_row(_segment("sync", "dc_pair")), _row(_segment("local"), 100, 10100)]
        activations = _make_transform("border-leaf")._device_activations(_device("border-leaf", rows))
        by_id = {act["segment"]["id"]: act for act in activations}
        assert is_fabric_l2(by_id["sync"]["segment"]) and by_id["sync"]["vni"] == 40000
        assert not is_fabric_l2(by_id["local"]["segment"])

    def test_a_leaf_routes_the_stretched_segment(self) -> None:
        """The anycast gateway of a stretched segment stays on the leaves."""
        activations = _make_transform("leaf")._device_activations(_device("leaf", [_row(_segment("sync", "dc_pair"))]))
        assert not is_fabric_l2(activations[0]["segment"])
        assert routed_segment_namespace(activations[0]["segment"])["name"] == "PROD"
