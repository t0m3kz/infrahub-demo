"""Unit tests for CheckPoolUtilization (checks/pool_utilization.py).

Payloads are raw pool_utilization query responses (edges/node/value), shaped like the live stack's DC10:
an IPv6 fabric with two pods, the pod technical pool fd00:2100::40/122 holding 22 /127 links.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from checks.pool_utilization import CheckPoolUtilization, PoolUsage, pool_usages

DC_ID = "dc10"


def _value(value: Any) -> dict[str, Any]:
    return {"value": value}


def _named(name: str) -> dict[str, Any]:
    return {"node": {"name": _value(name)}}


def _p2p(slice_: str, count: int) -> list[str]:
    """The first ``count`` /127 (or /31) links of ``slice_``."""
    net = ipaddress.ip_network(slice_)
    links = net.subnets(new_prefix=127 if net.version == 6 else 31)
    return [str(next(links)) for _ in range(count)]


def _prefix_pool(pool_id: str, name: str, slice_: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "node": {
            "id": pool_id,
            "name": _value(name),
            "resources": {"edges": [{"node": {"prefix": _value(slice_), "children": {"edges": children}}}]},
        }
    }


def _child(prefix: str) -> dict[str, Any]:
    return {"node": {"prefix": _value(prefix)}}


def _dc_child(prefix: str, pool_id: str | None = None, *, kind: str = "", name: str = "", addresses: int = 0) -> dict:
    pools = [{"node": {"id": pool_id, "__typename": kind, "name": _value(name)}}] if pool_id else []
    return {
        "node": {
            "prefix": _value(prefix),
            "ip_addresses": {"count": addresses},
            "resource_pool": {"edges": pools},
        }
    }


def _address_pool(
    pool_id: str, name: str, slice_: str, addresses: int, *, namespace: str = "default", slice_namespace: str = ""
) -> dict[str, Any]:
    return {
        "node": {
            "id": pool_id,
            "name": _value(name),
            "ip_namespace": _named(namespace),
            "resources": {
                "edges": [
                    {
                        "node": {
                            "prefix": _value(slice_),
                            "ip_namespace": _named(slice_namespace or namespace),
                            "ip_addresses": {"count": addresses},
                        }
                    }
                ]
            },
        }
    }


def _pod(
    index: int, technical_slice: str, links: int, loopback_slice: str, loopbacks: int, *, design: str = "S_MIDDLE"
) -> dict[str, Any]:
    return {
        "node": {
            "id": f"pod-{index}",
            "name": _value(f"DC10-1-POD-{index}"),
            "design": _named(design),
            "parent": {"node": {"id": DC_ID, "__typename": "TopologyDataCenter"}},
            "prefix_pool": _prefix_pool(
                f"pod-{index}-technical",
                f"dc10-1-pod-{index}-technical-pool",
                technical_slice,
                [_child(link) for link in _p2p(technical_slice, links)],
            ),
            "loopback_pool": _address_pool(
                f"pod-{index}-loopback", f"dc10-1-pod-{index}-loopback-pool", loopback_slice, loopbacks
            ),
        }
    }


def _payload(
    *,
    pod1_links: int = 22,
    management_addresses: int = 24,
    technical_slice: str = "fd00:2100::/114",
    max_pods: int = 8,
) -> dict[str, Any]:
    """DC10 as on the live stack: pods of 22 and 6 links, 10 and 2 loopbacks, a 4-address fabric loopback pool."""
    pods = [
        _pod(1, "fd00:2100::40/122", pod1_links, "fd00:2000::420/124", 10),
        _pod(2, "fd00:2100::/122", 6, "fd00:2000::410/124", 2),
    ]
    dc = {
        "id": DC_ID,
        "name": _value("DC10"),
        "design": {"node": {"name": _value("L"), "max_pods": _value(max_pods)}},
        "technical_pool": _prefix_pool(
            "dc-technical",
            "dc10-technical-pool",
            technical_slice,
            [
                _dc_child("fd00:2100::40/122", "pod-1-technical", kind="CoreIPPrefixPool"),
                _dc_child("fd00:2100::/122", "pod-2-technical", kind="CoreIPPrefixPool"),
            ],
        ),
        "loopback_pool": _prefix_pool(
            "dc-loopback",
            "dc10-loopback-pool",
            "fd00:2000::400/118",
            [
                _dc_child("fd00:2000::420/124", "pod-1-loopback", kind="CoreIPAddressPool", addresses=10),
                _dc_child("fd00:2000::410/124", "pod-2-loopback", kind="CoreIPAddressPool", addresses=2),
                _dc_child(
                    "fd00:2000::400/124",
                    "dc-fabric-loopback",
                    kind="CoreIPAddressPool",
                    name="dc10-dc-fabric-loopback-pool",
                    addresses=4,
                ),
            ],
        ),
        # The slice is a `default` prefix, the addresses are in MANAGEMENT: the hierarchy count is 0.
        "management_pool": _address_pool(
            "dc-management",
            "dc10-management-pool",
            "172.16.1.0/24",
            0,
            namespace="MANAGEMENT",
            slice_namespace="default",
        ),
    }
    management = [f"172.16.1.{host}/32" for host in range(1, management_addresses + 1)] + ["172.16.2.1/32"]
    return {
        "TopologyDataCenter": {"edges": [{"node": dc}]},
        "TopologyPod": {"edges": pods},
        "IpamIPAddress": {
            "edges": [{"node": {"address": _value(a), "ip_namespace": _named("MANAGEMENT")}} for a in management]
        },
    }


def _run(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """(errors, warnings) the check logs for ``payload``."""
    check = CheckPoolUtilization(branch="test")
    check.validate(payload)
    errors = [log for log in check.logs if log["level"] == "ERROR"]
    warnings = [
        log["message"] for log in check.logs if log["level"] == "INFO" and log["message"].startswith("WARNING:")
    ]
    return errors, warnings


def _usage(payload: dict[str, Any], name: str) -> PoolUsage:
    return next(usage for usage in pool_usages(payload) if usage.name == name)


class TestPoolUsages:
    @pytest.mark.parametrize(
        ("pool", "used", "capacity"),
        [
            ("dc10-1-pod-1-technical-pool", 44, 64),  # 22 /127 links, 2 addresses each
            ("dc10-1-pod-2-technical-pool", 12, 64),
            ("dc10-1-pod-1-loopback-pool", 10, 15),  # IPv6 /124 minus the subnet-router anycast
            ("dc10-technical-pool", 128, 16384),  # two nested /122 pod pools by their own size
            ("dc10-loopback-pool", 48, 1024),  # three /124 slices
            ("dc10-dc-fabric-loopback-pool", 4, 15),
            ("dc10-management-pool", 24, 254),  # MANAGEMENT addresses inside the slice, IPv4 minus 2
        ],
    )
    def test_used_and_capacity_per_pool(self, pool: str, used: int, capacity: int) -> None:
        """Each fabric pool's used/capacity, in addresses, as the live stack's DC10 has them."""
        usage = _usage(_payload(), pool)
        assert (usage.used, usage.capacity) == (used, capacity)

    def test_every_pool_reported_once(self) -> None:
        """Pod loopback slices are children of the DC loopback pool but reported once, against their pod."""
        names = [usage.name for usage in pool_usages(_payload())]
        assert len(names) == len(set(names)) == 8

    def test_ipv4_links_count_two_addresses(self) -> None:
        """A /31 link in an IPv4 pod technical pool counts 2 of the slice's addresses (no network/broadcast)."""
        payload = _payload()
        payload["TopologyPod"]["edges"] = [_pod(1, "1.0.32.0/26", 14, "1.127.2.16/28", 3)]
        assert (
            _usage(payload, "dc10-1-pod-1-technical-pool").used,
            _usage(payload, "dc10-1-pod-1-technical-pool").capacity,
        ) == (28, 64)
        assert _usage(payload, "dc10-1-pod-1-loopback-pool").capacity == 14

    def test_management_addresses_outside_the_slice_are_ignored(self) -> None:
        """Only MANAGEMENT addresses inside the pool's slice are counted (172.16.2.1 belongs to another DC)."""
        assert _usage(_payload(management_addresses=3), "dc10-management-pool").used == 3

    def test_hierarchy_count_when_slice_shares_the_namespace(self) -> None:
        """A management slice in its pool's namespace is counted from the IPAM hierarchy, not the address list."""
        payload = _payload()
        dc = payload["TopologyDataCenter"]["edges"][0]["node"]
        dc["management_pool"] = _address_pool("dc-management", "dc10-management-pool", "172.16.1.0/24", 7)
        assert _usage(payload, "dc10-management-pool").used == 7


class TestCheckPoolUtilization:
    def test_live_shape_passes_silently(self) -> None:
        """DC10 as deployed (pod-1 technical at 68.8%) raises nothing."""
        assert _run(_payload()) == ([], [])

    def test_warns_at_eighty_percent(self) -> None:
        """26 links (52/64 = 81.2%) warn, naming the pool, its slice, the usage and the pod design to enlarge."""
        errors, warnings = _run(_payload(pod1_links=26))
        assert errors == []
        assert warnings == [
            "WARNING: Pool 'dc10-1-pod-1-technical-pool' (fd00:2100::40/122) of pod 'DC10-1-POD-1' is 81.2% used "
            "(52/64 addresses): enlarge pod design 'S_MIDDLE' or move racks out of pod 'DC10-1-POD-1'."
        ]

    def test_below_eighty_percent_is_silent(self) -> None:
        """25 links (50/64 = 78.1%) stay below the warning threshold."""
        assert _run(_payload(pod1_links=25)) == ([], [])

    def test_full_link_pool_is_an_error(self) -> None:
        """A pod technical pool with every /127 taken cannot allocate the next link: an ERROR on the pool."""
        errors, _ = _run(_payload(pod1_links=32))
        assert len(errors) == 1
        assert (
            "is 100.0% used (64/64 addresses) and no free address is left, the next fabric link" in errors[0]["message"]
        )
        assert (errors[0]["object_id"], errors[0]["object_type"]) == ("pod-1-technical", "CoreIPPrefixPool")

    def test_overcommitted_pool_is_an_error(self) -> None:
        """More allocated than the slice holds (here: a shrunken /123 under 44 addresses) is an ERROR."""
        payload = _payload()
        technical = payload["TopologyPod"]["edges"][0]["node"]["prefix_pool"]["node"]
        technical["resources"]["edges"][0]["node"]["prefix"] = _value("fd00:2100::40/123")
        errors, _ = _run(payload)
        assert len(errors) == 1
        assert "holds 44/32 addresses (137.5%): allocations overlap" in errors[0]["message"]

    def test_full_management_pool_is_an_error(self) -> None:
        """254 MANAGEMENT addresses fill a /24: the next device gets no management address."""
        errors, _ = _run(_payload(management_addresses=254))
        assert len(errors) == 1
        assert "'dc10-management-pool' (172.16.1.0/24) of data center 'DC10' is 100.0% used" in errors[0]["message"]
        assert (
            "the next management address cannot be allocated: enlarge data center design 'L'." in errors[0]["message"]
        )

    def test_dc_pool_without_room_for_another_pod_is_an_error(self) -> None:
        """A DC technical /121 holds exactly the two /122 pods while design L allows 8: no room for pod 3."""
        errors, _ = _run(_payload(technical_slice="fd00:2100::/121"))
        assert [error["message"] for error in errors] == [
            "Pool 'dc10-technical-pool' (fd00:2100::/121) of data center 'DC10' is 100.0% used (128/128 addresses) "
            "and there is no free /122 for another pod while design 'L' allows 8 pods and 2 exist: enlarge data "
            "center design 'L' or move pod 'DC10-1-POD-1' to another data center."
        ]

    def test_dc_pool_full_at_max_pods_only_warns(self) -> None:
        """The same full DC pool at the design's max_pods is the envelope reached: a WARNING, no ERROR."""
        errors, warnings = _run(_payload(technical_slice="fd00:2100::/121", max_pods=2))
        assert errors == []
        assert len(warnings) == 1
        assert "'dc10-technical-pool' (fd00:2100::/121) of data center 'DC10' is 100.0% used" in warnings[0]

    def test_pool_without_slice_is_an_error(self) -> None:
        """A pool with no resource on the branch can allocate nothing."""
        payload = _payload()
        payload["TopologyPod"]["edges"][1]["node"]["loopback_pool"]["node"]["resources"]["edges"] = []
        errors, _ = _run(payload)
        assert [error["message"] for error in errors] == [
            "Pool 'dc10-1-pod-2-loopback-pool' (no slice) of pod 'DC10-1-POD-2' has no slice: nothing can be "
            "allocated from it; enlarge pod design 'S_MIDDLE' or move racks out of pod 'DC10-1-POD-2'."
        ]

    def test_empty_payload_passes(self) -> None:
        """No data center and no pod: nothing to measure."""
        assert _run(
            {"TopologyDataCenter": {"edges": []}, "TopologyPod": {"edges": []}, "IpamIPAddress": {"edges": []}}
        ) == ([], [])
