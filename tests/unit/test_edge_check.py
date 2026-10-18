"""Unit tests for CheckEdge: a transit session needs a BGW edge with an INTERNET L3 VNI."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.edge import CheckEdge, warn_transit_without_internet_bgw

_INTERNET_NS = {"name": "INTERNET", "l3_vni": 60002, "namespace_type": "internet"}


def _edge(
    *, bgw: bool = True, rt_as: int | None = 65000, namespace: dict[str, Any] | None = _INTERNET_NS
) -> dict[str, Any]:
    """Cleaned edge data with one VRF peering whose local address is in ``namespace``."""
    interfaces: list[dict[str, Any]] = [
        {"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}}
    ]
    if bgw:
        interfaces.append({"name": "Loopback1", "role": "multisite-vip", "ip_address": {"address": "10.0.0.50/32"}})
    capabilities = []
    if namespace:
        capabilities.append(
            {
                "typename": "ManagedBGP",
                "peerings": [
                    {
                        "name": "TRANSIT-LUMEN-FRA1",
                        "password": "x",
                        "capabilities": [
                            {
                                "name": "Ethernet1/14",
                                "device": {"name": "eg-fr01"},
                                "ip_address": {"address": "203.0.113.0/31", "ip_namespace": namespace},
                            },
                            {"name": "eth1", "device": {"name": "EXT-LUMEN-FRA1"}},
                        ],
                    }
                ],
            }
        )
    return {
        "name": "eg-fr01",
        "interfaces": interfaces,
        "capabilities": capabilities,
        "deployment": {"evpn_rt_as": {"asn": rt_as}} if rt_as else {},
    }


def test_transit_on_bgw_with_l3_vni_passes() -> None:
    """A BGW edge with an INTERNET L3 VNI raises nothing."""
    assert warn_transit_without_internet_bgw(_edge()) == []


@pytest.mark.parametrize("kwargs", [{"bgw": False}, {"rt_as": None}])
def test_transit_on_non_bgw_edge_warns(kwargs: dict[str, Any]) -> None:
    """No multisite-vip interface, or no fabric evpn_rt_as, means the edge is no BGW."""
    warnings = warn_transit_without_internet_bgw(_edge(**kwargs))
    assert len(warnings) == 1
    assert "not an EVPN Multi-Site border gateway" in warnings[0]
    assert "eg-fr01" in warnings[0]


def test_transit_namespace_without_l3_vni_warns() -> None:
    """The INTERNET namespace must carry an L3 VNI."""
    warnings = warn_transit_without_internet_bgw(_edge(namespace={"name": "INTERNET", "namespace_type": "internet"}))
    assert len(warnings) == 1
    assert "no L3 VNI" in warnings[0]


@pytest.mark.parametrize(
    "namespace", [None, {"name": "PROD", "l3_vni": 60001, "namespace_type": "prod"}], ids=["no-session", "tenant-vrf"]
)
def test_edge_without_transit_session_is_not_checked(namespace: dict[str, Any] | None) -> None:
    """An edge without an internet VRF session is not a transit edge, BGW or not."""
    assert warn_transit_without_internet_bgw(_edge(bgw=False, rt_as=None, namespace=namespace)) == []


def test_check_logs_warning_as_info_not_error() -> None:
    """The warning must not block the merge: it goes through log_info, never log_error."""
    check = cast(Any, CheckEdge.__new__(CheckEdge))
    errors: list[str] = []
    infos: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)

    device = _edge(bgw=False)
    device["interfaces"].append({"name": "Ethernet1/1", "role": "uplink"})
    check.validate({"DcimDevice": {"edges": [{"node": device}]}})

    assert not [e for e in errors if "border gateway" in e]
    assert len(infos) == 1
    assert infos[0].startswith("WARNING: Edge 'eg-fr01'")
