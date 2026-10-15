"""Unit tests for the exchange transit addressing plan."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
import yaml

from utils.exchange_transit import (
    CONTEXT_VLAN_MAX,
    CONTEXT_VLAN_MIN,
    EXCHANGE_PEERS,
    MANAGEMENT_TYPE,
    TENANT_NAMESPACE_TYPES,
    TRANSIT_SLOT,
    TRANSIT_VLAN_BAND,
    TRANSIT_VNI_MAX,
    TRANSIT_VNI_MIN,
    ZONE_BY_NS_TYPE,
    is_routed_tenant_segment,
    joins_prod_and_nonprod,
    namespace_type_for_environment,
    transit_addresses,
    transit_vlan,
    transit_vni,
    zone_name_for_namespace_type,
)

MAX_ENCODABLE_VNI = 65535


def test_slots_are_unique() -> None:
    """Each namespace type owns its own VLAN slot."""
    assert len(set(TRANSIT_SLOT.values())) == len(TRANSIT_SLOT)


def test_slot_vlan_ranges_are_disjoint() -> None:
    """The context VLAN pool is narrower than the slot distance, so slots never overlap."""
    assert CONTEXT_VLAN_MAX - CONTEXT_VLAN_MIN < TRANSIT_VLAN_BAND
    seen: set[int] = set()
    for ns_type in TRANSIT_SLOT:
        vlans = {transit_vlan(v, ns_type) for v in range(CONTEXT_VLAN_MIN, CONTEXT_VLAN_MAX + 1)}
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


def test_transit_vni_band_disjoint_from_bootstrap_vni_ranges(root_dir: Path) -> None:
    """The band must not overlap any shipped number pool nor the fixed namespace L3 VNIs."""
    pools: list[tuple[str, int, int]] = []
    for path in (root_dir / "data" / "bootstrap" / "18_vni_pools.yml",):
        for doc in yaml.safe_load_all(path.read_text()):
            spec = (doc or {}).get("spec") or {}
            if spec.get("kind") == "CoreNumberPool":
                pools.extend((p["name"], p["start_range"], p["end_range"]) for p in spec["data"])
    assert pools
    for name, start, end in pools:
        assert end < TRANSIT_VNI_MIN or start > TRANSIT_VNI_MAX, f"pool {name} overlaps transit VNI band"

    namespaces = yaml.safe_load((root_dir / "data" / "bootstrap" / "22_namespaces.yml").read_text())
    for ns in namespaces["spec"]["data"]:
        assert not TRANSIT_VNI_MIN <= ns["l3_vni"] <= TRANSIT_VNI_MAX


def test_transit_vni_band_documented_in_namespace_table(root_dir: Path) -> None:
    """The VNI band comment table in 22_namespaces.yml lists the transit band."""
    text = (root_dir / "data" / "bootstrap" / "22_namespaces.yml").read_text()
    assert f"{TRANSIT_VNI_MIN}-{TRANSIT_VNI_MAX}" in text


def test_transit_addresses_offsets() -> None:
    """.1 anycast, .4 VIP, .5/.6 members; .2/.3 are never handed out."""
    result = transit_addresses("100.66.0.8/29")
    assert result == {"anycast": "100.66.0.9", "vip": "100.66.0.12", "members": ["100.66.0.13", "100.66.0.14"]}
    used = {result["anycast"], result["vip"], *result["members"]}
    assert "100.66.0.10" not in used
    assert "100.66.0.11" not in used
    assert all(ipaddress.ip_address(a) in ipaddress.ip_network("100.66.0.8/29") for a in used)


def test_exchange_peers_never_pair_prod_with_non_prod() -> None:
    """PROD and NON-PROD must only ever exchange with INTERNET and MANAGEMENT."""
    for ns_type, peers in EXCHANGE_PEERS.items():
        assert ns_type not in peers
        assert ns_type in {"prod", "non_prod"}
        assert peers == ("internet", "management")
    assert "non_prod" not in EXCHANGE_PEERS["prod"]
    assert "prod" not in EXCHANGE_PEERS["non_prod"]


def test_exchange_peers_never_join_prod_and_non_prod() -> None:
    """Property: no tenant type is paired with a peer of the other tenant side, nor with itself."""
    tenant_types = set(EXCHANGE_PEERS)
    for tenant, peers in EXCHANGE_PEERS.items():
        for peer in peers:
            assert peer not in tenant_types
        other = tenant_types - {tenant}
        assert not other & set(peers)
    assert {t for t in tenant_types if t == "prod"} and {t for t in tenant_types if t == "non_prod"}


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
    """Prod vs non-prod joins; a missing environment fails closed."""
    assert joins_prod_and_nonprod(env_a, env_b) is joins


@pytest.mark.parametrize(
    ("environment", "ns_type", "zone"),
    [
        ("p", "prod", "PROD-ZONE"),
        ("n", "non_prod", "NONPROD-ZONE"),
        ("s", "non_prod", "NONPROD-ZONE"),
        ("d", "non_prod", "NONPROD-ZONE"),
        ("t", "non_prod", "NONPROD-ZONE"),
    ],
)
def test_environment_to_namespace_type_and_zone(environment: str, ns_type: str, zone: str) -> None:
    """`p` is prod, everything else non_prod."""
    assert namespace_type_for_environment(environment) == ns_type
    assert zone_name_for_namespace_type(ns_type) == zone


def test_internet_zone_name() -> None:
    """The INTERNET namespace maps to INTERNET-ZONE."""
    assert zone_name_for_namespace_type("internet") == "INTERNET-ZONE"


def test_management_zone_and_type() -> None:
    """The MANAGEMENT namespace maps to MANAGEMENT-ZONE; the type constant is the namespace_type value."""
    assert MANAGEMENT_TYPE == "management"
    assert zone_name_for_namespace_type(MANAGEMENT_TYPE) == "MANAGEMENT-ZONE"
    assert TRANSIT_SLOT[MANAGEMENT_TYPE] == 3


def _bootstrap_objects(root_dir: Path, name: str) -> dict[str, dict]:
    """Every object of a bootstrap file keyed by name."""
    docs = yaml.safe_load_all((root_dir / "data" / "bootstrap" / name).read_text())
    return {item["name"]: item for doc in docs for item in doc["spec"]["data"] if "name" in item}


def test_every_exchange_peer_has_a_namespace_zone_and_transit_pool(root_dir: Path) -> None:
    """Each peer type of EXCHANGE_PEERS has a namespace, a SecurityZone and a FW-Transit-<NS>-IPv4 pool in it."""
    namespaces = _bootstrap_objects(root_dir, "22_namespaces.yml")
    zones = _bootstrap_objects(root_dir, "24_security_zones.yml")
    pools = _bootstrap_objects(root_dir, "23_transit_pools.yml")
    by_type = {ns["namespace_type"]: name for name, ns in namespaces.items()}
    for ns_type in {peer for peers in EXCHANGE_PEERS.values() for peer in peers}:
        ns_name = by_type[ns_type]
        assert ZONE_BY_NS_TYPE[ns_type] in zones
        assert pools[f"FW-Transit-{ns_name}-IPv4"]["ip_namespace"] == ns_name


def test_management_transit_pool_and_zone(root_dir: Path) -> None:
    """MANAGEMENT: /29 legs out of 100.66.48.0/20 in its own namespace; zone trust level 90."""
    pool = _bootstrap_objects(root_dir, "23_transit_pools.yml")["FW-Transit-MANAGEMENT-IPv4"]
    resource = pool["resources"]["data"][0]
    assert (pool["default_prefix_length"], resource["prefix"], resource["ip_namespace"]) == (
        29,
        "100.66.48.0/20",
        "MANAGEMENT",
    )
    assert _bootstrap_objects(root_dir, "24_security_zones.yml")["MANAGEMENT-ZONE"]["trust_level"] == 90


def test_services_segment_and_its_policy_rules(root_dir: Path) -> None:
    """The INFRA services segment: routed in MANAGEMENT, zoned, pinned to one leaf port; PROD permits name it."""
    spec = yaml.safe_load((root_dir / "data/demos/30_all/07_applications/infra/00_segment.yml").read_text())["spec"]
    customer = spec["data"][0]
    segment = customer["network_segments"][0]["data"]
    gateway = segment["gateway"]["data"]
    assert customer["org_id"] == "INFRA"
    assert (segment["environment"], segment["security_zone"]) == ("p", "MANAGEMENT-ZONE")
    assert segment["customer_deployment"] == "C006-P-DC11"
    assert (gateway["ip_namespace"], gateway["ip_prefix"]["data"]["ip_namespace"]) == ("MANAGEMENT", "MANAGEMENT")
    assert ipaddress.ip_interface(gateway["address"]).network == ipaddress.ip_network(
        gateway["ip_prefix"]["data"]["prefix"]
    )
    assert len(segment["interface_capabilities"]) == 1
    # Computed segment name: {owner org_id}-{customer_name}-{environment}, lowercased.
    name = f"{customer['org_id']}-{segment['customer_name']}-{segment['environment']}".lower()

    policy_file = root_dir / "data/demos/30_all/08_interconnects/07_zone_policies/05_security_policy.yml"
    rules = {
        rule["name"]: rule
        for doc in yaml.safe_load_all(policy_file.read_text())
        for policy in doc["spec"]["data"]
        for rule in policy["rules"]["data"]
    }
    for rule_name, port in (("nordix-prod-to-core-dns", 53), ("nordix-prod-to-core-ntp", 123)):
        rule = rules[rule_name]
        assert (rule["action"], rule["protocol"], rule["port_start"]) == ("permit", "udp", port)
        assert rule["destination_segment"] == name
        assert rule["apply_on_switch"] is False
        assert rule["index"] < 100


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


def test_snat_pool_and_context_relationship_are_declared(root_dir: Path) -> None:
    """Bootstrap pool: public TEST-NET-1 prefix in INTERNET; schema: optional cardinality-one IpamIPAddress peer."""
    docs = list(yaml.safe_load_all((root_dir / "data" / "bootstrap" / "23_transit_pools.yml").read_text()))
    pool = next(
        item
        for doc in docs
        if doc["spec"]["kind"] == "CoreIPAddressPool"
        for item in doc["spec"]["data"]
        if item["name"] == "FW-SNAT-INTERNET-IPv4"
    )
    prefix = pool["resources"]["data"][0]
    assert (prefix["prefix"], prefix["role"], prefix["ip_namespace"], pool["ip_namespace"]) == (
        "192.0.2.0/24",
        "public",
        "INTERNET",
        "INTERNET",
    )

    schema = yaml.safe_load((root_dir / "schemas" / "extensions" / "capabilities" / "ha.yml").read_text())
    node = next(n for n in schema["nodes"] if n["name"] == "FirewallContext")
    rel = next(r for r in node["relationships"] if r["name"] == "snat_address")
    assert (rel["peer"], rel["cardinality"], rel["kind"], rel["optional"]) == (
        "IpamIPAddress",
        "one",
        "Attribute",
        True,
    )
    assert rel["identifier"] == "firewall_context__snat_address"
    assert rel["order_weight"] >= 1000


def _segment(kind: str, namespace_type: str | None, zone: str | None) -> dict:
    """A cleaned segment with its gateway namespace type and zone."""
    gateway = {"ip_prefix": {"ip_namespace": {"namespace_type": namespace_type}}} if namespace_type else None
    return {"typename": kind, "gateway": gateway, "security_zone": {"name": zone} if zone else None}


@pytest.mark.parametrize(
    ("segment", "tenant"),
    [
        (_segment("ManagedVxlanSegment", "prod", "PROD-ZONE"), True),
        (_segment("ManagedVlanSegment", "non_prod", None), True),
        (_segment("ManagedVlanSegment", "management", "MANAGEMENT-ZONE"), False),
        (_segment("ManagedVxlanSegment", "internet", "INTERNET-ZONE"), False),
        (_segment("ManagedVxlanSegment", "prod", "MANAGEMENT-ZONE"), False),
        (_segment("ManagedVxlanSegment", None, "PROD-ZONE"), False),
        (_segment("ManagedExternalSegment", "prod", "PROD-ZONE"), False),
        (_segment("CloudNetworkSegment", "prod", "PROD-ZONE"), False),
    ],
    ids=["vxlan-prod", "vlan-nonprod", "management", "internet", "exchange-zone", "no-gateway", "external", "cloud"],
)
def test_routed_tenant_segment(segment: dict, tenant: bool) -> None:
    """The one tenant-SVI test of L0 and the group tags: VLAN/VXLAN, PROD/NON-PROD gateway, no exchange zone."""
    assert is_routed_tenant_segment(segment) is tenant


def test_tenant_namespace_types_are_the_exchange_peers_tenants() -> None:
    """One source: the tenant VRFs are the namespace types that exchange with others."""
    assert TENANT_NAMESPACE_TYPES == frozenset({"prod", "non_prod"}) == frozenset(EXCHANGE_PEERS)
