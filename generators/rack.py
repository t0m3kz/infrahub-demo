"""Rack mixin for rack generator context bootstrap and role/deployment compatibility."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import logging

from .helpers import DeviceNamingConfig
from .models import Pool
from .protocols import LocationRack, TopologyPod
from .types import RoutingOptions

_POD_POOL_MAX_RETRIES = 10
_POD_POOL_RETRY_DELAY = 3.0
_POD_POOL_RETRY_CAP = 20.0
_POD_POOL_RETRY_JITTER = 0.25

# Which fabric_templates roles a pod's deployment_type knows how to cable.
#
# - "tor" cables to the pod spines: directly (tor deployment, cumulative
#   offset across the pod) or to the middle rack's leafs in the same row
#   (mixed deployment, calculate_cabling_offsets' mixed+tor branch). Neither
#   path exists for middle_rack — there every leaf lives in the same rack as
#   its ToRs, and _generate_tors() has no local-leaf cabling branch for it.
# - "l2_leaf"/"access_leaf" always cable to a local leaf pair — in the same
#   rack for middle_rack/network racks, or the middle rack in the same row
#   for mixed compute racks (_resolve_local_leaf_cabling_target). tor
#   deployment has no leafs anywhere in the pod, so neither role has a
#   target to cable to.
# - "firewall"/"load_balancer" cable to the DC's border-leaf pair's dedicated
#   firewall/load-balancer ports (_generate_firewalls_and_load_balancers) —
#   valid wherever "border_leaf" is valid, since they depend on it existing.
ROLES_BY_DEPLOYMENT_TYPE: dict[str, frozenset[str]] = {
    "tor": frozenset({"tor", "border_leaf", "firewall", "load_balancer"}),
    "mixed": frozenset({"leaf", "tor", "l2_leaf", "access_leaf", "border_leaf", "firewall", "load_balancer"}),
    "middle_rack": frozenset({"leaf", "l2_leaf", "access_leaf", "border_leaf", "firewall", "load_balancer"}),
}

# "tor" cables to a spine; "l2_leaf"/"access_leaf" cable to a local leaf pair.
# No cabling strategy satisfies both on the same rack, so they're mutually
# exclusive there regardless of deployment_type.
MUTUALLY_EXCLUSIVE_ROLE_GROUPS: tuple[frozenset[str], ...] = (frozenset({"tor", "l2_leaf", "access_leaf"}),)

# rack_type values for racks with no leaf template of their own — they wait
# for, and cable to, the leaf pair in the network/middle rack that shares
# their row. "tor" holds real ToRs (role="tor", cables to spines) and/or
# l2-leaf/access-leaf switches (cable to the row's leaf pair); "compute" is
# the same "no local leaf" shape used for a rack whose fabric_templates are
# l2-leaf/access-leaf only (no role="tor" — see ROLES_BY_DEPLOYMENT_TYPE).
# Checksum inheritance and row-ordering gates treat both identically; only
# the per-role cabling target (spine vs. row leaf) differs.
ROW_DEPENDENT_RACK_TYPES: frozenset[str] = frozenset({"tor", "compute"})


class RackMixin:
    """Mixin extracting rack-specific lifecycle checks and context setup."""

    # Attributes provided by CommonGenerator / RackGenerator
    client: Any
    logger: logging.Logger
    data: Any
    deployment_id: str
    pod_name: str
    fabric_name: str
    # CommonGenerator._retry_delay — annotation only (no method body), so this
    # mixin never shadows the real staticmethod at runtime via MRO.
    _retry_delay: Callable[..., float]

    async def fetch_rack_devices_with_interfaces(
        self,
        rack: LocationRack | None = None,
        role_filter: str | None = None,
        interface_role: str = "downlink",
    ) -> list[dict]:
        """Implemented by RackGenerator; declared here for static type checking."""
        raise NotImplementedError

    def _derive_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive spine device names and interface names from query data.

        The rack.gql query already fetches pod.amount_of_spines,
        pod.spine_template.interfaces(role="downlink"), and all naming
        indexes (dc.index, pod.index). The pod generator always creates
        spines with strategy="standard" and indexes=[dc.index, pod.index],
        so spine names are deterministic - no API call needed.

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        pod = self.data.pod
        dc = pod.parent
        spine_count = pod.amount_of_spines
        spine_template = pod.spine_template

        if not spine_count or not spine_template:
            raise RuntimeError(
                f"Rack {self.data.name}: Cannot derive spine info - "
                f"amount_of_spines={spine_count}, spine_template={'set' if spine_template else 'None'}"
            )

        naming = DeviceNamingConfig(strategy=dc.naming_convention)
        spine_indexes = [dc.index, pod.index]
        device_names = sorted(
            [
                naming.format_device_name(
                    self.fabric_name,
                    "spine",
                    index=idx,
                    fabric_name=self.fabric_name,
                    indexes=spine_indexes,
                )
                for idx in range(1, spine_count + 1)
            ]
        )

        interface_names = sorted(iface.name for iface in spine_template.interfaces)

        if not interface_names:
            raise RuntimeError(f"Rack {self.data.name}: Spine template has no downlink interfaces")

        self.logger.info(
            f"Derived {len(device_names)} spine device names with "
            f"{len(interface_names)} downlink interface(s) from query data"
        )
        return device_names, interface_names

    def _derive_super_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive super-spine device names and uplink interface names from query data.

        The DC generator always creates super-spines with indexes=[dc.index], so names
        are deterministic - no API call needed. Super-spine template interfaces are
        pre-filtered to role="uplink" in rack.gql.

        Returns:
            Tuple of (device_names, interface_names) for create_cabling.

        Raises:
            RuntimeError: when super-spine count or template is not set on the DC.
        """
        dc = self.data.pod.parent
        count = dc.amount_of_super_spines
        template = dc.super_spine_template

        if not count or not template:
            raise RuntimeError(
                f"Rack {self.data.name}: Cannot derive super-spine info - "
                f"amount_of_super_spines={count}, super_spine_template={'set' if template else 'None'}"
            )

        naming = DeviceNamingConfig(strategy=self.data.pod.parent.naming_convention)
        device_names = sorted(
            naming.format_device_name(
                self.fabric_name,
                "super-spine",
                index=idx,
                fabric_name=self.fabric_name,
                indexes=[dc.index],
            )
            for idx in range(1, count + 1)
        )

        interface_names = sorted(iface.name for iface in template.interfaces if iface.role == "uplink")
        if not interface_names:
            raise RuntimeError(f"Rack {self.data.name}: Super-spine template has no uplink interfaces")

        self.logger.info(
            f"Derived {len(device_names)} super-spine device names with "
            f"{len(interface_names)} uplink interface(s) from query data"
        )
        return device_names, interface_names

    async def _wait_for_pod_pools(self, pod: Any) -> Any:
        """Retry-refetch the pod directly until add_pod has written its pools.

        A rack's own created-trigger can fire and reach here before add_pod's
        created-trigger task is even visible in the task list — nesting racks
        under their pod in one object-load mutation removed the incidental
        delay that made wait_for_parent_generator_and_refetch's task-list check
        reliable in practice. Checking the pod NODE's actual persisted state
        (not the task list) closes that race regardless of task-list indexing
        timing.
        """
        for attempt in range(_POD_POOL_MAX_RETRIES):
            pod_obj = await self.client.get(kind=TopologyPod, id=pod.id, include=["loopback_pool", "prefix_pool"])
            loopback_pool_id = pod_obj.loopback_pool.id
            prefix_pool_id = pod_obj.prefix_pool.id
            if loopback_pool_id and prefix_pool_id:
                pod.loopback_pool = Pool(id=loopback_pool_id)
                pod.prefix_pool = Pool(id=prefix_pool_id)
                return pod
            if attempt < _POD_POOL_MAX_RETRIES - 1:
                delay = self._retry_delay(
                    _POD_POOL_RETRY_DELAY, attempt, cap=_POD_POOL_RETRY_CAP, jitter=_POD_POOL_RETRY_JITTER
                )
                self.logger.info(
                    f"Rack {self.data.name}: Pod {pod.name} pools not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_POD_POOL_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
        return pod

    async def _prepare_generation_context(self) -> None:
        """Compute and store shared context needed by every per-role generation method."""
        pod = self.data.pod
        dc = pod.parent
        self.deployment_id = dc.id
        self.pod_name = pod.name.lower()
        self.fabric_name = dc.name.lower()

        if not pod.loopback_pool or not pod.prefix_pool:
            pod = await self._wait_for_pod_pools(pod)

        if not pod.loopback_pool or not pod.prefix_pool:
            self.logger.error(
                f"Rack {self.data.name}: Pod {pod.name} pools not found. "
                f"Run pod generator first: infrahubctl generator generate_pod name={pod.name}"
            )

        self._management_pool_id = dc.management_pool.id if dc.management_pool else None
        self._loopback_pool_id = pod.loopback_pool.id if pod.loopback_pool else None

        suite = self.data.parent
        self._device_indexes: list[int] = [
            dc.index,
            pod.index,
            suite.index,
            self.data.row_index,
            self.data.index,
        ]

        if pod.deployment_type == "tor":
            self.logger.info(
                f"ToR rack {self.data.name}: using suite={suite.index}, row={self.data.row_index}, "
                f"rack_index={self.data.index}"
            )

        dc_design = dc.design
        self._naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            dc.naming_convention,
        )
        self._is_ipv6 = dc_design.is_ipv6 if dc_design else False

        try:
            self._spine_device_names, self._spine_interfaces = self._derive_spine_info()
        except RuntimeError as exc:
            self.logger.error(str(exc))

        routing_options: RoutingOptions = RoutingOptions(design=dc_design)
        if pod.asn_pool and pod.asn_pool.id:
            routing_options["asn_pool"] = pod.asn_pool.id

        self._technical_pool_id = pod.prefix_pool.id if pod.prefix_pool else None
        self._p2p_prefix_length = 127 if dc_design and getattr(dc_design, "p2p_ipv6", False) else 31
        self._routing_options = routing_options
