"""The 30_all customer and fabric data states intent only: nothing a schema default or a generator supplies.

A restated default is a second source of truth that silently diverges when the
schema default moves; a hand-typed derived value (a segment's security zone)
either repeats the generator or contradicts it. Both are deleted from the data,
and these tests keep them out. ``environment`` is the deliberate exception: it
must stay explicit (a silent ``p`` means the PROD namespace, zone and firewall
leg), and the schema requires it on footprints and applications.
"""

from __future__ import annotations

import pytest

from tests.integration.demo_data import ROOT, Declared, attribute, declared, declared_in, name
from utils.exchange_transit import is_exchange_zone

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
    "ManagedVlanSegment",
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
    """The attributes an object declares with exactly the value the server would set without them
    (its object template's, else the schema default)."""
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
    """A host from PowerEdge-R640_SERVER would be created provisioning: its `status: active` is intent, not a
    restated default, and stays."""
    hosts = [obj for obj in declared("DcimPhysicalDevice", _ALL_DEMO) if obj.item.get("name") == "dc10-pod1-server-1"]
    assert len(hosts) == 1
    assert hosts[0].default("status") == "provisioning"
    assert hosts[0].item["status"] == "active"


@pytest.mark.parametrize(
    "kind",
    [
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerCloud",
        "TopologyCustomerOffice",
        "AppApplication",
    ],
)
def test_environment_is_required_with_no_default(kind: str) -> None:
    """A footprint or an application always states its environment."""
    definition = attribute(kind, "environment") or {}
    assert definition.get("optional") is False
    assert "default_value" not in definition


def _generator_zoned_segments() -> set[str]:
    """Segments whose zone a generator derives: every VXLAN segment (add_vxlan_segment) and every
    segment an application component lives on (add_app_application)."""
    vxlan = {name(obj) for obj in declared("ManagedVxlanSegment", _ALL_DEMO)}
    used = {
        str(obj.item["network_segment"])
        for obj in declared("AppComponent", _ALL_DEMO)
        if obj.item.get("network_segment")
    }
    return vxlan | used


def test_the_scan_finds_generator_zoned_segments() -> None:
    """VXLAN segments, colocation VLAN segments with components, and cloud subnets with components."""
    zoned = _generator_zoned_segments()
    assert {"c005-web-frontend-local-dc10-p", "c011-nordix-prod-p", "c003-vaultex-subnet-prod-p"} <= zoned


def test_generator_zoned_segments_declare_no_tenant_zone() -> None:
    """ensure_security_zone derives PROD-ZONE/NONPROD-ZONE from the environment: typing it is a second source.
    An exchange zone (MANAGEMENT services) is authored data the generator keeps."""
    zoned = _generator_zoned_segments()
    offenders = [
        f"{obj.path.relative_to(ROOT)}: {name(obj)} security_zone {obj.item['security_zone']}"
        for obj in declared_in(_ALL_DEMO)
        if obj.kind in {"ManagedVxlanSegment", "ManagedVlanSegment", "CloudNetworkSegment"}
        and obj.item.get("security_zone")
        and not is_exchange_zone(obj.item["security_zone"])
        and name(obj) in zoned
    ]
    assert not offenders, "\n".join(offenders)


def test_segments_no_generator_touches_keep_their_zone() -> None:
    """A segment no generator zones (an L2-only colocation VLAN, a cloud subnet without an app) must state it,
    or checks/segment_security.py reports it zoneless."""
    zoned = _generator_zoned_segments()
    missing = [
        f"{obj.path.relative_to(ROOT)}: {name(obj)}"
        for obj in declared_in(_ALL_DEMO)
        if obj.kind in {"ManagedVlanSegment", "CloudNetworkSegment"}
        and name(obj) not in zoned
        and not obj.item.get("security_zone")
    ]
    assert not missing, "\n".join(missing)


def test_no_tenant_vlan_segment_sits_in_a_data_center_footprint() -> None:
    """Inside a DC every tenant segment is VXLAN (checks/segment_placement.py): only an exchange-zone
    service segment (the MANAGEMENT services) may be a VLAN segment on a TopologyCustomerDC."""
    dc_footprints = {name(obj) for obj in declared("TopologyCustomerDC", _ALL_DEMO)}
    vlan_segments = declared("ManagedVlanSegment", _ALL_DEMO)
    assert any(obj.item.get("customer_deployment") in dc_footprints for obj in vlan_segments), (
        "the scan found no VLAN segment in a DC footprint (the MANAGEMENT services segment is one)"
    )
    offenders = [
        f"{obj.path.relative_to(ROOT)}: {name(obj)} on {obj.item['customer_deployment']}"
        for obj in vlan_segments
        if obj.item.get("customer_deployment") in dc_footprints and not is_exchange_zone(obj.item.get("security_zone"))
    ]
    assert not offenders, "tenant VLAN segments in a DC footprint:\n" + "\n".join(offenders)
