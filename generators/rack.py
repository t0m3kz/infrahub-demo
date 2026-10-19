"""Rack mixin for rack generator context bootstrap and role/deployment compatibility."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import logging

from .helpers import DeviceNameContext, DeviceNamingConfig
from .helpers.routing import (
    RoutingStrategy,
    loopback_prefix_length,
    p2p_prefix_length,
)
from .helpers.template_interfaces import template_interface_names_by_role
from .pod_config import pod_deployment_type, spine_slot_role, spine_slot_templates
from .protocols import TopologyDataCenter, TopologyPod
from .types import RoutingOptions, naming_convention_of

_POD_POOL_MAX_RETRIES = 10
_POD_POOL_RETRY_DELAY = 3.0
# The pod's own generators — a rack waits for an in-flight one before reading
# pod-level data (spines, pools).
POD_GENERATORS = ("add_pod", "pod_rack_cascade")

# Pod-level pools this generator cannot proceed without. The ASN pool is not
# one of them: it is the parent DC's fabric_asn_pool, conditional — see _wait_for_pools.
_POD_POOL_FIELDS = ("loopback_pool", "prefix_pool")

# Underlays that allocate a per-device ASN from the DC's fabric_asn_pool. An OSPF
# underlay never does, so a missing pool is not worth waiting for there.
_EBGP_UNDERLAY_STRATEGIES = frozenset({RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value})


def spine_bgp_readiness_role(dc: Mapping[str, Any]) -> str:
    """The ManagedBGP process role the pod's spines must own before a rack bootstraps.

    The rack's leaf sessions peer with the spine process of this role, so only
    a role the strategy actually creates may be waited for:

    * eBGP underlay (ebgp-ebgp, ebgp-ibgp): ``underlay``, pre-seeded by add_pod
      before cabling.
    * ospf-ibgp: no underlay BGP exists (the underlay is OSPF); the spines
      carry only the shared-AS ``overlay`` process.
    """
    strategy = dc.get("routing_strategy", RoutingStrategy.EBGP_EBGP.value)
    return "underlay" if strategy in _EBGP_UNDERLAY_STRATEGIES else "overlay"


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
    # CommonGenerator.wait_for_parent_generator_and_refetch — annotation only.
    wait_for_parent_generator_and_refetch: Callable[..., Awaitable[dict | None]]

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

        naming = DeviceNamingConfig(strategy=naming_convention_of(dc, lower=False))
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

    def _missing_pools(self, pod: dict[str, Any], dc: Mapping[str, Any], *, needs_asn_pool: bool) -> list[str]:
        """Which required pools are still unresolved, as ``pod.<field>`` / ``dc.fabric_asn_pool`` labels."""
        missing = [f"pod.{field}" for field in _POD_POOL_FIELDS if not (pod.get(field) or {}).get("id")]
        if needs_asn_pool and not (dc.get("fabric_asn_pool") or {}).get("id"):
            missing.append("dc.fabric_asn_pool")
        return missing

    async def _wait_for_pools(self, pod: dict[str, Any], dc: dict[str, Any], *, needs_asn_pool: bool) -> None:
        """Retry-refetch the pod (and, if needed, the DC) until its pools are persisted; updates both in place.

        The rack's query snapshot can predate the writes it depends on: racks
        are created in the same object-load as their DC and pod, so the rack's
        created-trigger can run before add_dc wrote ``fabric_asn_pool`` or
        add_pod wrote the loopback/prefix pools. Checking the nodes' persisted
        state (not the task list, which may not show the pod's task yet) closes
        that race.

        The DC's fabric_asn_pool needs no wait of its own beyond the pod's:
        add_pod only writes the pod pools after it has seen the DC pool, so
        visible pod pools imply a persisted DC pool. It is still refetched here
        when the snapshot lacks it, because a rack that went on without it
        built RoutingOptions with no pool — RoutingPlanner then only warned,
        created no AS and no BGP process for the rack's devices, and the run
        still reported success (DC12's second network rack in a 30_all load).
        """
        for attempt in range(_POD_POOL_MAX_RETRIES):
            missing = self._missing_pools(pod, dc, needs_asn_pool=needs_asn_pool)
            if any(label.startswith("pod.") for label in missing):
                pod_obj = await self.client.get(kind=TopologyPod, id=pod["id"], include=list(_POD_POOL_FIELDS))
                for field in _POD_POOL_FIELDS:
                    if pool_id := getattr(pod_obj, field).id:
                        pod[field] = {"id": pool_id}
            if needs_asn_pool and not (dc.get("fabric_asn_pool") or {}).get("id"):
                dc_obj = await self.client.get(kind=TopologyDataCenter, id=dc["id"], include=["fabric_asn_pool"])
                if pool_id := dc_obj.fabric_asn_pool.id:
                    dc["fabric_asn_pool"] = {"id": pool_id}

            missing = self._missing_pools(pod, dc, needs_asn_pool=needs_asn_pool)
            if not missing:
                return
            # Under load add_pod's own task can surface only now, queued behind
            # other fabrics' work: once it is in flight, wait for it outright
            # rather than spending blind retries until they run out.
            if await self.wait_for_parent_generator_and_refetch(POD_GENERATORS, pod["id"]) is not None:
                continue
            if attempt < _POD_POOL_MAX_RETRIES - 1:
                delay = self._retry_delay(_POD_POOL_RETRY_DELAY, attempt)
                self.logger.info(
                    f"Rack {self.data['name']}: {', '.join(missing)} not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_POD_POOL_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)

    async def _prepare_generation_context(self) -> None:
        """Compute and store shared context needed by every per-role generation method."""
        pod = self.data["pod"]
        dc = pod["parent"]
        self.deployment_id = dc["id"]
        self.pod_name = pod["name"].lower()
        self.fabric_name = dc["name"].lower()

        needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in _EBGP_UNDERLAY_STRATEGIES
        if self._missing_pools(pod, dc, needs_asn_pool=needs_asn_pool):
            await self._wait_for_pools(pod, dc, needs_asn_pool=needs_asn_pool)

        if missing_pools := self._missing_pools(pod, dc, needs_asn_pool=needs_asn_pool):
            # Refuse rather than carry on. There is no legitimate state in which
            # these are absent: add_dc creates the fabric ASN pool, add_pod
            # creates the loopback/prefix pools, and the retry loop above
            # already covers the visibility lag. Carrying on builds a rack whose
            # devices are cabled and addressed but have no BGP process —
            # indistinguishable from a healthy rack unless you count processes.
            # Raising fails this generator's task, which is how such a rack
            # becomes visible at all. Nothing has been created at this point, so
            # the failed run leaves no partial state.
            hints = []
            if any(label.startswith("pod.") for label in missing_pools):
                hints.append(f"infrahubctl generator generate_pod name={pod['name']}")
            if "dc.fabric_asn_pool" in missing_pools:
                hints.append(f"infrahubctl generator generate_dc name={dc['name']}")
            message = (
                f"Rack {self.data['name']}: {', '.join(missing_pools)} not found after "
                f"{_POD_POOL_MAX_RETRIES} attempts — refusing to generate devices that would have no routing. "
                f"Run the generator first: {' / '.join(hints)}"
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

        if pod_deployment_type(pod["layout"]) == "tor":
            self.logger.info(
                f"ToR rack {self.data['name']}: using suite={suite['index']}, row={self.data['row_index']}, "
                f"rack_index={self.data['index']}"
            )

        self._naming_conv = naming_convention_of(dc, lower=False)
        underlay_protocol = dc.get("underlay_protocol", "ipv6")
        self._loopback_prefix_length = loopback_prefix_length(underlay_protocol)
        self._p2p_prefix_length = p2p_prefix_length(underlay_protocol)

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
        dc_asn_pool = dc.get("fabric_asn_pool")
        if dc_asn_pool and dc_asn_pool.get("id"):
            routing_options["asn_pool"] = dc_asn_pool["id"]

        pod_prefix_pool = pod.get("prefix_pool")
        self._technical_pool_id = pod_prefix_pool["id"] if pod_prefix_pool else None
        self._routing_options = routing_options
