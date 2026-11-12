"""The device generics: a slim DcimDevice (name, interfaces) and DcimDeviceManaged (everything else of a device)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_MOVED_ATTRIBUTES = {"description", "role", "status"}
_MOVED_RELATIONSHIPS = {"device_type", "platform", "primary_address", "tags"}
_MANAGED_KINDS = {
    "DcimPhysicalDevice",
    "DcimVirtualDevice",
    "CloudNetworkAppliance",
    "ManagedControllerPhysical",
    "ManagedControllerVirtual",
}
# The provider's port of a colocation metro (an Equinix Fabric port) is an interface holder of the same kind.
_TRANSPORTS = {"CloudDirectConnect", "CloudVPNConnection", "TopologyProviderConnect"}
# THE INTERNET: a passive meeting point, a slim device that is also a facility (a carrier circuit's location).
_PASSIVE = {"TopologyInternet"}


def _documents() -> list[dict[str, Any]]:
    return [yaml.safe_load(path.read_text()) or {} for path in sorted(_SCHEMAS.rglob("*.yml"))]


def _kinds(section: str) -> dict[str, dict[str, Any]]:
    """Every node (or generic) by kind name, over all schema files."""
    return {
        f"{item['namespace']}{item['name']}": item for document in _documents() for item in document.get(section) or []
    }


def _names(item: dict[str, Any], section: str) -> set[str]:
    return {field["name"] for field in item.get(section) or []}


def _extension_names(kind: str) -> set[str]:
    return {
        relationship["name"]
        for document in _documents()
        for extension in (document.get("extensions") or {}).get("nodes") or []
        if extension["kind"] == kind
        for relationship in extension.get("relationships") or []
    }


def test_the_slim_device_holds_only_a_name_and_its_interfaces() -> None:
    """All an interface needs of its device: the unique name and the component edge."""
    device = _kinds("generics")["DcimDevice"]

    assert _names(device, "attributes") == {"name"}
    assert _names(device, "relationships") == {"interfaces"}
    assert next(a for a in device["attributes"] if a["name"] == "name")["unique"] is True


def test_the_managed_device_holds_the_rest_of_the_device() -> None:
    """Role, status, type, platform, address, tags, and the owner extension (the software image is DcimSoftwareRunner's)."""
    managed = _kinds("generics")["DcimDeviceManaged"]

    assert _MOVED_ATTRIBUTES <= _names(managed, "attributes")
    assert _MOVED_RELATIONSHIPS <= _names(managed, "relationships")
    assert "owner" in _extension_names("DcimDeviceManaged")
    assert "software_image" not in _extension_names("DcimDeviceManaged")
    assert not _extension_names("DcimDevice")


def test_everything_that_runs_software_inherits_the_runner_and_the_image_has_one_reverse_edge() -> None:
    """A device, a cluster and a cloud instance share ONE software_image edge; the image lists them in `runners`."""
    nodes = _kinds("nodes")
    runners = {kind for kind, node in nodes.items() if "DcimSoftwareRunner" in node.get("inherit_from", [])}
    assert runners == {
        "DcimPhysicalDevice",
        "DcimVirtualDevice",
        "ManagedControllerPhysical",
        "ManagedControllerVirtual",
        "CloudNetworkAppliance",
        "ManagedHypervisorCluster",
        "CloudHypervisorCluster",
        "ManagedKubernetesCluster",
        "CloudKubernetesCluster",
        "CloudInstance",
    }
    image = nodes["DcimSoftwareImage"]
    assert _names(image, "relationships") >= {"platform", "runners"}
    assert not {"devices", "clusters"} & _names(image, "relationships")


def test_every_managed_device_kind_inherits_both_generics() -> None:
    """Physical, virtual, appliance and controllers are devices of both generics."""
    nodes = _kinds("nodes")

    for kind in sorted(_MANAGED_KINDS):
        inherited = nodes[kind]["inherit_from"]
        assert "DcimDevice" in inherited and "DcimDeviceManaged" in inherited, kind


def test_a_cloud_transport_inherits_only_the_slim_device() -> None:
    """A transport is an interface holder: no role, platform, owner, artifact or capability edge."""
    nodes = _kinds("nodes")

    for kind in sorted(_TRANSPORTS):
        inherited = set(nodes[kind]["inherit_from"])
        assert "DcimDevice" in inherited, kind
        assert not inherited & {"DcimDeviceManaged", "CoreArtifactTarget", "AppInstance"}, kind


def test_the_internet_is_a_passive_device_and_a_facility_with_one_interface_kind() -> None:
    """Ports through the slim device, a location through the facility; no role, platform, owner, artifact, capability."""
    nodes = _kinds("nodes")

    assert set(nodes["TopologyInternet"]["inherit_from"]) == {
        "DcimDevice",
        "TopologyDeployment",
        "TopologyConnectableLocation",
        "TopologyFacility",
    }
    assert nodes["TopologyInternet"]["parent"] == "TopologyLabel"
    assert nodes["TopologyInternetInterface"]["inherit_from"] == ["DcimInterface"]


def test_only_the_known_kinds_inherit_the_slim_device() -> None:
    """A new holder of interfaces is a decision: managed kinds take both generics."""
    inheritors = {kind for kind, node in _kinds("nodes").items() if "DcimDevice" in (node.get("inherit_from") or [])}

    assert inheritors == _MANAGED_KINDS | _TRANSPORTS | _PASSIVE


def test_the_peers_that_need_a_managed_device_are_retyped() -> None:
    """A transport cannot be put into a data center, a platform, a controller or an SD-WAN gateway."""
    nodes, generics = _kinds("nodes"), _kinds("generics")
    peers = {
        "DcimPlatform": generics["DcimPlatform"] if "DcimPlatform" in generics else nodes["DcimPlatform"],
        "ManagedController": generics["ManagedController"],
        "TopologyDeviceHosting": generics["TopologyDeviceHosting"],
        "TopologyCustomerOffice": nodes["TopologyCustomerOffice"],
    }
    relationship_of = {
        "DcimPlatform": "devices",
        "ManagedController": "managed_devices",
        "TopologyDeviceHosting": "devices",
        "TopologyCustomerOffice": "sdwan_gateway",
    }

    for kind, item in peers.items():
        relationship = next(r for r in item["relationships"] if r["name"] == relationship_of[kind])
        assert relationship["peer"] == "DcimDeviceManaged", kind


def test_the_provider_connect_is_one_slim_device_placed_by_one_facility() -> None:
    """The provider's connect device holds interfaces and nothing else: no account, owner."""
    node = _kinds("nodes")["TopologyProviderConnect"]
    assert set(node["inherit_from"]) == {"DcimDevice", "DcimProviderPort"}
    (location,) = [r for r in node["relationships"] if r["name"] == "location"]
    assert location["peer"] == "TopologyFacility" and location["optional"] is False and location["cardinality"] == "one"
    assert {r["name"] for r in node["relationships"]} == {"location"}
