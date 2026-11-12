"""The 30_all customer and fabric data states intent only:"""

from __future__ import annotations

import pytest

from tests.integration.demo_data import ROOT, Declared, attribute, declared, declared_in, name, relationship
from utils.exchange_transit import is_exchange_zone, namespace_type_for_environment, zone_name_for_namespace_type

_ALL_DEMO = ROOT / "data" / "demos" / "30_all"

# Kinds whose 30_all declarations carry no restated schema default.
_MINIMAL_KINDS = (
    "TopologyDataCenter",
    "TopologyPod",
    "LocationRack",
    "TopologyCustomerDC",
    "TopologyCustomerColocation",
    "TopologyCustomerCloud",
    "TopologyCustomerOffice",
    "ManagedVxlanSegment",
    "AppApplication",
    "AppComponent",
    "AppDependency",
    "DcimPhysicalDevice",
    "DcimVirtualDevice",
    "ManagedControllerPhysical",
    "ManagedControllerVirtual",
)
_ALWAYS_EXPLICIT = frozenset({"environment"})


def _restated_defaults(obj: Declared) -> list[str]:
    """The attributes an object declares with exactly the value the server would set without them."""
    restated = []
    for key, value in obj.item.items():
        definition = attribute(obj.kind, key)
        if key in _ALWAYS_EXPLICIT or not definition:
            continue
        if obj.default(key) is not None and value == obj.default(key):
            restated.append(f"{key}: {value}")
    return restated


@pytest.mark.parametrize("kind", _MINIMAL_KINDS)
def test_no_restated_schema_defaults(kind: str) -> None:
    """status: active on a device, access_status: auto, rack_type: compute, ... are the defaults: leave them out."""
    offenders = [
        f"{obj.path.relative_to(ROOT)}: {name_or_key}"
        for obj in declared(kind, _ALL_DEMO)
        for name_or_key in _restated_defaults(obj)
    ]
    assert not offenders, f"{kind} restates schema defaults:\n" + "\n".join(offenders)


def test_device_status_defaults_to_active() -> None:
    """The default the deleted `status: active` lines rely on."""
    assert (attribute("DcimPhysicalDevice", "status") or {}).get("default_value") == "active"


def test_a_template_status_overrides_the_schema_default() -> None:
    """A host from PowerEdge-R640_SERVER would be created provisioning:"""
    hosts = [obj for obj in declared("DcimPhysicalDevice", _ALL_DEMO) if obj.item.get("name") == "dc10-pod1-server-1"]
    assert len(hosts) == 1
    assert hosts[0].default("status") == "provisioning"
    assert hosts[0].item["status"] == "active"


@pytest.mark.parametrize(
    "kind",
    [
        "TopologyEnvironmentScoped",
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerCloud",
        "AppApplication",
        "ManagedVxlanSegment",
    ],
)
def test_environment_is_required_with_no_default(kind: str) -> None:
    """A DC, colocation or cloud footprint, a segment or an application always states its environment."""
    definition = attribute(kind, "environment") or {}
    assert definition.get("optional") is False
    assert "default_value" not in definition


def test_an_office_has_no_environment() -> None:
    """An office is always PROD and is no TopologyCustomer: it states none."""
    assert attribute("TopologyCustomerOffice", "environment") is None


_SEGMENT_KINDS = ("ManagedVxlanSegment", "CloudNetworkSegment")
_ALL_DATA = ROOT / "data"


def test_the_scan_finds_segments_of_every_dataset() -> None:
    """VXLAN segments (a colocation cage's too) and cloud subnets of 30_all, 00_full_topology and the others."""
    found = {name(obj) for obj in declared_in(_ALL_DATA) if obj.kind in _SEGMENT_KINDS}
    assert {"c005-web-frontend-local-dc10-p", "c011-nordix-prod-p", "c003-vaultex-subnet-prod-p"} <= found
    assert any(obj.kind == "CloudNetworkSegment" for obj in declared_in(ROOT / "data" / "demos" / "00_full_topology"))


def test_security_zone_is_required_with_no_default() -> None:
    """The schema refuses a segment without its routing domain: nothing fills it in after creation."""
    definition = relationship("ManagedNetworkSegment", "security_zone") or {}
    assert definition.get("optional") is False


@pytest.mark.parametrize("kind", _SEGMENT_KINDS)
def test_every_authored_segment_names_its_security_zone(kind: str) -> None:
    """Every segment in data/ states its zone:"""
    offenders = []
    for obj in declared(kind, _ALL_DATA):
        zone = obj.item.get("security_zone")
        expected = zone_name_for_namespace_type(namespace_type_for_environment(str(obj.item.get("environment"))))
        if not zone or (zone != expected and not is_exchange_zone(zone)):
            offenders.append(f"{obj.path.relative_to(ROOT)}: {name(obj)} security_zone {zone}, expected {expected}")
    assert not offenders, "\n".join(offenders)
