"""Leaf fixtures that carry IPv6 segments (a segment has ONE gateway of ONE family, ``ip_version``).

The IPv4 fixtures are the base: the text of their input swaps each segment's addresses for IPv6 ones (a ULA /64 per
segment, the gateway on its ``::1``), so the SVI, the VRF's IPv6 family, the ACL (with the internet band) and the
routing-identity origination are rendered in IPv6 form. ``dual_stack`` turns only seg-100 into IPv6: the VRF then
carries both families on one device. No demo segment is IPv6, so no existing golden contains these.
"""

from __future__ import annotations

import json

from tests.smoke.fixture_builders.common import write_data_fixtures
from tests.smoke.fixture_builders.fabric import build_device_data
from tests.smoke.fixture_builders.firewall import build_firewall_data, build_leaf_internet_permit_data
from transforms.config.firewall import Firewall
from transforms.config.leaf import Leaf

FIREWALL_PLATFORMS = ("paloalto_panos", "checkpoint_gaia", "juniper_junos")
PLATFORMS = ("cisco_nxos", "arista_eos", "cisco_xe", "sonic", "dell_sonic")

# IPv4 address -> IPv6 address of each fixture segment (gateway on the first address, prefix of the segment).
_SEG_100 = {"10.100.0.1/24": "fd5a:1760:0:100::1/64", "10.100.0.0/24": "fd5a:1760:0:100::/64"}
_SEG_200 = {"10.200.0.1/24": "fd5a:1760:0:200::1/64", "10.200.0.0/24": "fd5a:1760:0:200::/64"}


def _to_ipv6(data: dict, *swaps: dict[str, str]) -> dict:
    """The input with every address of ``swaps`` replaced (text swap over the whole GraphQL response)."""
    text = json.dumps(data)
    for swap in swaps:
        for old, new in swap.items():
            text = text.replace(f'"{old}"', f'"{new}"')
    return json.loads(text)


def build_ipv6_leaf_data(*, device_name: str, platform: str) -> dict:
    """The internet-permit leaf (ACL with an internet band) whose two segments are both IPv6."""
    data = build_leaf_internet_permit_data(device_name=device_name, platform=platform)
    return _to_ipv6(data, _SEG_100, _SEG_200)


def build_dual_stack_leaf_data(*, device_name: str, platform: str) -> dict:
    """seg-100 IPv6 and seg-200 IPv4 in the same VRF, both tagged for routing identity: one device, both families."""
    data = build_device_data(
        device_name=device_name,
        role="leaf",
        platform=platform,
        scenario="ebgp_ibgp",
        include_segments=True,
        include_acls=True,
        vlan_segment_vrf=True,
        routing_identity=True,
    )
    return _to_ipv6(data, _SEG_100)


# The firewall fixture's NONPROD segment and its interface, as IPv6 (the PROD zone stays IPv4: one firewall, both families).
_FW_NONPROD = {"10.0.2.0/24": "fd5a:1760:0:2::/64", "10.0.2.1/30": "fd5a:1760:0:2::1/126"}
_FW_PROD = {
    "10.0.1.0/24": "fd5a:1760:0:1::/64",
    "10.0.10.0/24": "fd5a:1760:0:10::/64",
    "10.0.1.1/30": "fd5a:1760:0:1::1/126",
}


def build_firewall_ipv6_data(*, device_name: str, platform: str, ipv6_only: bool = False) -> dict:
    """The firewall with a rule table where NONPROD is IPv6 (``ipv6_only``: PROD too), the other zone IPv4."""
    data = build_firewall_data(device_name=device_name, platform=platform)
    return _to_ipv6(data, _FW_NONPROD, *([_FW_PROD] if ipv6_only else []))


def write_fixtures() -> tuple[int, int]:
    """Write the IPv6 fixtures: every leaf platform with IPv6 segments, NX-OS with both families, each firewall with both."""
    print("\nGenerating IPv6 segment fixtures (leaf):")
    fixtures: list[tuple[type, str, dict]] = [
        (Leaf, f"leaf_{platform}_ipv6_segments", build_ipv6_leaf_data(device_name="dc1-leaf-01", platform=platform))
        for platform in PLATFORMS
    ]
    fixtures.append(
        (
            Leaf,
            "leaf_cisco_nxos_dual_stack",
            build_dual_stack_leaf_data(device_name="dc1-leaf-01", platform="cisco_nxos"),
        )
    )
    fixtures += [
        (
            Firewall,
            f"firewall_{platform}_dual_stack",
            build_firewall_ipv6_data(device_name="dc1-fw-01", platform=platform),
        )
        for platform in FIREWALL_PLATFORMS
    ]
    return write_data_fixtures(fixtures)
