"""The fabric-design and pool-size expectations tests/integration/test_61 and test_71 assert.

fabric_constants.expected_*_pool_slices turn the bootstrap designs (via the unit-test simulator) and a DC's
underlay_protocol into the slice every fabric pool must have; fabric_pool_helpers.fetch_fabric_pools reads the live
slices and find_fabric_design_errors / find_fabric_pool_errors compare them. A finder that silently returns
no error would let a broken generator run green, so each rule is driven through a failing inventory here.

The pinned slices are the ones the two scenarios build: 30_all's DCs (design L, S_MIDDLE pods, IPv6) and
the arista scope's DC14 (design M, S_MIDDLE pods, IPv4). Their host bits are tests/unit/test_fabric_sizing.py's
golden table; this file pins the family and prefix length each pool gets from them.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.fabric_constants import (
    DEFAULT_UNDERLAY_PROTOCOL,
    FabricSlice,
    expected_dc_pool_slices,
    expected_pod_pool_slices,
)
from tests.integration.fabric_pool_helpers import fetch_fabric_pools, find_fabric_design_errors, find_fabric_pool_errors
from tests.unit.simulators.fabric_designs import DC_DESIGN_NAMES, dc_design

# ---------------------------------------------------------------------------
# Expected slices
# ---------------------------------------------------------------------------


def test_30_all_dc_pools_are_the_ipv6_slices_of_design_l() -> None:
    assert expected_dc_pool_slices("L", "ipv6") == {
        "technical": FabricSlice(6, 115),
        "loopback": FabricSlice(6, 117),
        "management": FabricSlice(4, 22),
        "fabric_loopback": FabricSlice(6, 124),
    }


def test_30_all_pod_pools_are_the_ipv6_slices_of_s_middle_in_design_l() -> None:
    assert expected_pod_pool_slices("L", "S_MIDDLE", "ipv6") == {
        "technical": FabricSlice(6, 121),
        "loopback": FabricSlice(6, 124),
    }


def test_dc14_pools_are_the_ipv4_slices_of_design_m() -> None:
    assert expected_dc_pool_slices("M", "ipv4") == {
        "technical": FabricSlice(4, 20),
        "loopback": FabricSlice(4, 22),
        "management": FabricSlice(4, 23),
        "fabric_loopback": FabricSlice(4, 29),
    }
    assert expected_pod_pool_slices("M", "S_MIDDLE", "ipv4") == {
        "technical": FabricSlice(4, 25),
        "loopback": FabricSlice(4, 28),
    }


# underlay_protocol -> (technical, loopback) family; management is IPv4 whatever the underlay.
_FAMILIES = {"ipv4": (4, 4), "ipv6": (6, 6), "dual_stack": (6, 4)}


@pytest.mark.parametrize("design", DC_DESIGN_NAMES)
@pytest.mark.parametrize("protocol", sorted(_FAMILIES))
def test_each_pool_takes_the_family_of_its_underlay(design: str, protocol: str) -> None:
    """Dual-stack: IPv6 links, IPv4 loopbacks; the fabric loopback follows the loopback, a pod its DC."""
    technical, loopback = _FAMILIES[protocol]
    dc = expected_dc_pool_slices(design, protocol)
    pod = expected_pod_pool_slices(design, "S_MIDDLE", protocol)

    dc_technical, dc_loopback, management, tier = (
        dc[pool] for pool in ("technical", "loopback", "management", "fabric_loopback")
    )
    assert dc_technical is not None and dc_loopback is not None and management is not None

    assert (dc_technical.version, dc_loopback.version, management.version) == (technical, loopback, 4)
    assert tier is None or tier.version == loopback
    assert (pod["technical"].version, pod["loopback"].version) == (technical, loopback)


@pytest.mark.parametrize("design", DC_DESIGN_NAMES)
def test_an_ipv4_slice_keeps_the_host_bits_of_the_ipv6_one(design: str) -> None:
    """The sizes are host bits: the same pool is 96 bits shorter in IPv6 than in IPv4."""
    ipv4, ipv6 = expected_dc_pool_slices(design, "ipv4"), expected_dc_pool_slices(design, "ipv6")

    for pool in ("technical", "loopback", "fabric_loopback"):
        v4, v6 = ipv4[pool], ipv6[pool]
        if v4 is None or v6 is None:
            assert v4 is v6 is None
        else:
            assert v6.prefix_length - v4.prefix_length == 96
    assert ipv4["management"] == ipv6["management"]


def test_a_design_without_dc_tiers_expects_no_fabric_loopback_pool() -> None:
    row = dc_design("S")
    assert row["max_super_spines_per_fabric"] == row["max_hyper_spines_per_fabric"] == 0
    assert row["max_border_leafs_per_fabric"] == 0

    assert expected_dc_pool_slices("S", "ipv6")["fabric_loopback"] is None


def test_an_unknown_design_raises_key_error() -> None:
    with pytest.raises(KeyError):
        expected_dc_pool_slices("XXL", "ipv6")
    with pytest.raises(KeyError):
        expected_pod_pool_slices("L", "NO_SUCH_POD", "ipv6")


def test_a_fabric_slice_reads_as_family_and_length() -> None:
    assert str(FabricSlice(6, 121)) == "IPv6 /121"


# ---------------------------------------------------------------------------
# fetch_fabric_pools: the raw get_fabric_pools.gql payload
# ---------------------------------------------------------------------------

_ALL_POD_DESIGNS = ["M_MIDDLE", "M_MIXED", "S_BORDER_SPINE_POD", "S_MIDDLE", "S_MIXED", "S_TOR"]


def _resources(*prefixes: str) -> dict[str, Any]:
    return {"edges": [{"node": {"prefix": {"value": prefix}}} for prefix in prefixes]}


def _pool(pool_id: str, name: str, *prefixes: str) -> dict[str, Any]:
    return {"node": {"id": pool_id, "name": {"value": name}, "resources": _resources(*prefixes)}}


def _raw_payload(*, underlay_protocol: str | None = "ipv6") -> dict[str, Any]:
    """DC10 (design L) with one S_MIDDLE pod, shaped like the live answer to get_fabric_pools.gql."""
    loopback_children = [
        ("fd00:2000::/124", "pool-dc10-fabric-loopback", "CoreIPAddressPool"),
        ("fd00:2000::10/124", "pool-pod1-loopback", "CoreIPAddressPool"),
    ]
    return {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "id": "dc-10",
                        "name": {"value": "DC10"},
                        "underlay_protocol": {"value": underlay_protocol},
                        "design": {
                            "node": {
                                "name": {"value": "L"},
                                "max_pods": {"value": 8},
                                "pod_templates": {
                                    "edges": [{"node": {"name": {"value": name}}} for name in _ALL_POD_DESIGNS]
                                },
                            }
                        },
                        "technical_pool": _pool("pool-dc10-technical", "dc10-technical-pool", "fd00:2100::/115"),
                        "loopback_pool": {
                            "node": {
                                "id": "pool-dc10-loopback",
                                "name": {"value": "dc10-loopback-pool"},
                                "resources": {
                                    "edges": [
                                        {
                                            "node": {
                                                "prefix": {"value": "fd00:2000::/117"},
                                                "children": {
                                                    "edges": [
                                                        {
                                                            "node": {
                                                                "prefix": {"value": prefix},
                                                                "resource_pool": {
                                                                    "edges": [
                                                                        {"node": {"id": owner, "__typename": kind}}
                                                                    ]
                                                                },
                                                            }
                                                        }
                                                        for prefix, owner, kind in loopback_children
                                                    ]
                                                },
                                            }
                                        }
                                    ]
                                },
                            }
                        },
                        "management_pool": _pool("pool-dc10-management", "dc10-management-pool", "172.16.0.0/22"),
                    }
                }
            ]
        },
        "TopologyPod": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "DC10-1-POD-1"},
                        "design": {"node": {"name": {"value": "S_MIDDLE"}}},
                        "parent": {"node": {"id": "dc-10", "__typename": "TopologyDataCenter"}},
                        "prefix_pool": _pool("pool-pod1-technical", "dc10-1-pod-1-technical-pool", "fd00:2100::80/121"),
                        "loopback_pool": _pool("pool-pod1-loopback", "dc10-1-pod-1-loopback-pool", "fd00:2000::10/124"),
                    }
                }
            ]
        },
    }


def _client(payload: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.execute_graphql = AsyncMock(return_value=payload)
    return client


async def test_fetch_fabric_pools_reads_designs_and_slices() -> None:
    client = _client(_raw_payload())

    inventory = await fetch_fabric_pools(client, "all-demo")

    client.execute_graphql.assert_awaited_once()
    assert client.execute_graphql.await_args.kwargs["branch_name"] == "all-demo"
    assert inventory == {
        "dcs": [
            {
                "id": "dc-10",
                "name": "DC10",
                "underlay_protocol": "ipv6",
                "design": "L",
                "max_pods": 8,
                "pod_templates": _ALL_POD_DESIGNS,
                "pools": {
                    "technical": ["fd00:2100::/115"],
                    "loopback": ["fd00:2000::/117"],
                    "management": ["172.16.0.0/22"],
                    # The pod's loopback slice is a child too, but a pod owns it.
                    "fabric_loopback": ["fd00:2000::/124"],
                },
            }
        ],
        "pods": [
            {
                "name": "DC10-1-POD-1",
                "dc_id": "dc-10",
                "design": "S_MIDDLE",
                "pools": {"technical": ["fd00:2100::80/121"], "loopback": ["fd00:2000::10/124"]},
            }
        ],
    }


async def test_fetch_fabric_pools_defaults_the_underlay_and_keeps_unset_pools_none() -> None:
    payload = _raw_payload(underlay_protocol=None)
    dc = payload["TopologyDataCenter"]["edges"][0]["node"]
    dc["technical_pool"] = {"node": None}
    dc["design"] = {"node": None}
    payload["TopologyPod"]["edges"][0]["node"]["loopback_pool"] = {"node": None}

    inventory = await fetch_fabric_pools(_client(payload), "all-demo")

    (dc_row,) = inventory["dcs"]
    assert dc_row["underlay_protocol"] == DEFAULT_UNDERLAY_PROTOCOL
    assert (dc_row["design"], dc_row["max_pods"], dc_row["pod_templates"]) == (None, None, [])
    assert dc_row["pools"]["technical"] is None
    # With the pod's loopback pool unset nothing tells its slice from the fabric loopback: both count.
    assert dc_row["pools"]["fabric_loopback"] == ["fd00:2000::/124", "fd00:2000::10/124"]
    assert inventory["pods"][0]["pools"]["loopback"] is None


# ---------------------------------------------------------------------------
# find_fabric_design_errors / find_fabric_pool_errors
# ---------------------------------------------------------------------------


def _dc(**overrides: Any) -> dict[str, Any]:
    """DC10 as fetch_fabric_pools returns it: design L, IPv6, every pool its expected slice."""
    return {
        "id": "dc-10",
        "name": "DC10",
        "underlay_protocol": "ipv6",
        "design": "L",
        "max_pods": 8,
        "pod_templates": list(_ALL_POD_DESIGNS),
        "pools": {
            "technical": ["fd00:2100::/115"],
            "loopback": ["fd00:2000::/117"],
            "management": ["172.16.0.0/22"],
            "fabric_loopback": ["fd00:2000::/124"],
        },
        **overrides,
    }


def _pod(index: int = 1, **overrides: Any) -> dict[str, Any]:
    """Pod ``index`` of DC10, design S_MIDDLE, its slices inside DC10's."""
    return {
        "name": f"DC10-1-POD-{index}",
        "dc_id": "dc-10",
        "design": "S_MIDDLE",
        "pools": {
            "technical": [f"fd00:2100::{index * 0x80:x}/121"],
            "loopback": [f"fd00:2000::{index * 0x10:x}/124"],
        },
        **overrides,
    }


def _inventory(dcs: list[dict[str, Any]] | None = None, pods: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"dcs": dcs if dcs is not None else [_dc()], "pods": pods if pods is not None else [_pod(1), _pod(2)]}


def test_a_settled_inventory_breaks_no_rule() -> None:
    inventory = _inventory()

    assert find_fabric_design_errors(inventory) == []
    assert find_fabric_pool_errors(inventory) == []


def test_a_dc14_like_ipv4_inventory_breaks_no_rule() -> None:
    dc = _dc(
        name="DC14",
        underlay_protocol="ipv4",
        design="M",
        max_pods=4,
        pools={
            "technical": ["10.14.0.0/20"],
            "loopback": ["10.15.0.0/22"],
            "management": ["172.16.14.0/23"],
            "fabric_loopback": ["10.15.0.0/29"],
        },
    )
    pod = _pod(pools={"technical": ["10.14.0.128/25"], "loopback": ["10.15.0.16/28"]})

    assert find_fabric_design_errors(_inventory([dc], [pod])) == []
    assert find_fabric_pool_errors(_inventory([dc], [pod])) == []


@pytest.mark.parametrize(
    ("dc", "pods", "expected"),
    [
        (_dc(design=None, max_pods=None, pod_templates=[]), [_pod()], "DC DC10 has no design"),
        (_dc(max_pods=None), [_pod()], "DC DC10: design L has no max_pods"),
        (_dc(), [_pod(design=None)], "pod DC10-1-POD-1 has no design"),
        (
            _dc(pod_templates=["S_TOR"]),
            [_pod()],
            "pod DC10-1-POD-1: design S_MIDDLE is not one of DC DC10 design L's pod_templates ['S_TOR']",
        ),
        (
            _dc(max_pods=1),
            [_pod(1), _pod(2)],
            "DC DC10 holds 2 pods ['DC10-1-POD-1', 'DC10-1-POD-2'], its design L allows 1",
        ),
        (_dc(), [_pod(dc_id="elsewhere")], "pod DC10-1-POD-1: its parent (elsewhere) is not a data center"),
    ],
    ids=["dc-no-design", "design-no-max-pods", "pod-no-design", "pod-design-not-hosted", "too-many-pods", "orphan"],
)
def test_each_design_rule_is_reported(dc: dict[str, Any], pods: list[dict[str, Any]], expected: str) -> None:
    assert find_fabric_design_errors(_inventory([dc], pods)) == [expected]


def test_max_pods_counts_every_pod_up_to_the_cap() -> None:
    assert find_fabric_design_errors(_inventory([_dc(max_pods=2)], [_pod(1), _pod(2)])) == []


def _with_pool(record: dict[str, Any], pool: str, slices: list[str] | None) -> dict[str, Any]:
    changed = copy.deepcopy(record)
    changed["pools"][pool] = slices
    return changed


@pytest.mark.parametrize(
    ("dc", "pod", "expected"),
    [
        (
            _with_pool(_dc(), "technical", ["fd00:2100::/114"]),
            _pod(),
            ["DC DC10 technical pool is fd00:2100::/114, expected IPv6 /115"],
        ),
        (
            _with_pool(_dc(), "management", ["fd00:3000::/118"]),
            _pod(),
            ["DC DC10 management pool is fd00:3000::/118, expected IPv4 /22"],
        ),
        (
            _with_pool(_dc(), "management", None),
            _pod(),
            ["DC DC10 management pool is not set, expected an IPv4 /22 slice"],
        ),
        (
            _with_pool(_dc(), "fabric_loopback", []),
            _pod(),
            ["DC DC10 fabric_loopback pool has 0 slice(s) [], expected exactly one IPv6 /124"],
        ),
        (
            _dc(),
            _with_pool(_pod(), "technical", ["fd00:2100::80/122"]),
            ["pod DC10-1-POD-1 technical pool is fd00:2100::80/122, expected IPv6 /121"],
        ),
        (
            _dc(),
            _with_pool(_pod(), "loopback", ["fd00:2000::10/124", "fd00:2000::20/124"]),
            [
                "pod DC10-1-POD-1 loopback pool has 2 slice(s) ['fd00:2000::10/124', 'fd00:2000::20/124'], "
                "expected exactly one IPv6 /124"
            ],
        ),
        (
            _dc(),
            _with_pool(_pod(), "technical", ["fd00:9999::/121"]),
            [
                "pod DC10-1-POD-1 technical pool slice fd00:9999::/121 is outside DC DC10's technical slice(s) "
                "['fd00:2100::/115']"
            ],
        ),
        (
            _dc(design="XXL"),
            _pod(),
            # A DC design the bootstrap lacks cannot size its pods either.
            [
                "DC DC10: design XXL is not a bootstrap design",
                "pod DC10-1-POD-1: design S_MIDDLE (DC XXL) is not a bootstrap design",
            ],
        ),
        (
            _dc(),
            _pod(design="NO_SUCH_POD"),
            ["pod DC10-1-POD-1: design NO_SUCH_POD (DC L) is not a bootstrap design"],
        ),
    ],
    ids=[
        "dc-wrong-length",
        "dc-wrong-family",
        "dc-pool-unset",
        "fabric-loopback-missing",
        "pod-wrong-length",
        "pod-two-slices",
        "pod-outside-dc",
        "unknown-dc-design",
        "unknown-pod-design",
    ],
)
def test_each_pool_rule_is_reported(dc: dict[str, Any], pod: dict[str, Any], expected: list[str]) -> None:
    assert find_fabric_pool_errors(_inventory([dc], [pod])) == expected


def test_a_dc_without_tiers_must_have_no_fabric_loopback_slice() -> None:
    dc = _dc(
        design="S",
        max_pods=2,
        pools={
            "technical": ["fd00:2100::/117"],
            "loopback": ["fd00:2000::/120"],
            "management": ["172.16.0.0/24"],
            "fabric_loopback": ["fd00:2000::/124"],
        },
    )
    pod = _pod(pools={"technical": ["fd00:2100::40/122"], "loopback": ["fd00:2000::10/124"]})

    assert find_fabric_pool_errors(_inventory([dc], [pod])) == [
        "DC DC10 fabric_loopback pool has slice(s) ['fd00:2000::/124'], expected none (the design has no DC tier)"
    ]


def test_pool_sizes_skip_what_the_design_rules_report() -> None:
    """No design (or no DC) means no expectation: find_fabric_design_errors owns those lines."""
    inventory = _inventory(
        [_dc(design=None, max_pods=None, pod_templates=[])],
        [_pod(1), _pod(2, dc_id="elsewhere"), _pod(3, design=None, dc_id="dc-10")],
    )

    assert find_fabric_pool_errors(inventory) == []
