"""Unit tests for the CheckEvpnFabric site-identity and router-id rules, on raw-shape fixtures run through clean_data."""

from __future__ import annotations

from typing import Any, cast

from checks.evpn_fabric import (
    CheckEvpnFabric,
    duplicate_rt_as_errors,
    stretched_management_errors,
    vtep_router_id_errors,
)
from tests.unit.vrf_instances_helpers import edges
from utils.data_cleaning import clean_data
from utils.limits import DEFAULT_NAMESPACE

_ENVIRONMENTS = (
    ("PROD", "prod"),
    ("NON-PROD", "non_prod"),
    ("INTERNET", "internet"),
    ("MANAGEMENT", "management"),
)


def _rt_as(asn: int | None) -> dict[str, Any] | None:
    """A raw ``evpn_rt_as`` relationship."""
    return {"node": {"asn": {"value": asn}}} if asn is not None else None


def _site(name: str, asn: int | None) -> dict[str, Any]:
    """A cleaned data center or metro."""
    return clean_data({"id": f"id-{name}", "name": {"value": name}, "evpn_rt_as": _rt_as(asn)})


def _segment(name: str, scope: str | None, ns_type: str | None) -> dict[str, Any]:
    """A raw ManagedVxlanSegment node."""
    namespace = {"node": {"name": {"value": "NS"}, "namespace_type": {"value": ns_type}}}
    gateway = {"node": {"ip_prefix": {"node": {"ip_namespace": namespace}}}} if ns_type else None
    return {"id": f"seg-{name}", "name": {"value": name}, "stretch_scope": {"value": scope}, "gateway": gateway}


def _vrf(namespace: str, l3_vni: int | None = 5000, ns_type: str | None = "prod") -> dict[str, Any]:
    """A raw ManagedVRF capability."""
    ns = {"name": {"value": namespace}, "l3_vni": {"value": l3_vni}, "namespace_type": {"value": ns_type}}
    return {"__typename": "ManagedVRF", "namespace": {"node": ns}}


def _overlay(router_id: str | None) -> dict[str, Any]:
    """A raw overlay ManagedBGP capability."""
    return {
        "__typename": "ManagedBGP",
        "name": {"value": "bgp"},
        "process_role": {"value": "overlay"},
        "router_id": {"node": {"address": {"value": router_id}}} if router_id else None,
    }


def _iface(role: str, vrf_namespace: str | None = None, contexts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A raw interface: an SVI in a VRF, a multisite-vip, or a service port tagged with contexts."""
    node: dict[str, Any] = {
        "__typename": "DcimVirtualInterface" if vrf_namespace else "DcimPhysicalInterface",
        "id": f"if-{role}",
        "role": {"value": role},
        "capabilities": edges(*(contexts or [])),
    }
    if vrf_namespace:
        node["vrf"] = {"node": {"namespace": {"node": {"name": {"value": vrf_namespace}}}}}
    return node


def _leg_context(namespace: str, ns_type: str) -> dict[str, Any]:
    """A raw firewall context with one transit leg on a firewall member."""
    leg = {
        "device": {"node": {"role": {"value": "firewall"}}},
        "ip_address": {
            "node": {
                "address": {"value": "10.1.0.1/29"},
                "ip_namespace": {"node": {"name": {"value": namespace}, "namespace_type": {"value": ns_type}}},
            }
        },
    }
    return {
        "__typename": "ManagedFirewallContext",
        "id": "ctx-1",
        "vlan_id": {"value": 100},
        "capabilities": edges(leg),
    }


def _device(
    name: str,
    role: str = "leaf",
    vrfs: tuple[dict[str, Any], ...] = (),
    router_id: str | None = None,
    interfaces: tuple[dict[str, Any], ...] = (),
    site_asn: int | None = None,
) -> dict[str, Any]:
    """A cleaned DcimDevice node of queries/validation/evpn_fabric.gql."""
    capabilities = [*vrfs, *([_overlay(router_id)] if router_id else [])]
    return clean_data(
        {
            "id": f"id-{name}",
            "name": {"value": name},
            "role": {"value": role},
            "deployment": {"node": {"evpn_rt_as": _rt_as(site_asn)}},
            "capabilities": edges(*capabilities),
            "interfaces": edges(*interfaces),
        }
    )


def _vtep(name: str, router_id: str | None, role: str = "leaf") -> dict[str, Any]:
    """A VTEP whose PROD instance is realized: it has an anycast SVI in PROD."""
    return _device(name, role, (_vrf("PROD"),), router_id, (_iface("svi", "PROD"),))


def test_distinct_rt_as_per_site_is_clean() -> None:
    """Different ASNs, and sites without one, raise nothing."""
    assert duplicate_rt_as_errors([_site("DC1", 65001), _site("DC2", None)], [_site("M1", 65002)]) == []


def test_two_sites_sharing_an_rt_as_is_an_error() -> None:
    """A data center and a metro on one ASN."""
    errors = duplicate_rt_as_errors([_site("DC1", 65001)], [_site("M1", 65001)])
    assert len(errors) == 1 and "DC1" in errors[0] and "M1" in errors[0]


def test_stretched_segment_in_management_is_an_error() -> None:
    """A non-local scope on a management gateway fails; local, unset or other types pass."""

    def run(*args: Any) -> list[str]:
        return stretched_management_errors([clean_data(_segment(*args))])

    assert len(run("oob", "dc_pair", "management")) == 1
    assert run("oob", "local", "management") == []
    assert run("oob", None, "management") == []
    assert run("web", "global", "prod") == []
    assert run("l2", "global", None) == []


def test_vtep_with_a_realized_l3_vni_needs_a_router_id() -> None:
    """No overlay process or an empty router_id fails; a spine is out of scope."""
    assert vtep_router_id_errors([_vtep("leaf-1", "10.0.0.1/32")]) == []
    assert len(vtep_router_id_errors([_vtep("leaf-1", None)])) == 1
    assert vtep_router_id_errors([_vtep("spine-1", None, role="spine")]) == []


def test_instance_without_an_l3_vni_or_in_default_is_not_realized() -> None:
    """An SVI in a namespace without an l3_vni, or an instance of default, has no RD."""
    no_vni = _device("leaf-1", "leaf", (_vrf("PROD", l3_vni=None),), None, (_iface("svi", "PROD"),))
    default = _device("leaf-2", "leaf", (_vrf(DEFAULT_NAMESPACE),), None, (_iface("svi", DEFAULT_NAMESPACE),))
    assert vtep_router_id_errors([no_vni, default]) == []


def test_vrf_lite_edge_needs_no_router_id() -> None:
    """An edge with all four instances but only sessions and addresses (no SVI, no BGW) is VRF-lite."""
    vrfs = tuple(_vrf(name, 5000 + i, ns_type) for i, (name, ns_type) in enumerate(_ENVIRONMENTS))
    assert vtep_router_id_errors([_device("edge-1", "edge", vrfs, None, site_asn=65001)]) == []


def test_border_gateway_edge_with_an_internet_instance_needs_a_router_id() -> None:
    """A multisite-vip edge exits through its INTERNET instance as an L3 VNI, with no SVI of its own."""
    internet = _vrf("INTERNET", 5002, "internet")
    bgw = _device("edge-1", "edge", (internet,), None, (_iface("multisite-vip"),), site_asn=65001)
    assert len(vtep_router_id_errors([bgw])) == 1
    with_id = _device("edge-1", "edge", (internet,), "10.0.0.9/32", (_iface("multisite-vip"),), site_asn=65001)
    assert vtep_router_id_errors([with_id]) == []


def test_multisite_vip_without_a_site_rt_as_is_not_a_border_gateway() -> None:
    """The transform's rule: a multisite-vip with no evpn_rt_as is not a BGW, so the instance stays VRF-lite."""
    internet = _vrf("INTERNET", 5002, "internet")
    assert vtep_router_id_errors([_device("edge-1", "edge", (internet,), None, (_iface("multisite-vip"),))]) == []


def test_site_rt_as_is_read_from_the_parent_of_a_pod() -> None:
    """A pod-tier device reaches the DC's evpn_rt_as through deployment.parent."""
    internet = _vrf("INTERNET", 5002, "internet")
    raw = {
        "id": "d",
        "name": {"value": "edge-1"},
        "role": {"value": "edge"},
        "deployment": {"node": {"evpn_rt_as": None, "parent": {"node": {"evpn_rt_as": _rt_as(65001)}}}},
        "capabilities": edges(internet),
        "interfaces": edges(_iface("multisite-vip")),
    }
    assert len(vtep_router_id_errors([clean_data(raw)])) == 1


def test_transit_leg_on_a_service_port_realizes_the_instance() -> None:
    """A border leaf routes a firewall context leg: that is an anycast SVI in the leg's namespace."""
    port = _iface("firewall", contexts=[_leg_context("PROD", "prod")])
    border = _device("bl-1", "border-leaf", (_vrf("PROD"),), None, (port,))
    assert len(vtep_router_id_errors([border])) == 1


def test_shared_router_id_is_an_error() -> None:
    """The same router id (prefix length ignored) on two VTEPs; others stay clean."""
    devices = [_vtep("leaf-1", "10.0.0.1/32"), _vtep("leaf-2", "10.0.0.1"), _vtep("leaf-3", "10.0.0.3/32")]
    errors = vtep_router_id_errors(devices)
    assert len(errors) == 1 and "leaf-1" in errors[0] and "leaf-2" in errors[0]


def test_check_class_reports_the_new_rules() -> None:
    """End to end through validate() on a raw response."""
    check = cast(Any, CheckEvpnFabric.__new__(CheckEvpnFabric))
    errors: list[str] = []
    check.log_error = lambda message, **_: errors.append(message)
    check.log_info = lambda message, **_: None
    raw_leaf = {
        "id": "d1",
        "name": {"value": "leaf-1"},
        "role": {"value": "leaf"},
        "deployment": {"node": {"evpn_rt_as": None}},
        "capabilities": edges(_vrf("PROD")),
        "interfaces": edges(_iface("svi", "PROD")),
    }
    check.validate(
        {
            "TopologyDataCenter": edges(
                {"id": "1", "name": {"value": "DC1"}, "routing_strategy": {"value": "ibgp"}, "evpn_rt_as": _rt_as(1)}
            ),
            "TopologyColocationMetro": edges({"id": "2", "name": {"value": "M1"}, "evpn_rt_as": _rt_as(1)}),
            "ManagedVxlanSegment": edges(_segment("oob", "global", "management")),
            "DcimDevice": edges(raw_leaf),
        }
    )
    assert len(errors) == 3
