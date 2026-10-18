"""Unit tests for 4d: SNAT rendering per firewall vendor and the edge's public-space export."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from transforms.config.edge import Edge
from transforms.config.firewall import Firewall, apply_context_routes
from transforms.helpers.bgp import get_internet_prefixes

_ROOT = Path(__file__).parent.parent.parent
_CONFIGS = _ROOT / "tests" / "smoke" / "configs"

# vendor -> substrings that must appear for the shared context (SNAT 192.0.2.5, PROD + NONPROD tenants)
_VENDOR_NAT = {
    "paloalto_panos": [
        "set vsys dc1-fw-shared rulebase nat rules snat-PROD-ZONE to INTERNET-ZONE",
        "source-translation dynamic-ip-and-port translated-address 192.0.2.5",
        "units ethernet1/1.3400 ip 192.0.2.5/32",
    ],
    "fortinet_fortios": [
        'edit "SNAT-dc1-fw-shared"',
        "set startip 192.0.2.5",
        "set ippool enable",
        'set poolname "SNAT-dc1-fw-shared"',
        'set dstintf "INTERNET-ZONE"',
    ],
    "cisco_asa": [
        " host 192.0.2.5",
        " nat (prod,internet) dynamic pat-pool SNAT-dc1-fw-shared",
        " nat (nonprod,internet) dynamic pat-pool SNAT-dc1-fw-shared",
    ],
    "juniper_junos": [
        "security nat source pool SNAT-POOL address 192.0.2.5/32",
        "rule-set SNAT-PROD-ZONE to zone INTERNET-ZONE",
        "then source-nat pool SNAT-POOL",
    ],
    "checkpoint_gaia": [
        'mgmt_cli add host name "SNAT-dc1-fw-shared" ip-address "192.0.2.5"',
        'original-source "PROD-ZONE" original-destination "INTERNET-ZONE" translated-source "SNAT-dc1-fw-shared"',
    ],
}
_NAT_MARKERS = ("nat rules", "firewall ippool", "pat-pool", "security nat", "add nat-rule")


def _run(transform_cls: type, data: dict[str, Any]) -> str:
    client = MagicMock()
    client.clone.return_value = client
    client.schema = MagicMock()
    client.schema.get = AsyncMock(return_value=MagicMock())
    instance = transform_cls(client=client, infrahub_node=MagicMock(), root_directory=str(_ROOT))
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def _firewall_input(vendor: str, *, snat: bool) -> dict[str, Any]:
    data = json.loads((_CONFIGS / f"firewall_{vendor}_exchange_transit" / "input.json").read_text())
    if not snat:
        for edge in data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"]:
            for cap in edge["node"].get("capabilities", {}).get("edges", []):
                cap["node"].pop("snat_address", None)
    return data


@pytest.mark.parametrize("vendor", sorted(_VENDOR_NAT))
def test_vendor_renders_source_nat_to_the_context_snat_address(vendor: str) -> None:
    output = _run(Firewall, _firewall_input(vendor, snat=True))
    for expected in _VENDOR_NAT[vendor]:
        assert expected in output


@pytest.mark.parametrize("vendor", sorted(_VENDOR_NAT))
def test_context_without_snat_address_renders_no_internet_nat(vendor: str) -> None:
    output = _run(Firewall, _firewall_input(vendor, snat=False))
    internet_nat = [
        line
        for line in output.splitlines()
        if any(m in line for m in _NAT_MARKERS) and ("INTERNET" in line or "internet" in line)
    ]
    assert not internet_nat
    assert "192.0.2" not in output


def _ctx(snat: str | None, *, internet: bool = True, management: bool = False) -> dict[str, Any]:
    legs = [
        {
            "ns_type": "prod",
            "zone": "PROD-ZONE",
            "nameif": "prod",
            "anycast": "100.66.0.1",
            "interface": "e.3000",
            "vrf": "P",
        }
    ]
    if internet:
        legs.append(
            {
                "ns_type": "internet",
                "zone": "INTERNET-ZONE",
                "nameif": "internet",
                "anycast": "100.66.32.1",
                "interface": "e.3400",
                "vrf": "I",
            }
        )
    if management:
        legs.append(
            {
                "ns_type": "management",
                "zone": "MANAGEMENT-ZONE",
                "nameif": "management",
                "anycast": "100.66.48.1",
                "vip": "100.66.48.4",
                "interface": "e.3600",
                "vrf": "M",
            }
        )
    return {"legs": legs, "segments": [], "snat": snat}


def test_internet_nat_entry_keeps_the_legacy_shape() -> None:
    context = _ctx("192.0.2.5")
    apply_context_routes(context, {})
    [entry] = context["nat"]
    assert entry["address"] == "192.0.2.5"
    assert entry["egress"]["zone"] == "INTERNET-ZONE"
    assert entry["suffix"] == ""
    assert entry["own_address"] is True
    assert entry["open_egress"] is True
    assert [leg["zone"] for leg in entry["tenant_legs"]] == ["PROD-ZONE"]


def test_management_nat_entry_translates_to_the_leg_vip_and_owns_nothing() -> None:
    context = _ctx("192.0.2.5", management=True)
    apply_context_routes(context, {})
    internet, management = context["nat"]
    assert internet["address"] == "192.0.2.5"
    assert management["address"] == "100.66.48.4"
    assert management["egress"]["zone"] == "MANAGEMENT-ZONE"
    assert management["suffix"] == "-MANAGEMENT"
    assert management["own_address"] is False
    assert management["open_egress"] is False
    assert [leg["zone"] for leg in management["tenant_legs"]] == ["PROD-ZONE"]


def test_management_nat_needs_no_snat_address() -> None:
    context = _ctx(None, internet=False, management=True)
    apply_context_routes(context, {})
    assert [entry["egress"]["zone"] for entry in context["nat"]] == ["MANAGEMENT-ZONE"]


@pytest.mark.parametrize("snat, internet", [(None, True), ("192.0.2.5", False)])
def test_no_nat_entry_without_snat_or_internet_leg(snat: str | None, internet: bool) -> None:
    context = _ctx(snat, internet=internet)
    apply_context_routes(context, {})
    assert context["nat"] == []


def test_no_nat_entry_without_a_tenant_leg() -> None:
    context = _ctx("192.0.2.5", management=True)
    context["legs"] = [leg for leg in context["legs"] if leg["ns_type"] != "prod"]
    apply_context_routes(context, {})
    assert context["nat"] == []


# --- MANAGEMENT NAT per vendor (golden input: shared PROD+NONPROD, acme PROD) ----

_VENDOR_MANAGEMENT_NAT = {
    "paloalto_panos": [
        "rulebase nat rules snat-PROD-ZONE-MANAGEMENT to MANAGEMENT-ZONE",
        "rulebase nat rules snat-NONPROD-ZONE-MANAGEMENT source-translation dynamic-ip-and-port translated-address 100.66.48.4",
    ],
    "fortinet_fortios": [
        'edit "SNAT-dc1-fw-shared-MANAGEMENT"',
        "set startip 100.66.48.4",
    ],
    "cisco_asa": [
        " host 100.66.48.4",
        " nat (prod,management) dynamic pat-pool SNAT-dc1-fw-shared-MANAGEMENT",
        " nat (nonprod,management) dynamic pat-pool SNAT-dc1-fw-shared-MANAGEMENT",
    ],
    "juniper_junos": [
        "security nat source pool SNAT-POOL-MANAGEMENT address 100.66.48.4/32",
        "rule-set SNAT-NONPROD-ZONE-MANAGEMENT to zone MANAGEMENT-ZONE",
    ],
    "checkpoint_gaia": [
        'mgmt_cli add host name "SNAT-dc1-fw-shared-MANAGEMENT" ip-address "100.66.48.4"',
        'original-source "NONPROD-ZONE" original-destination "MANAGEMENT-ZONE" translated-source "SNAT-dc1-fw-shared-MANAGEMENT"',
    ],
}


@pytest.mark.parametrize("vendor", sorted(_VENDOR_MANAGEMENT_NAT))
def test_vendor_renders_management_nat_behind_the_leg_vip(vendor: str) -> None:
    output = _run(Firewall, _firewall_input(vendor, snat=True))
    for expected in _VENDOR_MANAGEMENT_NAT[vendor]:
        assert expected in output


@pytest.mark.parametrize("vendor", sorted(_VENDOR_MANAGEMENT_NAT))
def test_management_nat_survives_without_a_snat_address(vendor: str) -> None:
    output = _run(Firewall, _firewall_input(vendor, snat=False))
    for expected in _VENDOR_MANAGEMENT_NAT[vendor]:
        assert expected in output
    assert "192.0.2" not in output


def test_panos_owns_no_secondary_ip_for_the_management_vip() -> None:
    output = _run(Firewall, _firewall_input("paloalto_panos", snat=True))
    assert "units ethernet1/1.3400 ip 192.0.2.5/32" in output
    assert not [line for line in output.splitlines() if line.endswith("ip 100.66.48.4/32")]


def test_fortinet_has_no_accept_all_policy_into_management() -> None:
    """FortiOS ties NAT to a policy: only INTERNET (open egress) gets an accept-all translating policy."""
    output = _run(Firewall, _firewall_input("fortinet_fortios", snat=True))
    assert 'set name "snat-PROD-ZONE-to-MANAGEMENT-ZONE"' not in output
    assert 'set name "snat-PROD-ZONE-to-INTERNET-ZONE"' in output


def test_fortinet_permit_into_management_carries_the_pool() -> None:
    from tests.unit.test_firewall_transform import _render_template

    legs = [
        {
            "interface": "e.3600",
            "parent_interface": {"name": "e"},
            "vlan": 3600,
            "own_ip": "100.66.48.5/29",
            "virtual_ip": None,
            "zone": "MANAGEMENT-ZONE",
        }
    ]
    egress = {"zone": "MANAGEMENT-ZONE"}
    nat = [
        {
            "suffix": "-MANAGEMENT",
            "egress": egress,
            "address": "100.66.48.4",
            "own_address": False,
            "open_egress": False,
            "tenant_legs": [{"zone": "PROD-ZONE"}],
        }
    ]
    rules = [
        {"seq": 20, "name": "dns", "src_zone": "PROD-ZONE", "dst_zone": "MANAGEMENT-ZONE", "action": "permit"},
        {"seq": 30, "name": "web", "src_zone": "PROD-ZONE", "dst_zone": "DMZ", "action": "permit"},
        {"seq": 40, "name": "drop", "src_zone": "PROD-ZONE", "dst_zone": "MANAGEMENT-ZONE", "action": "deny"},
    ]
    context = {
        "name": "c1",
        "legs": legs,
        "routes": [],
        "isolation": [],
        "nat": nat,
        "policies": [{"rules": rules}],
        "tenant_name": None,
    }
    out = _render_template("fortinet_fortios", name="fw1", contexts=[context])
    blocks = out.split("edit ")
    by_name = {b.split("\n")[0]: b for b in blocks if "set name" in b}
    assert 'set poolname "SNAT-c1-MANAGEMENT"' in by_name["20"]
    assert "set poolname" not in by_name["30"]
    assert "set poolname" not in by_name["40"]
    assert "910001" not in out  # no accept-all NAT policy towards a non-open egress


# --- edge ---------------------------------------------------------------------


def test_internet_prefixes_keep_only_public_ipv4_of_the_internet_namespace() -> None:
    roots = [
        {"prefix": "192.0.2.0/24", "ip_namespace": {"namespace_type": "internet"}},
        {"prefix": "198.51.100.0/24", "ip_namespace": {"namespace_type": "prod"}},
        {"prefix": "2001:db8::/32", "ip_namespace": {"namespace_type": "internet"}},
        {"prefix": "192.0.2.0/24", "ip_namespace": {"namespace_type": "internet"}},
    ]
    assert get_internet_prefixes(roots) == ["192.0.2.0/24"]
    assert get_internet_prefixes(None) == []


def _edge_output(*, prefixes: bool) -> str:
    data = json.loads((_CONFIGS / "edge_cisco_nxos_internet_bgw" / "input.json").read_text())
    data = copy.deepcopy(data)
    if not prefixes:
        data.pop("IpamPrefix")
    return _run(Edge, data)


def test_edge_aggregates_and_permits_only_the_public_prefix() -> None:
    output = _edge_output(prefixes=True)
    assert "      aggregate-address 192.0.2.0/24 summary-only" in output
    assert "ip prefix-list PL-TRANSIT-OUT-PUBLIC seq 10 permit 192.0.2.0/24" in output
    assert "route-map RM-TRANSIT-OUT permit 10\n  match ip address prefix-list PL-TRANSIT-OUT-PUBLIC" in output
    assert "route-map RM-TRANSIT-OUT deny 20" in output


def test_edge_without_known_public_prefix_stays_deny_all_outbound() -> None:
    output = _edge_output(prefixes=False)
    assert "aggregate-address" not in output
    assert "route-map RM-TRANSIT-OUT deny 10" in output
    assert "PL-TRANSIT-OUT-PUBLIC" not in output
