"""PBR of the traffic that enters a VRF from the overlay (the L3 VNI SVI of a border leaf): the rules and the guard."""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.smoke.fixture_builders.transit import build_overlay_pbr_bl_data
from tests.smoke.fixture_builders.vrf import with_vrf_objects
from transforms.config.border_leaf import BorderLeaf
from transforms.helpers.firewall_contexts import _flatten_deployment_firewall_contexts
from transforms.helpers.firewall_pbr import get_overlay_pbr_rules
from utils.data_cleaning import clean_data


def _render(platform: str, data: dict[str, Any]) -> str:
    client = MagicMock()
    client.clone.return_value = client
    client.schema.get = AsyncMock(return_value=MagicMock())
    transform = BorderLeaf(client=client, infrahub_node=MagicMock(), root_directory=".")
    # the VRF memberships and SVIs the config demands, as the fixture writers add them
    return asyncio.run(transform.transform(with_vrf_objects(transform, copy.deepcopy(data))))


def _rules(platform: str = "cisco_nxos") -> list[dict[str, Any]]:
    device = clean_data(build_overlay_pbr_bl_data(device_name="bl-1", platform=platform))["DcimDevice"][0]
    contexts = _flatten_deployment_firewall_contexts(device["deployment"])
    return get_overlay_pbr_rules(device["interfaces"], contexts, device["deployment"])


def test_each_footprint_is_steered_by_the_prefix_of_its_segment_to_the_vip_of_its_own_context() -> None:
    """The VRF is shared: the shared context's footprint to its VIP, the dedicated one's to another."""
    (rule,) = _rules()
    assert (rule["vlan_id"], rule["vrf"]) == (3900, "PROD")
    assert {s["footprint"]: (s["prefixes"], s["fw_nexthop"]) for s in rule["steers"]} == {
        "dep-acme": (["10.2.0.0/24"], "100.66.0.12"),
        "dep-shared": (["10.1.0.0/24"], "100.66.0.4"),
    }


def test_an_l3_vni_svi_without_a_policy_steers_nothing() -> None:
    """No policy attached: the interface is routed normally."""
    data = clean_data(build_overlay_pbr_bl_data(device_name="bl-1", platform="cisco_nxos"))["DcimDevice"][0]
    for iface in data["interfaces"]:
        if iface.get("role") == "l3vni":
            iface["capabilities"] = []
    assert (
        get_overlay_pbr_rules(
            data["interfaces"], _flatten_deployment_firewall_contexts(data["deployment"]), data["deployment"]
        )
        == []
    )


def test_a_footprint_with_no_routed_segment_in_the_vrf_steers_nothing() -> None:
    """A policy for a footprint that has no segment in this VRF yields no rule, never an empty ACL."""
    data = clean_data(build_overlay_pbr_bl_data(device_name="bl-1", platform="cisco_nxos"))["DcimDevice"][0]
    for iface in data["interfaces"]:
        if iface.get("role") == "l3vni":
            iface["capabilities"] = [
                c for c in iface["capabilities"] if c["customer_deployment"]["id"] == "dep-nowhere"
            ] or [{"typename": "RoutingPolicy", "kind": "pbr", "customer_deployment": {"id": "dep-nowhere"}}]
    contexts = _flatten_deployment_firewall_contexts(data["deployment"])
    assert get_overlay_pbr_rules(data["interfaces"], contexts, data["deployment"]) == []


def test_nx_os_renders_the_acl_the_route_map_and_the_attachment() -> None:
    """Definition and attachment: an ACL per footprint, one route-map, `ip policy route-map` on the L3 VNI SVI."""
    out = _render("cisco_nxos", build_overlay_pbr_bl_data(device_name="bl-1", platform="cisco_nxos"))
    assert "feature pbr" in out
    assert "ip access-list PBR-OVERLAY-VLAN3900-1" in out and "permit ip any 10.2.0.0/24" in out
    assert "route-map RM-OVERLAY-PBR-VLAN3900 permit 20" in out and "set ip next-hop 100.66.0.4" in out
    svi = out.split("interface Vlan3900")[1].split("interface Vlan")[0]
    assert "ip policy route-map RM-OVERLAY-PBR-VLAN3900" in svi and "ip forward" in svi


@pytest.mark.parametrize("platform", ["arista_eos", "sonic", "dell_sonic"])
def test_a_platform_that_cannot_render_it_refuses_instead_of_dropping_the_pbr(platform: str) -> None:
    """The contract: no silent skip and no fallback to plain routing."""
    with pytest.raises(ValueError, match="cannot render the PBR of the overlay ingress for VLANs \\[3900\\]"):
        _render(platform, build_overlay_pbr_bl_data(device_name="bl-1", platform=platform))
