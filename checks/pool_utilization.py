"""Measure how full every fabric pool is: warn before a design is outgrown, fail when allocation is impossible.

Pools, all reached through the deployments (a pool is branch-agnostic, its allocations are not), so only this
branch's allocations count:

    data center  technical and loopback (container pools: the pods' slices are their children), management, and
                 the fabric-loopback pool (an address-pool child of the loopback slice that no pod owns)
    pod          technical (its fabric links) and loopback

Used and capacity, in addresses:

    prefix pool   used = the slice's direct children by size (a /127 or /31 link is 2, a sub-pool its own size);
                  capacity = every address of the slice
    address pool  used = the addresses under the slice (a management pool's sit in MANAGEMENT, outside its
                  `default` slice, and are matched by containment); capacity = the assignable hosts, as the
                  allocator hands them out: IPv4 skips network and broadcast, IPv6 the subnet-router anycast,
                  a /31, /32, /127 or /128 skips nothing

Severity:

    WARNING  >= 80 % used. The sizing leaves >= 25 % headroom at a design's envelope, so a pool this full has
             outgrown its design or is about to.
    ERROR    more used than the pool holds (overlapping or out-of-range allocations), a pool with no slice, or
             allocation impossible: a link or device pool with no free address, a DC pool with no room for
             another pod while its design allows more pods. A DC pool full at max_pods is the design's envelope
             reached: a WARNING.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from utils.data_cleaning import clean_data

from .base import WarningCheck

WARNING_PERCENT = 80
"""Used share at which a pool warns: the sizing keeps 25 % free at the design's envelope."""

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class PoolUsage:
    """One pool's usage, with what its owner should do about it."""

    id: str
    name: str
    kind: str
    slices: tuple[IPNetwork, ...]
    used: int
    capacity: int
    owner: str
    remedy: str
    exhausted: str | None
    """Why the next allocation is impossible, None while it is possible."""

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.capacity if self.capacity else 100.0

    def label(self) -> str:
        slices = ", ".join(str(net) for net in self.slices) or "no slice"
        return f"Pool '{self.name}' ({slices}) of {self.owner}"


def _name(node: Any) -> str:
    return (node.get("name") if isinstance(node, dict) else None) or "<unset>"


def _assignable(net: IPNetwork) -> int:
    """Addresses an address pool hands out of ``net``."""
    reserved = (2 if net.prefixlen < 31 else 0) if net.version == 4 else (1 if net.prefixlen < 127 else 0)
    return net.num_addresses - reserved


def _largest_free(net: IPNetwork, children: list[IPNetwork]) -> int:
    """Size of the largest aligned block of ``net`` no child occupies."""
    # Any: ipaddress types subnet_of/address_exclude per family, the version test keeps the families apart.
    free: list[Any] = [net]
    for child in children:
        for index, block in enumerate(free):
            if child.version == block.version and child.subnet_of(block):
                free[index : index + 1] = [] if child == block else list(block.address_exclude(child))
                break
    return max((block.num_addresses for block in free), default=0)


def _resources(pool: dict[str, Any]) -> list[tuple[IPNetwork, dict[str, Any]]]:
    return [
        (ipaddress.ip_network(resource["prefix"]), resource)
        for resource in pool.get("resources") or []
        if isinstance(resource, dict) and resource.get("prefix")
    ]


def _children(resource: dict[str, Any]) -> list[tuple[IPNetwork, dict[str, Any]]]:
    return [
        (ipaddress.ip_network(child["prefix"]), child)
        for child in resource.get("children") or []
        if isinstance(child, dict) and child.get("prefix")
    ]


def _address_used(pool: dict[str, Any], net: IPNetwork, resource: dict[str, Any], foreign: dict[str, list]) -> int:
    """Addresses of the pool's namespace inside ``net``: the IPAM hierarchy when the slice is in that namespace,
    containment over the namespace's addresses when it is not (a management pool)."""
    namespace = _name(pool.get("ip_namespace"))
    if _name(resource.get("ip_namespace")) == namespace:
        return int(resource.get("ip_addresses") or 0)
    return sum(1 for address in foreign.get(namespace, []) if address.version == net.version and address in net)


def _leaf_exhausted(used: int, capacity: int, consumer: str) -> str | None:
    if capacity and used >= capacity:
        return f"no free address is left, the next {consumer} cannot be allocated"
    return None


def _address_pool(
    pool: dict[str, Any], *, owner: str, remedy: str, consumer: str, foreign: dict[str, list]
) -> PoolUsage:
    resources = _resources(pool)
    used = sum(_address_used(pool, net, resource, foreign) for net, resource in resources)
    capacity = sum(_assignable(net) for net, _ in resources)
    return PoolUsage(
        id=str(pool.get("id")),
        name=_name(pool),
        kind="CoreIPAddressPool",
        slices=tuple(net for net, _ in resources),
        used=used,
        capacity=capacity,
        owner=owner,
        remedy=remedy,
        exhausted=_leaf_exhausted(used, capacity, consumer),
    )


def pool_usages(data: Any) -> list[PoolUsage]:
    """Every fabric pool's usage from the pool_utilization query (raw or cleaned)."""
    cleaned = clean_data(data)
    foreign: dict[str, list] = {}
    for address in cleaned.get("IpamIPAddress") or []:
        foreign.setdefault(_name(address.get("ip_namespace")), []).append(ipaddress.ip_interface(address["address"]).ip)

    usages: list[PoolUsage] = []
    pods_by_dc: dict[str, list[dict[str, Any]]] = {}
    pod_of_pool: dict[str, str] = {}
    for pod in cleaned.get("TopologyPod") or []:
        pods_by_dc.setdefault(str((pod.get("parent") or {}).get("id")), []).append(pod)
        pod_name = _name(pod)
        owner = f"pod '{pod_name}'"
        remedy = f"enlarge pod design '{_name(pod.get('design'))}' or move racks out of pod '{pod_name}'"
        technical = pod.get("prefix_pool")
        if isinstance(technical, dict):
            pod_of_pool[str(technical.get("id"))] = pod_name
            resources = _resources(technical)
            used = sum(child.num_addresses for _, resource in resources for child, _ in _children(resource))
            capacity = sum(net.num_addresses for net, _ in resources)
            usages.append(
                PoolUsage(
                    id=str(technical.get("id")),
                    name=_name(technical),
                    kind="CoreIPPrefixPool",
                    slices=tuple(net for net, _ in resources),
                    used=used,
                    capacity=capacity,
                    owner=owner,
                    remedy=remedy,
                    exhausted=_leaf_exhausted(used, capacity, "fabric link"),
                )
            )
        loopback = pod.get("loopback_pool")
        if isinstance(loopback, dict):
            pod_of_pool[str(loopback.get("id"))] = pod_name
            usages.append(
                _address_pool(loopback, owner=owner, remedy=remedy, consumer="device loopback", foreign=foreign)
            )

    for dc in cleaned.get("TopologyDataCenter") or []:
        dc_name = _name(dc)
        design = dc.get("design") or {}
        owner = f"data center '{dc_name}'"
        design_remedy = f"enlarge data center design '{_name(design)}'"
        pods = len(pods_by_dc.get(str(dc.get("id")), []))
        for attribute in ("technical_pool", "loopback_pool"):
            pool = dc.get(attribute)
            if isinstance(pool, dict):
                usages.extend(
                    _container_usages(
                        pool,
                        owner=owner,
                        design_remedy=design_remedy,
                        design=design,
                        pods=pods,
                        pod_of_pool=pod_of_pool,
                    )
                )
        management = dc.get("management_pool")
        if isinstance(management, dict):
            usages.append(
                _address_pool(
                    management, owner=owner, remedy=design_remedy, consumer="management address", foreign=foreign
                )
            )
    return usages


def _container_usages(
    pool: dict[str, Any],
    *,
    owner: str,
    design_remedy: str,
    design: dict[str, Any],
    pods: int,
    pod_of_pool: dict[str, str],
) -> list[PoolUsage]:
    """A DC prefix pool (the pods' slices are its children) and the DC-level address pools sliced from it."""
    resources = _resources(pool)
    sub_pools: list[PoolUsage] = []
    pod_slices: list[tuple[IPNetwork, str]] = []
    used = 0
    largest_free = 0
    for net, resource in resources:
        children = _children(resource)
        used += sum(child.num_addresses for child, _ in children)
        largest_free = max(largest_free, _largest_free(net, [child for child, _ in children]))
        for child, node in children:
            for owner_pool in node.get("resource_pool") or []:
                pool_id = str(owner_pool.get("id"))
                if pool_id in pod_of_pool:
                    pod_slices.append((child, pod_of_pool[pool_id]))
                elif owner_pool.get("typename") == "CoreIPAddressPool":
                    child_used = int(node.get("ip_addresses") or 0)
                    sub_pools.append(
                        PoolUsage(
                            id=pool_id,
                            name=_name(owner_pool),
                            kind="CoreIPAddressPool",
                            slices=(child,),
                            used=child_used,
                            capacity=_assignable(child),
                            owner=owner,
                            remedy=design_remedy,
                            exhausted=_leaf_exhausted(child_used, _assignable(child), "fabric-tier loopback"),
                        )
                    )

    max_pods = design.get("max_pods")
    exhausted = None
    remedy = design_remedy
    if pod_slices:
        biggest, biggest_pod = min(pod_slices, key=lambda item: (-item[0].num_addresses, item[1]))
        remedy = f"{design_remedy} or move pod '{biggest_pod}' to another data center"
        if isinstance(max_pods, int) and pods < max_pods and largest_free < biggest.num_addresses:
            exhausted = (
                f"there is no free /{biggest.prefixlen} for another pod while design '{_name(design)}' allows "
                f"{max_pods} pods and {pods} exist"
            )
    usage = PoolUsage(
        id=str(pool.get("id")),
        name=_name(pool),
        kind="CoreIPPrefixPool",
        slices=tuple(net for net, _ in resources),
        used=used,
        capacity=sum(net.num_addresses for net, _ in resources),
        owner=owner,
        remedy=remedy,
        exhausted=exhausted,
    )
    return [usage, *sub_pools]


class CheckPoolUtilization(WarningCheck):
    """Every fabric pool has room: WARNING at >= 80 % used, ERROR when the next allocation is impossible."""

    query = "pool_utilization"

    def validate(self, data: Any) -> None:
        for usage in pool_usages(data):
            share = f"{usage.used}/{usage.capacity} addresses"
            if not usage.slices:
                self._error(usage, f"{usage.label()} has no slice: nothing can be allocated from it; {usage.remedy}.")
            elif usage.used > usage.capacity:
                self._error(
                    usage,
                    f"{usage.label()} holds {share} ({usage.percent:.1f}%): allocations overlap or sit outside its "
                    f"assignable range; fix the IPAM data, then {usage.remedy}.",
                )
            elif usage.exhausted:
                self._error(
                    usage,
                    f"{usage.label()} is {usage.percent:.1f}% used ({share}) and {usage.exhausted}: {usage.remedy}.",
                )
            elif usage.percent >= WARNING_PERCENT:
                self.log_warning(message=f"{usage.label()} is {usage.percent:.1f}% used ({share}): {usage.remedy}.")

    def _error(self, usage: PoolUsage, message: str) -> None:
        self.log_error(message=message, object_id=usage.id, object_type=usage.kind)
