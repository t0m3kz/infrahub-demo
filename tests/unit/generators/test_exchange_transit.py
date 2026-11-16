"""Unit tests for the exchange transit addressing plan."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
import yaml

from utils.exchange_transit import (
    EXCHANGE_PEERS,
    MANAGEMENT_TYPE,
    TENANT_NAMESPACE_TYPES,
    TRANSIT_SLOT,
    TRANSIT_VNI_MAX,
    TRANSIT_VNI_MIN,
    WAN_EXIT_VLAN_BASE,
    is_routed_tenant_segment,
    joins_prod_and_nonprod,
    namespace_type_for_environment,
    transit_addresses,
    transit_vlan,
    transit_vni,
    wan_exit_vlan,
    zone_name_for_namespace_type,
)
from utils.limits import (
    FW_CONTEXT_VLAN_START,
    LB_CONTEXT_VLAN_END,
    MAX_ENCODABLE_VNI,
    TRANSIT_VLAN_BAND,
)
from utils.routing_domain import RoutingDomainError


def test_slots_are_unique() -> None:
    """Each namespace type owns its own VLAN slot."""
    assert len(set(TRANSIT_SLOT.values())) == len(TRANSIT_SLOT)


def test_slot_vlan_ranges_are_disjoint() -> None:
    """The context VLAN pool is narrower than the slot distance, so slots never overlap."""
    assert LB_CONTEXT_VLAN_END - FW_CONTEXT_VLAN_START < TRANSIT_VLAN_BAND
    seen: set[int] = set()
    for ns_type in TRANSIT_SLOT:
        vlans = {transit_vlan(v, ns_type) for v in range(FW_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END + 1)}
        assert not vlans & seen
        seen |= vlans
    assert min(seen) == 3000
    assert max(seen) == 3799


def test_transit_vlan_stable() -> None:
    """Same inputs always give the same VLAN."""
    assert transit_vlan(3005, "prod") == 3005
    assert transit_vlan(3005, "non_prod") == 3205
    assert transit_vlan(3005, "internet") == 3405
    assert transit_vlan(3005, "management") == 3605


def test_transit_vni_band_is_encodable_and_matches_constants() -> None:
    """Transit VNIs stay at or below 65535 and match the published band."""
    assert transit_vni(3000) == TRANSIT_VNI_MIN == 64000
    assert transit_vni(3799) == TRANSIT_VNI_MAX == 64799
    assert TRANSIT_VNI_MAX <= MAX_ENCODABLE_VNI


def test_transit_addresses_offsets() -> None:
    """.1 anycast, .4 VIP, .5/.6 members; .2/.3 are never handed out."""
    result = transit_addresses("100.66.0.8/29")
    assert result == {"anycast": "100.66.0.9", "vip": "100.66.0.12", "members": ["100.66.0.13", "100.66.0.14"]}
    used = {result["anycast"], result["vip"], *result["members"]}
    assert "100.66.0.10" not in used
    assert "100.66.0.11" not in used
    assert all(ipaddress.ip_address(a) in ipaddress.ip_network("100.66.0.8/29") for a in used)


def test_exchange_peers_never_pair_prod_with_non_prod() -> None:
    """PROD and NONPROD must only ever exchange with INTERNET and MANAGEMENT."""
    for ns_type, peers in EXCHANGE_PEERS.items():
        assert ns_type not in peers
        assert ns_type in {"prod", "non_prod"}
        assert peers == ("internet", "management")
    assert "non_prod" not in EXCHANGE_PEERS["prod"]
    assert "prod" not in EXCHANGE_PEERS["non_prod"]


@pytest.mark.parametrize(
    ("env_a", "env_b", "joins"),
    [
        ("p", "p", False),
        ("s", "d", False),
        ("n", "t", False),
        ("p", "s", True),
        ("d", "p", True),
        ("p", "", True),
        (None, "p", True),
        (None, None, True),
        ("", "s", True),
    ],
)
def test_joins_prod_and_nonprod(env_a: str | None, env_b: str | None, joins: bool) -> None:
    """Prod vs nonprod joins; a missing environment fails closed."""
    assert joins_prod_and_nonprod(env_a, env_b) is joins


@pytest.mark.parametrize(
    ("environment", "ns_type", "zone"),
    [
        ("p", "prod", "PROD"),
        ("n", "non_prod", "NONPROD"),
        ("s", "non_prod", "NONPROD"),
        ("d", "non_prod", "NONPROD"),
        ("t", "non_prod", "NONPROD"),
    ],
)
def test_environment_to_namespace_type_and_zone(environment: str, ns_type: str, zone: str) -> None:
    """`p` is prod, everything else non_prod."""
    assert namespace_type_for_environment(environment) == ns_type
    assert zone_name_for_namespace_type(ns_type) == zone


def test_internet_zone_name() -> None:
    """The INTERNET namespace maps to INTERNET."""
    assert zone_name_for_namespace_type("internet") == "INTERNET"


def test_management_zone_and_type() -> None:
    """The MANAGEMENT namespace maps to MANAGEMENT; the type constant is the vrf_type value."""
    assert MANAGEMENT_TYPE == "management"
    assert zone_name_for_namespace_type(MANAGEMENT_TYPE) == "MANAGEMENT"
    assert TRANSIT_SLOT[MANAGEMENT_TYPE] == 3


def _bootstrap_objects(root_dir: Path, name: str) -> dict[str, dict]:
    """Every object of a bootstrap file keyed by name."""
    docs = yaml.safe_load_all((root_dir / "data" / "bootstrap" / name).read_text())
    return {item["name"]: item for doc in docs for item in doc["spec"]["data"] if "name" in item}


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("192.0.2.5/32", True),
        ("8.8.8.8", True),
        ("10.0.0.1/32", False),
        ("100.66.32.5/29", False),
        ("127.0.0.1", False),
        ("169.254.0.1", False),
        ("172.31.255.1", False),
        ("192.168.1.1", False),
        ("239.1.1.1", False),
        ("198.18.0.1/32", False),
        ("198.19.255.1", False),
        ("100.64.0.1/32", False),
        ("100.127.255.254", False),
        ("192.0.0.9", False),
        ("240.0.0.1", False),
        ("198.20.0.1", True),
        ("100.128.0.1", True),
        ("198.51.100.7", True),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    """Only addresses outside NON_INTERNET_PREFIXES (RFC 1918 / 6598 / 2544 / loopback / link-local / multicast) are public."""
    from utils.exchange_transit import is_public_address

    assert is_public_address(address) is public


def _segment(kind: str, vrf_type: str | None, zone: str | None) -> dict:
    """A cleaned segment with default IPAM identity and explicit zone type."""
    gateway = {"ip_prefix": {"ip_namespace": {"name": "default"}}} if vrf_type else None
    return {
        "typename": kind,
        "gateway": gateway,
        "security_zone": {"name": zone, "vrf_type": vrf_type} if zone else None,
    }


@pytest.mark.parametrize(
    ("segment", "tenant"),
    [
        (_segment("ManagedVxlanSegment", "prod", "PROD"), True),
        (_segment("ManagedVxlanSegment", "non_prod", "NONPROD"), True),
        (_segment("ManagedVxlanSegment", "management", "MANAGEMENT"), False),
        (_segment("ManagedVxlanSegment", "internet", "INTERNET"), False),
        (_segment("ManagedVxlanSegment", "prod", "MANAGEMENT"), False),
        (_segment("ManagedVxlanSegment", None, "PROD"), False),
        (_segment("CloudNetworkSegment", "prod", "PROD"), False),
    ],
    ids=[
        "vxlan-prod",
        "vxlan-nonprod",
        "management",
        "internet",
        "exchange-zone",
        "no-gateway",
        "cloud",
    ],
)
def test_routed_tenant_segment(segment: dict, tenant: bool) -> None:
    """The one tenant-SVI test of L0 and the group tags: VXLAN, PROD/NONPROD gateway, no exchange zone."""
    assert is_routed_tenant_segment(segment) is tenant


def test_routed_segment_without_zone_cannot_infer_it_from_ipam() -> None:
    """Legacy IPAM routing labels cannot supply a missing explicit segment owner."""
    segment = _segment("ManagedVxlanSegment", "prod", None)
    segment["gateway"]["ip_prefix"]["ip_namespace"] = {"name": "PROD", "vrf_type": "prod"}

    with pytest.raises(RoutingDomainError, match="missing security zone"):
        is_routed_tenant_segment(segment)


def test_tenant_namespace_types_are_the_exchange_peers_tenants() -> None:
    """One source: the tenant VRFs are the namespace types that exchange with others."""
    assert TENANT_NAMESPACE_TYPES == frozenset({"prod", "non_prod"}) == frozenset(EXCHANGE_PEERS)


def test_wan_exit_vlans_follow_the_slots_just_after_the_transit_band() -> None:
    assert WAN_EXIT_VLAN_BASE == 3800
    assert {ns_type: wan_exit_vlan(ns_type) for ns_type in TRANSIT_SLOT} == {
        "prod": 3800,
        "non_prod": 3801,
        "internet": 3802,
        "management": 3803,
    }


def test_wan_exit_vlans_collide_with_nothing_else_on_a_switch() -> None:
    """Not a customer VLAN (100-2999), not a transit leg (3000-3799), not an L3 VNI SVI (3900-3967)."""
    from utils.limits import CUSTOMER_VLAN_ID_MAX, L3VNI_SVI_VLAN_BASE

    tags = {wan_exit_vlan(ns_type) for ns_type in TRANSIT_SLOT}
    assert all(tag > CUSTOMER_VLAN_ID_MAX for tag in tags)
    assert all(tag > LB_CONTEXT_VLAN_END + TRANSIT_VLAN_BAND * max(TRANSIT_SLOT.values()) for tag in tags)
    assert all(tag < L3VNI_SVI_VLAN_BASE for tag in tags)
