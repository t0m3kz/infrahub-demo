"""Unit tests for ``plain_vrf_names``: the VRF instances without an L3 VNI, in the order the edge renders them."""

from __future__ import annotations

from typing import Any

from transforms.helpers.vrf import plain_vrf_names
from utils.vrf_demand import VRF_KIND


def _device(*names: str) -> dict[str, Any]:
    return {"capabilities": [{"typename": VRF_KIND, "namespace": {"name": name}} for name in names]}


def _bgp(*vrfs: str) -> list[dict[str, Any]]:
    return [{"sessions": [{"vrf": vrf} for vrf in vrfs] + [{"vrf": None}]}]


def test_session_vrfs_first_in_session_order_then_the_rest_by_name() -> None:
    """Today's order (the sessions') is kept; an instance with no session follows, sorted."""
    names = plain_vrf_names(_device("A", "NON-PROD", "PROD", "Z"), _bgp("PROD", "NON-PROD", "PROD"), None)

    assert names == ["PROD", "NON-PROD", "A", "Z"]


def test_an_instance_with_an_l3_vni_is_left_to_the_vxlan_block() -> None:
    """The VXLAN L3 VNI mappings realize an instance; it is not a plain ``vrf context``."""
    vxlan = {"l3_vni_mappings": [{"vrf_name": "PROD"}]}

    assert plain_vrf_names(_device("PROD", "OTHER"), _bgp("PROD"), vxlan) == ["OTHER"]


def test_a_session_vrf_without_an_instance_is_not_rendered() -> None:
    """The device's VRF set is its instances (the transform refuses a session VRF without one)."""
    assert plain_vrf_names(_device("A"), _bgp("GHOST"), None) == ["A"]
