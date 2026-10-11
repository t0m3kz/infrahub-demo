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
    TRANSIT_SLOT,
    TRANSIT_VLAN_BAND,
    TRANSIT_VNI_MAX,
    TRANSIT_VNI_MIN,
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
    """PROD and NON-PROD must only ever exchange with INTERNET."""
    for ns_type, peers in EXCHANGE_PEERS.items():
        assert ns_type not in peers
        assert ns_type in {"prod", "non_prod"}
        assert peers == ("internet",)
    assert "non_prod" not in EXCHANGE_PEERS["prod"]
    assert "prod" not in EXCHANGE_PEERS["non_prod"]


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
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    """Only addresses outside RFC 1918 / 6598 / loopback / link-local / multicast are public."""
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
