"""The cloud connection schema: one gateway kind, attachments with two targets, neutral enums, short descriptions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_TOPOLOGY = Path(__file__).resolve().parents[2] / "schemas" / "extensions" / "topology"
_FILES = ("topology_cloud_network.yml", "topology_cloud.yml", "topology_cloud_services.yml")
_OPERATOR_WORDS = ("aws", "azure", "gcp", "oci", "ibm", "alibaba", "equinix", "megaport", "vpc", "vnet")


def _documents() -> list[dict[str, Any]]:
    return [yaml.safe_load((_TOPOLOGY / name).read_text()) for name in _FILES]


def _nodes() -> dict[str, dict[str, Any]]:
    """Every Cloud* node and generic by kind name."""
    found: dict[str, dict[str, Any]] = {}
    for document in _documents():
        for section in ("nodes", "generics"):
            for node in document.get(section) or []:
                found[f"{node['namespace']}{node['name']}"] = node
    return found


def _names(node: dict[str, Any], section: str) -> set[str]:
    return {item["name"] for item in node.get(section) or []}


def _field(node: dict[str, Any], section: str, name: str) -> dict[str, Any]:
    (found,) = [item for item in node[section] if item["name"] == name]
    return found


def _extension(kind: str) -> dict[str, Any]:
    for document in _documents():
        for extension in (document.get("extensions") or {}).get("nodes") or []:
            if extension["kind"] == kind:
                return extension
    raise AssertionError(f"no extension of {kind}")


def test_the_transit_gateway_kinds_are_merged_into_the_gateway() -> None:
    """No CloudTransitGateway, attachment or route table of the old names remain."""
    kinds = set(_nodes())

    assert {"CloudGateway", "CloudGatewayAttachment", "CloudGatewayRouteTable"} <= kinds
    assert not {k for k in kinds if "TransitGateway" in k}


def test_gateway_types_are_hub_edge_customer_and_the_region_is_optional() -> None:
    """The type is neutral and a global gateway has no region."""
    gateway = _nodes()["CloudGateway"]
    choices = {choice["name"] for choice in _field(gateway, "attributes", "gateway_type")["choices"]}

    assert choices == {"hub", "edge", "customer"}
    assert _field(gateway, "relationships", "region")["optional"] is True
    assert not {"transit_gateway", "virtual_network", "announced_prefixes"} & _names(gateway, "relationships")


def test_the_gateway_lists_its_attachments_on_the_attachment_identifier() -> None:
    """The reverse edge `attachments` shares the identifier of the attachment's `gateway`."""
    attachment = _nodes()["CloudGatewayAttachment"]
    extension = _extension("CloudGateway")
    reverse = _field(extension, "relationships", "attachments")

    assert reverse["peer"] == "CloudGatewayAttachment"
    assert reverse["identifier"] == _field(attachment, "relationships", "gateway")["identifier"]


def test_an_attachment_has_two_optional_targets_and_one_prefix_list() -> None:
    """virtual_network and peer_gateway are optional (a check holds exactly one); no attachment_type, no transport."""
    attachment = _nodes()["CloudGatewayAttachment"]

    assert _field(attachment, "relationships", "virtual_network")["optional"] is True
    assert _field(attachment, "relationships", "peer_gateway")["optional"] is True
    assert _field(attachment, "relationships", "allowed_prefixes")["peer"] == "IpamPrefix"
    assert "attachment_type" not in _names(attachment, "attributes")
    assert "transport" not in _names(attachment, "relationships")
    identifiers = [r["identifier"] for r in attachment["relationships"] if r["peer"] == "CloudGateway"]
    assert len(identifiers) == len(set(identifiers)) == 2


def test_a_route_table_belongs_to_a_gateway() -> None:
    """The route table points at the gateway, unique by it."""
    table = _nodes()["CloudGatewayRouteTable"]

    assert _field(table, "relationships", "gateway")["peer"] == "CloudGateway"
    assert ["gateway", "name__value"] in table["uniqueness_constraints"]


def test_a_peering_lands_through_its_gateway_only() -> None:
    """transit_gateway and virtual_network are gone; the scope has no transit; prefixes go us -> cloud."""
    peering = _nodes()["CloudPeeringInterface"]
    scopes = {choice["name"] for choice in _field(peering, "attributes", "scope")["choices"]}

    assert not {"transit_gateway", "virtual_network"} & _names(peering, "relationships")
    assert scopes == {"private", "public", "saas"}
    assert "advertised_prefixes" in _names(peering, "relationships")


def test_a_vpn_connection_keeps_the_provider_gateway_and_an_optional_customer_gateway() -> None:
    """customer_gateway is optional (a SaaS tunnel has none), gateway is the one provider-side edge."""
    vpn = _nodes()["CloudVPNConnection"]

    assert _names(vpn, "relationships") == {"customer_gateway", "gateway"}
    assert _field(vpn, "relationships", "customer_gateway")["optional"] is True


def test_a_transport_is_a_slim_device_with_its_place_on_the_transport_generic() -> None:
    """Both transports hold interfaces (DcimDevice) and no managed-device field; the place is one generic's."""
    nodes = _nodes()
    transport = nodes["CloudTransport"]

    for kind in ("CloudDirectConnect", "CloudVPNConnection"):
        inherited = nodes[kind]["inherit_from"]
        assert "CloudTransport" in inherited and "DcimDevice" in inherited
        assert "DcimDeviceManaged" not in inherited and "DcimCapabilities" not in inherited
    assert _names(transport, "relationships") == {"account", "region", "saas_region", "owner"}
    assert all(_field(transport, "relationships", name)["optional"] for name in _names(transport, "relationships"))


def test_a_peering_is_an_interface_of_its_transport_and_keeps_no_transport_edges() -> None:
    """DcimInterface gives device, name and mtu; account, region, saas_region, owner and transport are the transport's."""
    peering = _nodes()["CloudPeeringInterface"]
    parent = _field(peering, "relationships", "parent_interface")

    assert "DcimInterface" in peering["inherit_from"]
    assert not {"transport", "account", "region", "saas_region", "owner"} & _names(peering, "relationships")
    assert "mtu" not in _names(peering, "attributes") and "vlan_id" in _names(peering, "attributes")
    assert parent["peer"] == "DcimInterface" and parent["identifier"] == "cloud_peering__parent_interface"


def test_virtual_network_peers_are_symmetric_on_one_identifier() -> None:
    """One many-relationship to its own kind with one identifier and no direction."""
    peers = _field(_nodes()["CloudVirtualNetwork"], "relationships", "peers")

    assert peers["peer"] == "CloudVirtualNetwork" and peers["cardinality"] == "many"
    assert peers["identifier"] and "direction" not in peers


def test_the_service_endpoint_is_a_component_of_the_virtual_network() -> None:
    """A parent edge, a (parent, name) HFID and the reverse Component on the network."""
    endpoint = _nodes()["CloudServiceEndpoint"]
    reverse = _field(_extension("CloudVirtualNetwork"), "relationships", "service_endpoints")

    assert _field(endpoint, "relationships", "virtual_network")["kind"] == "Parent"
    assert endpoint["human_friendly_id"] == ["virtual_network__name__value", "name__value"]
    assert reverse["kind"] == "Component" and reverse["peer"] == "CloudServiceEndpoint"
    assert {choice["name"] for choice in _field(endpoint, "attributes", "access")["choices"]} == {"address", "route"}


@pytest.mark.parametrize(
    ("kind", "attribute"),
    [
        ("CloudGateway", "gateway_type"),
        ("CloudDirectConnect", "connection_type"),
        ("CloudPeeringInterface", "scope"),
        ("CloudPeeringInterface", "tunnel_mode"),
        ("CloudServiceEndpoint", "access"),
        ("CloudNetworkSegment", "segment_type"),
    ],
)
def test_enum_values_carry_no_operator_word(kind: str, attribute: str) -> None:
    """The operator's word lives in names and descriptions, never in a choice name."""
    names = [choice["name"] for choice in _field(_nodes()[kind], "attributes", attribute)["choices"]]

    assert names and not [n for n in names if any(word in n for word in _OPERATOR_WORDS)]


def test_no_description_exceeds_128_characters() -> None:
    """The server refuses a longer description."""
    too_long = []
    for kind, node in _nodes().items():
        for section in ("attributes", "relationships"):
            for item in node.get(section) or []:
                if len(item.get("description") or "") > 128:
                    too_long.append(f"{kind}.{item['name']}")
                for choice in item.get("choices") or []:
                    if len(choice.get("description") or "") > 128:
                        too_long.append(f"{kind}.{item['name']}={choice['name']}")
        if len(node.get("description") or "") > 128:
            too_long.append(kind)

    assert too_long == []
