"""Routing-protection fixtures: a spine's and a leaf's BGP sessions with the derived maximum-prefix, staged and enforced.

Each fixture is a NEW directory next to the unprotected ones, whose goldens stay as they were: the derivation needs the
hosting deployment's loopback pool (``TopologyDataCenter.loopback_pool``) and its ``routing_filter_enforce`` switch
(utils/bgp_expectation.py, transforms/helpers/bgp_facts.py). Staged (``routing_filter_enforce`` false) renders the
limit as warning-only and the filters unattached; enforced renders them live.
"""

from __future__ import annotations

from tests.smoke.fixture_builders.common import _edges, _node, _v, write_data_fixtures
from tests.smoke.fixture_builders.fabric import build_device_data
from tests.smoke.fixture_builders.route_protection_roots import add_environment_roots
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine

# The loopbacks of the base fixture (10.0.0.1 the device, 10.0.0.100/.101 the spines) sit in this pool.
LOOPBACK_POOL = "10.0.0.0/24"
# A border gateway (dc1-border-01) carries a multisite VIP and exits to the internet.
BORDER_GATEWAY = "dc1-border-01"
PLATFORMS = ("arista_eos", "cisco_nxos", "cisco_xe", "dell_sonic", "sonic")
SPINE_PLATFORMS = ("arista_eos", "cisco_nxos", "sonic")


def add_route_protection_facts(data: dict, *, device_name: str, enforce: bool) -> None:
    """Give a device fixture what the route protection reads: the pool and switch on its deployment, the extra roots.

    The roots are the ones queries/config/*.gql carry: the environment prefixes, the VRF instances, the multisite VIP
    interfaces (which make a device a border gateway) and the segments (their count bounds the EVPN routes).
    """
    deployment = data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]
    deployment["routing_filter_enforce"] = _v(enforce)
    deployment["loopback_pool"] = _node({"resources": _edges([{"prefix": _v(LOOPBACK_POOL)}])})
    add_environment_roots(data, tenant_members=[device_name, BORDER_GATEWAY], internet_members=[BORDER_GATEWAY])
    data["DcimVirtualInterface"] = _edges(
        [
            {
                "ip_address": _node({"address": _v("10.0.0.250/32")}),
                "device": _node(
                    {
                        "name": _v(BORDER_GATEWAY),
                        "role": _v("border-leaf"),
                        "interfaces": _edges(
                            [{"role": _v("loopback"), "ip_address": _node({"address": _v("10.0.0.50/32")})}]
                        ),
                    }
                ),
            }
        ]
    )


def build_protected_device_data(*, device_name: str, role: str, platform: str, enforce: bool) -> dict:
    """The base device fixture (with its segments, so a leaf is a member of VRF_A) and the route protection's facts."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform=platform,
        scenario="ebgp_ebgp",
        include_segments=role == "leaf",
    )
    add_route_protection_facts(data, device_name=device_name, enforce=enforce)
    return data


def write_fixtures() -> tuple[int, int]:
    """Write the staged and enforced leaf fixtures per platform and the spine ones on the CLI platforms and SONiC."""
    fixtures: list[tuple[type, str, dict]] = [
        (
            transform_cls,
            f"{type_prefix}_{platform}_routing_protection_{'enforced' if enforce else 'staged'}",
            build_protected_device_data(
                device_name=f"dc1-{type_prefix}-01", role=role, platform=platform, enforce=enforce
            ),
        )
        for transform_cls, role, type_prefix, platforms in (
            (Leaf, "leaf", "leaf", PLATFORMS),
            (Spine, "spine", "spine", SPINE_PLATFORMS),
        )
        for platform in platforms
        for enforce in (False, True)
    ]
    print("\nGenerating routing-protection fixtures (leaf, spine; staged and enforced):")
    return write_data_fixtures(fixtures)
