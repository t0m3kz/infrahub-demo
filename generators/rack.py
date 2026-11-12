"""Rack mixin for rack generator context bootstrap and role/deployment compatibility."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import logging
    from .common import CommonGenerator

from utils.limits import WAIT_FOR_PEER_GENERATOR

from .helpers.rack import pod_row_racks
from .helpers.routing import RoutingStrategy, loopback_prefix_length, p2p_prefix_length
from .helpers.template_interfaces import template_interface_names_by_role
from .pod_config import pod_deployment_type, pod_spine_slot_device_names, spine_slot_role, spine_slot_templates
from .protocols import DcimPhysicalDevice, DcimPhysicalInterface, LocationRack, TopologyDataCenter, TopologyPod
from .types import RoutingOptions, naming_convention_of

# The pod's own generators: a rack waits for an in-flight one before reading pod-level data
POD_GENERATORS = ("add_pod", "pod_rack_cascade")

# Pod-level pools this generator needs. The ASN pool is conditional (the parent DC's fabric_asn_pool), see
# _wait_for_pools
_POD_POOL_FIELDS = ("loopback_pool", "prefix_pool")

# Underlays that allocate a per-device ASN from the DC's fabric_asn_pool (OSPF never does)
_EBGP_UNDERLAY_STRATEGIES = frozenset({RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value})


def spine_bgp_readiness_role(dc: Mapping[str, Any]) -> str:
    """The ManagedBGP process role the pod's spines must own before a rack bootstraps. Only a role the strategy creates
    may be waited for: ``underlay`` for eBGP underlays (pre-seeded by add_pod), the shared-AS ``overlay`` for ospf-ibgp.
    """
    strategy = dc.get("routing_strategy", RoutingStrategy.EBGP_EBGP.value)
    return "underlay" if strategy in _EBGP_UNDERLAY_STRATEGIES else "overlay"


# Roles that read _spine_device_names/_spine_interfaces: "leaf" and "tor" cable to the spines, "access_leaf" peers EVPN
# to them; an l2-leaf-only rack needs neither
_SPINE_DEPENDENT_ROLES: frozenset[str] = frozenset({"leaf", "tor", "access_leaf"})

# fabric_templates roles a pod's deployment_type can cable: a tor pod has no leafs, so no target for
# l2_leaf/access_leaf.
ROLES_BY_DEPLOYMENT_TYPE: dict[str, frozenset[str]] = {
    "tor": frozenset({"tor"}),
    "mixed": frozenset({"leaf", "tor", "l2_leaf", "access_leaf"}),
    "middle_rack": frozenset({"leaf", "l2_leaf", "access_leaf"}),
}

# "tor" cables to a spine, "l2_leaf"/"access_leaf" to a local leaf pair: mutually exclusive on one rack
MUTUALLY_EXCLUSIVE_ROLE_GROUPS: tuple[frozenset[str], ...] = (frozenset({"tor", "l2_leaf", "access_leaf"}),)

# rack_type values with no leaf template of their own: they wait for, and cable to, their row's leaf pair. "tor" holds
# real ToRs and/or l2-leaf/access-leaf switches; "compute" is the same shape with l2-leaf/access-leaf only. Only the
# cabling target (spine vs row leaf) differs.
ROW_DEPENDENT_RACK_TYPES: frozenset[str] = frozenset({"tor", "compute"})


class RackMixin:
    """Rack-specific lifecycle checks and context setup."""

    # Attributes provided by CommonGenerator / RackGenerator
    client: Any
    logger: logging.Logger
    data: Any
    deployment_id: str
    pod_name: str | None
    fabric_name: str
    # Type checking only: aliases of the host's other bases, so nothing shadows them in the MRO
    if TYPE_CHECKING:
        _retry_delay = staticmethod(CommonGenerator._retry_delay)
        run_generator = CommonGenerator.run_generator
        wait_for_parent_generator_and_refetch = CommonGenerator.wait_for_parent_generator_and_refetch

    def _present_roles(self) -> set[str]:
        """Implemented by RackGenerator; declared for static type checking."""
        raise NotImplementedError

    def _derive_spine_info(self) -> tuple[list[str], list[str]]:
        """Spine (or border-spine) device names and interface names from query data, for create_cabling. The pod
        generator creates them with strategy="standard" and indexes=[dc.index, pod.index], so names are deterministic.
        Returns ``(device_names, interface_names)``.
        """
        pod = self.data["deployment"]
        spine_entries = spine_slot_templates(pod.get("fabric_templates", []))

        if not spine_entries:
            raise RuntimeError(
                f"Rack {self.data['name']}: Cannot derive spine info - no spine/border-spine fabric_templates entries"
            )

        device_names = pod_spine_slot_device_names(pod)

        # Interface names come from the first template only: assumes consistent downlink naming across spine templates
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

    async def _existing_spine_slot_names(self) -> list[str]:
        """The pod's spine-slot devices that already exist, sorted by name. Looked up by name (a border-spine is
        deployed to the DC); missing spines do not hold the rack back (create_cabling waits for their ports).
        """
        pod = self.data["deployment"]
        names = pod_spine_slot_device_names(pod)
        if not names:
            return []
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            name__values=names,
            role__value=spine_slot_role(pod.get("fabric_templates") or []),
        )
        return sorted(device.name.value for device in devices)

    def _missing_pools(self, pod: dict[str, Any], dc: Mapping[str, Any], *, needs_asn_pool: bool) -> list[str]:
        """Which required pools are still unresolved, as ``pod.<field>`` / ``dc.fabric_asn_pool`` labels."""
        missing = [f"pod.{field}" for field in _POD_POOL_FIELDS if not (pod.get(field) or {}).get("id")]
        if needs_asn_pool and not (dc.get("fabric_asn_pool") or {}).get("id"):
            missing.append("dc.fabric_asn_pool")
        return missing

    async def _wait_for_pools(self, pod: dict[str, Any], dc: dict[str, Any], *, needs_asn_pool: bool) -> None:
        """Retry-refetch the pod (and, if needed, the DC) until its pools are persisted; updates both in place. The
        rack's snapshot can predate add_dc/add_pod writes (racks load with their DC and pod), so persisted state is
        checked, not the task list. The DC pool is refetched when the snapshot lacks it: without it RoutingPlanner only
        warns and the rack gets no BGP process.
        """
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
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
            # add_pod's task may surface late behind other work: once in flight, wait for it rather than burn retries
            if await self.wait_for_parent_generator_and_refetch(POD_GENERATORS, pod["id"]) is not None:
                continue
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"Rack {self.data['name']}: {', '.join(missing)} not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)

    async def _prepare_generation_context(self) -> None:
        """Compute and store the shared context needed by every per-role generation method."""
        pod = self.data["deployment"]
        dc = pod["parent"]
        self.deployment_id = dc["id"]
        self.pod_name = pod["name"].lower()
        self.fabric_name = dc["name"].lower()

        needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in _EBGP_UNDERLAY_STRATEGIES
        if self._missing_pools(pod, dc, needs_asn_pool=needs_asn_pool):
            await self._wait_for_pools(pod, dc, needs_asn_pool=needs_asn_pool)

        if missing_pools := self._missing_pools(pod, dc, needs_asn_pool=needs_asn_pool):
            # Refuse rather than carry on: these are always present (add_dc/add_pod create them, the retry loop covers
            # lag). A rack without them has no BGP process and looks healthy; raising makes it visible, and nothing has
            # been created yet.
            hints = []
            if any(label.startswith("pod.") for label in missing_pools):
                hints.append(f"infrahubctl generator generate_pod name={pod['name']}")
            if "dc.fabric_asn_pool" in missing_pools:
                hints.append(f"infrahubctl generator generate_dc name={dc['name']}")
            message = (
                f"Rack {self.data['name']}: {', '.join(missing_pools)} not found after "
                f"{WAIT_FOR_PEER_GENERATOR.attempts} attempts — refusing to generate devices that would have no routing. "
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

        if pod_deployment_type(pod["design"]) == "tor":
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
            # logger.error() raises, which would fail every rack, even an l2-leaf-only one that needs no spine; fail
            # only racks that cable or route to a spine
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

    async def _fan_out_to_row_dependent_racks(self) -> None:
        """Fan out to the row-dependent (tor/compute) racks' own add_rack runs (mixed mode only), after the leafs they
        cable to exist. wait=False: a row-dependent rack waits for an in-flight add_rack on this network rack, so
        blocking here would deadlock.
        """
        deployment_type = pod_deployment_type(self.data["deployment"]["design"])

        # Only network racks in mixed deployment feed row-dependent racks
        if deployment_type != "mixed" or self.data["rack_type"] != "network":
            return

        # Skip racks with no leaf template (e.g. border-leaf-only racks)
        if not self.data.get("leafs"):
            self.logger.warning(f"Rack {self.data['name']} has no leaf template — skipping row-dependent rack fan-out")
            return

        # The pod projection carries every rack of the pod; racks are data, and one created later has its own trigger
        # run
        row_dependent_racks = pod_row_racks(self.data["deployment"], self.data["row_index"], ROW_DEPENDENT_RACK_TYPES)

        await self.run_generator("add_rack", [rack["id"] for rack in row_dependent_racks], wait=False)

    async def _get_leaf_devices_in_row(self, pod_id: str, row_index: int) -> tuple[list[str], list[str]]:
        """Query leaf devices in the same row and their downlink interfaces, for l2-leaf/access-leaf-to-leaf cabling.
        Retries: a row-dependent rack's trigger can fire before its row's network rack has created its leafs, so actual
        devices/interfaces are checked.
        """
        # The pod projection has the racks; only a row whose network rack is missing from it (same load) is re-read per
        # attempt
        row_racks = pod_row_racks(self.data["deployment"], row_index)
        rack_ids = [rack["id"] for rack in row_racks]
        network_rack_known = any(rack["rack_type"] == "network" for rack in row_racks)
        leaf_devices: list[Any] = []
        leaf_interfaces: list[Any] = []
        # A row-dependent rack waits for its row's leaf devices and their downlinks
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            if not network_rack_known:
                rack_ids = [
                    rack.id
                    for rack in await self.client.filters(
                        kind=LocationRack,
                        deployment__ids=[pod_id],
                        row_index__value=row_index,
                    )
                ]
            leaf_devices = await self.client.filters(
                kind=DcimPhysicalDevice,
                role__value="leaf",
                rack__ids=rack_ids,
            )
            device_names = [dev.name.value for dev in leaf_devices]
            leaf_interfaces = (
                await self.client.filters(
                    kind=DcimPhysicalInterface,
                    device__name__values=device_names,
                    role__value="downlink",
                )
                if device_names
                else []
            )
            if rack_ids and leaf_devices and leaf_interfaces:
                break
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"Rack {self.data['name']}: row {row_index} leaf devices/interfaces not ready yet "
                    f"(racks={len(rack_ids)}, leafs={len(leaf_devices)}, interfaces={len(leaf_interfaces)}) — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)

        if not rack_ids:
            self.logger.error(
                f"Rack {self.data['name']}: No racks found in row {row_index}. "
                "Cannot cable the compute rack's switches to the row leafs for mixed deployment."
            )

        if not leaf_devices:
            self.logger.error(
                f"Rack {self.data['name']}: No leaf devices found in row {row_index}. "
                "Cannot cable the compute rack's switches to the row leafs for mixed deployment."
            )

        device_names = [dev.name.value for dev in leaf_devices]

        if not leaf_interfaces:
            self.logger.error(
                f"Rack {self.data['name']}: No downlink interfaces found on leaf devices in row {row_index}. "
                "Cannot cable the compute rack's switches to the row leafs for mixed deployment."
            )

        interface_names = sorted({iface.name.value for iface in leaf_interfaces})

        self.logger.info(
            f"Found {len(device_names)} leaf devices in row {row_index} with {len(interface_names)} unique downlink interfaces"
        )
        return device_names, interface_names

    async def _resolve_local_leaf_cabling_target(
        self,
        *,
        created_leaf_devices: list[str],
        leaf_row_cache: tuple[list[str], list[str]] | None,
        devices_per_rack: int,
        role_label: str,
    ) -> tuple[list[str], list[str], int, Literal["intra_rack_middle", "intra_rack_mixed"]] | None:
        """Resolve the leaf devices/interfaces a rack's l2-leaf or access-leaf batch cables to: the local leaf pair
        (network rack) or the row's middle-rack leafs (compute rack). Returns None if unresolved (already logged); the
        caller should ``continue``.
        """
        if created_leaf_devices:
            leaf_interfaces_objects = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=created_leaf_devices,
                role__value="downlink",
            )
            if not leaf_interfaces_objects:
                self.logger.error(
                    f"Rack {self.data['name']}: No downlink interfaces on leafs — cannot cable {role_label}s."
                )
                return None
            leaf_interfaces = sorted({iface.name.value for iface in leaf_interfaces_objects})
            return created_leaf_devices, leaf_interfaces, 0, "intra_rack_middle"

        # Compute rack (mixed deployment): cable to middle-rack leafs in the same row
        pod = self.data["deployment"]
        effective_rack_index = max(0, self.data["index"] - 1)
        cabling_offset = effective_rack_index * devices_per_rack

        if leaf_row_cache is None:
            leaf_row_cache = await self._get_leaf_devices_in_row(pod_id=pod["id"], row_index=self.data["row_index"])
        leaf_device_names, leaf_interfaces = leaf_row_cache

        if not leaf_device_names:
            self.logger.error(
                f"Rack {self.data['name']}: No middle-rack leafs in row {self.data['row_index']} — "
                f"cannot cable {role_label}s."
            )
            return None

        return leaf_device_names, leaf_interfaces, cabling_offset, "intra_rack_mixed"
