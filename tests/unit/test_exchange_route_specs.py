"""utils/exchange_transit.py exchange_route_specs: the one rule for the static routes of a firewall context exchange."""

from __future__ import annotations

from utils.exchange_transit import exchange_route_specs

_TENANT = {"namespace": "PROD", "l3_vni": 50001, "ns_type": "prod", "vip": "100.66.0.4"}
_INTERNET = {"namespace": "INTERNET", "l3_vni": 50003, "ns_type": "internet", "vip": "100.66.32.4"}
_MANAGEMENT = {"namespace": "MANAGEMENT", "l3_vni": 50004, "ns_type": "management", "vip": "100.66.48.4"}


def test_a_shared_context_gets_the_default_route_and_the_return_route_of_its_address() -> None:
    routes = exchange_route_specs(_TENANT, _INTERNET, shared=True, snat="192.0.2.5")
    assert routes == [
        {"namespace": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4", "local_only": False},
        {
            "namespace": "INTERNET",
            "l3_vni": 50003,
            "prefix": "192.0.2.5/32",
            "nexthop": "100.66.32.4",
            "local_only": False,
        },
    ]


def test_a_dedicated_context_gets_no_default_route() -> None:
    """Its tenant is steered by the leaf PBR; only the return route of its address is a static."""
    routes = exchange_route_specs(_TENANT, _INTERNET, shared=False, snat="192.0.2.6")
    assert [route["prefix"] for route in routes] == ["192.0.2.6/32"]


def test_the_tenant_default_stays_local_when_it_is_not_exported() -> None:
    routes = exchange_route_specs(_TENANT, _INTERNET, shared=True, snat=None, export_tenant_default=False)
    assert routes == [
        {"namespace": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4", "local_only": True}
    ]


def test_a_management_exchange_needs_no_return_route() -> None:
    """MANAGEMENT is translated to its own leg's VIP: only the default route, and none without a SNAT address."""
    routes = exchange_route_specs(_TENANT, _MANAGEMENT, shared=True, snat="192.0.2.5")
    assert [route["prefix"] for route in routes] == ["0.0.0.0/0"]


def test_no_address_means_no_return_route() -> None:
    assert exchange_route_specs(_TENANT, _INTERNET, shared=False, snat=None) == []
