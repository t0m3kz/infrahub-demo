"""Rack mixin for rack generator context bootstrap and role/deployment compatibility."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import logging

from .helpers import DeviceNameContext, DeviceNamingConfig
from .helpers.routing import RoutingStrategy, p2p_is_ipv6, underlay_is_ipv6
from .helpers.template_interfaces import template_interface_names_by_role
from .pod_config import spine_slot_role, spine_slot_templates
from .protocols import LocationRack, TopologyPod
from .types import RoutingOptions

_POD_POOL_MAX_RETRIES = 10
_POD_POOL_RETRY_DELAY = 3.0
_POD_POOL_RETRY_CAP = 20.0
_POD_POOL_RETRY_JITTER = 0.25

# Pod-level pools this generator cannot proceed without, in the order add_pod
# writes them. asn_pool comes last and is conditional — see _wait_for_pod_pools.
_POD_POOL_FIELDS = ("loopback_pool", "prefix_pool", "asn_pool")

# Underlays that allocate a per-device ASN from the pod's ASN pool. An OSPF
# underlay never does, so a missing asn_pool is not worth waiting for there.
_EBGP_UNDERLAY_STRATEGIES = frozenset({RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value})

# Roles whose generation reads _spine_device_names/_spine_interfaces: "leaf"
# and "tor" cable to the pod's spines, and "access_leaf" opens a second,
# underlay-less overlay EVPN session straight to them. An l2-leaf-only rack
# touches neither, so spine info it cannot derive is genuinely harmless there
# and only there.
_SPINE_DEPENDENT_ROLES: frozenset[str] = frozenset({"leaf", "tor", "access_leaf"})

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
# - border-leaf/firewall/load-balancer no longer live at rack level — they're
#   declared on TopologyDataCenter's own fabric_templates and placed/cabled
#   by dc.py's _generate_dc_scoped_fabric_devices, split across the DC's pods.
ROLES_BY_DEPLOYMENT_TYPE: dict[str, frozenset[str]] = {
    "tor": frozenset({"tor"}),
    "mixed": frozenset({"leaf", "tor", "l2_leaf", "access_leaf"}),
    "middle_rack": frozenset({"leaf", "l2_leaf", "access_leaf"}),
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

    def _present_roles(self) -> set[str]:
        """Implemented by RackGenerator; declared here for static type checking."""
        raise NotImplementedError

    def _derive_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive spine (or border-spine, in micro-fabric mode) device names
        and interface names from query data.

        The rack.gql query already fetches pod.fabric_templates (role="spine"
        or role="border-spine" — the two fill the same slot, see
        generators/pod_config.py's spine_slot_templates) and all naming
        indexes (dc.index, pod.index). The pod generator always creates
        spine-slot devices with strategy="standard" and
        indexes=[dc.index, pod.index], so names are deterministic - no API
        call needed.

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        pod = self.data["pod"]
        dc = pod["parent"]
        fabric_templates = pod.get("fabric_templates", [])
        spine_entries = spine_slot_templates(fabric_templates)
        spine_role = spine_slot_role(fabric_templates)

        if not spine_entries:
            raise RuntimeError(
                f"Rack {self.data['name']}: Cannot derive spine info - no spine/border-spine fabric_templates entries"
            )

        naming = DeviceNamingConfig(strategy=dc.get("naming_convention", "standard"))
        spine_indexes = [dc["index"], pod["index"]]
        device_names: list[str] = []
        for entry in spine_entries:
            device_names.extend(
                naming.format_device_name(
                    DeviceNameContext.from_indexes(
                        fabric_name=self.fabric_name,
                        device_role=spine_role,
                        role_index=idx,
                        indexes=spine_indexes,
                    )
                )
                for idx in range(1, entry["quantity"] + 1)
            )
        device_names = sorted(device_names)

        # Interface names come from the first entry's template only — same accepted
        # limitation as dc.py's/pod.py's super-spine/spine device creation: assumes
        # every spine template shares consistent downlink-interface naming.
        first_template_interfaces = spine_entries[0]["template"].get("interfaces", [])
        interface_names = template_interface_names_by_role(
            interfaces=first_template_interfaces,
            role="downlink",
        )
        if not interface_names:
            interface_names = template_interface_names_by_role(
                interfaces=first_template_interfaces,
                role=None,
            )
        interface_names = sorted(interface_names)

        if not interface_names:
            raise RuntimeError(f"Rack {self.data['name']}: Spine template has no downlink interfaces")

        self.logger.info(
            f"Derived {len(device_names)} spine device names with "
            f"{len(interface_names)} downlink interface(s) from query data"
        )
        return device_names, interface_names

    def _missing_pod_pools(self, pod: dict[str, Any], *, needs_asn_pool: bool) -> list[str]:
        """Which of this pod's required pools are still unresolved."""
        return [
            field
            for field in _POD_POOL_FIELDS
            if not (pod.get(field) or {}).get("id") and (field != "asn_pool" or needs_asn_pool)
        ]

    async def _wait_for_pod_pools(self, pod: dict[str, Any], *, needs_asn_pool: bool) -> dict[str, Any]:
        """Retry-refetch the pod directly until add_pod has written its pools.

        A rack's own created-trigger can fire and reach here before add_pod's
        created-trigger task is even visible in the task list — nesting racks
        under their pod in one object-load mutation removed the incidental
        delay that made wait_for_parent_generator_and_refetch's task-list check
        reliable in practice. Checking the pod NODE's actual persisted state
        (not the task list) closes that race regardless of task-list indexing
        timing.

        asn_pool has to be waited on alongside the other two, not assumed to
        arrive with them: add_pod creates the loopback/prefix pools first and
        only links the DC's fabric ASN pool onto the pod afterwards (see
        generators/topology/pod.py). A rack that stopped polling the moment
        loopback/prefix appeared kept the None asn_pool from its own query
        snapshot, so _prepare_generation_context built RoutingOptions without a
        pool — and RoutingPlanner then emitted "No ASN pool for <device>" as a
        *warning*, created no AS and therefore no BGP process for any device in
        the rack, while the run still reported "generation completed". Observed
        on DC12's second network rack in a 30_all load: four devices cabled,
        addressed and entirely unrouted, with no failed task to show for it.
        """
        for attempt in range(_POD_POOL_MAX_RETRIES):
            pod_obj = await self.client.get(kind=TopologyPod, id=pod["id"], include=list(_POD_POOL_FIELDS))
            for field in _POD_POOL_FIELDS:
                if pool_id := getattr(pod_obj, field).id:
                    pod[field] = {"id": pool_id}

            missing = self._missing_pod_pools(pod, needs_asn_pool=needs_asn_pool)
            if not missing:
                return pod
            if attempt < _POD_POOL_MAX_RETRIES - 1:
                delay = self._retry_delay(
                    _POD_POOL_RETRY_DELAY, attempt, cap=_POD_POOL_RETRY_CAP, jitter=_POD_POOL_RETRY_JITTER
                )
                self.logger.info(
                    f"Rack {self.data['name']}: Pod {pod['name']} {', '.join(missing)} not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_POD_POOL_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
        return pod

    async def _prepare_generation_context(self) -> None:
        """Compute and store shared context needed by every per-role generation method."""
        pod = self.data["pod"]
        dc = pod["parent"]
        self.deployment_id = dc["id"]
        self.pod_name = pod["name"].lower()
        self.fabric_name = dc["name"].lower()

        needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in _EBGP_UNDERLAY_STRATEGIES
        if self._missing_pod_pools(pod, needs_asn_pool=needs_asn_pool):
            pod = await self._wait_for_pod_pools(pod, needs_asn_pool=needs_asn_pool)

        if missing_pools := self._missing_pod_pools(pod, needs_asn_pool=needs_asn_pool):
            # Refuse rather than carry on. There is no legitimate state in which
            # these are absent: add_dc creates the fabric ASN pool, add_pod
            # creates the loopback/prefix pools and links the ASN pool onto the
            # pod, and the retry loop above already covers the visibility lag.
            # Carrying on builds a rack whose devices are cabled and addressed
            # but have no BGP process — indistinguishable from a healthy rack
            # unless you count processes. Raising fails this generator's task,
            # which is how such a rack becomes visible at all. Nothing has been
            # created at this point, so the failed run leaves no partial state.
            message = (
                f"Rack {self.data['name']}: Pod {pod['name']} {', '.join(missing_pools)} not found after "
                f"{_POD_POOL_MAX_RETRIES} attempts — refusing to generate devices that would have no routing. "
                f"Run the pod generator first: infrahubctl generator generate_pod name={pod['name']}"
            )
            self.logger.error(message)
            raise RuntimeError(message)

        dc_management_pool = dc.get("management_pool")
        self._management_pool_id = dc_management_pool["id"] if dc_management_pool else None
        pod_loopback_pool = pod.get("loopback_pool")
        self._loopback_pool_id = pod_loopback_pool["id"] if pod_loopback_pool else None

        suite = self.data["parent"]
        self._device_indexes: list[int] = [
            dc["index"],
            pod["index"],
            suite["index"],
            self.data["row_index"],
            self.data["index"],
        ]

        if pod["deployment_type"] == "tor":
            self.logger.info(
                f"ToR rack {self.data['name']}: using suite={suite['index']}, row={self.data['row_index']}, "
                f"rack_index={self.data['index']}"
            )

        self._naming_conv = cast(
            Literal["standard", "hierarchical", "flat", "computed"],
            dc.get("naming_convention", "standard"),
        )
        self._is_ipv6 = underlay_is_ipv6(dc.get("underlay_protocol", "ipv6"))

        self._spine_role: Literal["spine", "border-spine"] = spine_slot_role(pod.get("fabric_templates", []))
        self._spine_device_names: list[str] = []
        self._spine_interfaces: list[str] = []
        try:
            self._spine_device_names, self._spine_interfaces = self._derive_spine_info()
        except RuntimeError as exc:
            # logger.error() raises GeneratorError (FailOnErrorLogger), so this
            # already failed the task — but it failed it for every rack, including
            # an l2-leaf-only one, which reads neither attribute and does not need
            # a spine to exist at all. Fail only the racks that actually cable or
            # route to a spine; tolerate the rest at INFO with the defaults above.
            if spine_dependent := self._present_roles() & _SPINE_DEPENDENT_ROLES:
                self.logger.error(
                    f"{exc} — refusing to generate role(s) {sorted(spine_dependent)}, which cannot be "
                    f"cabled or routed to a spine that could not be derived."
                )
                raise
            self.logger.info(f"{exc} — rack has no spine-dependent roles, continuing.")

        routing_options: RoutingOptions = RoutingOptions(design=dc)
        pod_asn_pool = pod.get("asn_pool")
        if pod_asn_pool and pod_asn_pool.get("id"):
            routing_options["asn_pool"] = pod_asn_pool["id"]

        pod_prefix_pool = pod.get("prefix_pool")
        self._technical_pool_id = pod_prefix_pool["id"] if pod_prefix_pool else None
        self._p2p_prefix_length = 127 if p2p_is_ipv6(dc.get("underlay_protocol", "ipv6")) else 31
        self._routing_options = routing_options
