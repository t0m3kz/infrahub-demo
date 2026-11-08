"""The reader of the ManagedStaticRoute capabilities: what the templates get, and what it refuses."""

from __future__ import annotations

from typing import Any

import pytest

from transforms.helpers.static_routes import get_static_routes


def _cap(
    prefix: str = "0.0.0.0/0", *, vrf: str | None = "PROD", exported: bool = True, distance: int = 1
) -> dict[str, Any]:
    return {
        "typename": "ManagedStaticRoute",
        "name": f"rt-{prefix}",
        "prefix": prefix,
        "next_hop": "10.0.0.4",
        "distance": distance,
        "exported": exported,
        "vrf": {"namespace": {"name": vrf, "l3_vni": 50001}} if vrf else None,
    }


def test_routes_are_sorted_and_a_local_one_is_marked() -> None:
    """The sort key is (vrf, prefix, next hop); an unexported route carries local_only."""
    routes = get_static_routes([_cap("10.9.0.0/24"), {"typename": "ManagedBGP"}, _cap(exported=False)])
    assert routes == [
        {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "10.0.0.4", "local_only": True},
        {"vrf": "PROD", "l3_vni": 50001, "prefix": "10.9.0.0/24", "nexthop": "10.0.0.4"},
    ]


def test_nothing_gives_nothing() -> None:
    """No capabilities, no routes."""
    assert get_static_routes(None) == [] and get_static_routes([]) == []


def test_a_route_in_the_default_table_is_refused_loudly() -> None:
    """The templates render VRF statics only; a person's default-table route must not vanish."""
    with pytest.raises(ValueError, match="default table"):
        get_static_routes([_cap(vrf=None)])


def test_a_distance_other_than_one_is_refused_loudly() -> None:
    """The templates have no distance; it must not be dropped silently."""
    with pytest.raises(ValueError, match="distance 5"):
        get_static_routes([_cap(distance=5)])


def test_an_ipv6_route_is_refused_loudly() -> None:
    """The templates render `ip route` only; an IPv6 prefix must not be written as one."""
    with pytest.raises(ValueError, match="not IPv4"):
        get_static_routes([_cap("::/0")])
