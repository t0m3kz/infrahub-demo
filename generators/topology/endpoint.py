"""Infrastructure generator for endpoint device connectivity.

This generator connects endpoint devices (servers) to network infrastructure
based on deployment type (middle_rack, tor, mixed). It follows deployment-aware
routing logic with proper interface type matching and dual-homing support.

Features:
- Suite-level device distribution
- Speed-aware interface matching (25G/100G)
- Connection fingerprinting for idempotency
- Pre-execution validation
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from infrahub_sdk.protocols import CoreGroup
from typing_extensions import TypedDict

from utils.data_cleaning import clean_data
from utils.limits import GROUP_APP_COMPONENTS

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..endpoint import EndpointUplinkMixin
from ..helpers.cabling import cable_endpoint_device_names
from ..pod_config import pod_deployment_type
from ..pools import PoolMixin
from ..protocols import (
    AppComponent,
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    ManagedControllerVirtual,
)
from ..types import ConnectionFingerprint
from .endpoint_lag import EndpointLagMixin
from .endpoint_targets import FREE_OR_CABLED_INTERFACE_STATUSES, EndpointTargetsMixin

# Tags the switch ports a component's instances are cabled to with its segment
# (generators/topology/app_instance_segment.py). A component usually exists
# before its server is cabled, and its own triggers do not fire when cabling
# lands later, so a run that cables this endpoint dispatches it for every
# component instanced on the endpoint or on a VM it hosts.
_COMPONENT_SEGMENT_GENERATOR = "add_app_component_segment"
# Its `targets` in .infrahub.yml are the GROUP_APP_COMPONENTS group: only members are dispatched.
# The endpoint NIC roles this generator cables (and whose far ends add_app_component_segment
# tags): plain uplinks and bond members.
ENDPOINT_NIC_ROLES = ("uplink", "lag")
# Instance kinds placed on a physical host through hosting_device. Their typenames are
# utils/app_instances.HOSTED_INSTANCE_TYPENAMES (what physical_host resolves for the generator
# and the NX-OS GPO transform); tests/unit/test_app_instances.py pins the two together.
HOSTED_INSTANCE_KINDS = (DcimVirtualDevice, ManagedControllerVirtual)


def _is_generated_uplink_cable(cable: Any, own_ends: set[str]) -> bool:
    """Whether create_cabling made this cable for one of this endpoint's
    uplinks: its name joins the two "<device>-<interface>" ends with "__",
    and one of them is ours. Hand-loaded cables use other names (CBL-...)."""
    return bool(own_ends & set(str(cable.name.value).split("__")))


class EndpointInterfaceData(TypedDict, total=False):
    """One interface projection (``PhysicalInterfaceFields`` in endpoint.gql)."""

    id: str
    name: str
    interface_type: str | None
    role: str | None
    status: str | None
    cable: dict[str, Any] | None


class EndpointPodRackData(TypedDict):
    """One rack of the pod (``racks`` of ``PodFields`` in endpoint.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str


class EndpointPodData(TypedDict, total=False):
    """Pod projection (``PodFields`` in endpoint.gql)."""

    id: str
    name: str
    design: dict[str, Any]
    index: int
    parent: dict[str, Any]
    racks: list[EndpointPodRackData]


class EndpointRackData(TypedDict, total=False):
    """Rack projection (``RackFields`` in endpoint.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str
    pod: EndpointPodData


class EndpointBondData(TypedDict):
    """One bond of the server (``BondFields`` in endpoint.gql)."""

    id: str
    name: str
    role: str | None
    lag_id: int | None
    member_interfaces: list[dict[str, str]]


class EndpointDeviceData(TypedDict, total=False):
    """Top-level endpoint device (``DcimDevice`` in endpoint.gql)."""

    id: str
    name: str
    role: str | None
    deployment: dict[str, Any] | None
    rack: EndpointRackData | None
    interfaces: list[EndpointInterfaceData]


def _endpoint_device_name(intf: Any) -> str | None:
    """The interface's device name (queried with ``include=["device"]``), or None without a device peer."""
    device = intf.device.peer
    return str(device.name.value) if device else None


class EndpointConnectivityGenerator(
    EndpointLagMixin, EndpointTargetsMixin, EndpointUplinkMixin, PoolMixin, CablingMixin, CommonGenerator
):
    """Generate connectivity for endpoint devices based on deployment patterns.

    Deployment strategies:
    - middle_rack: Connect to Leaf switches in network rack in same row
    - tor: Connect to ToR switches in same rack, fallback to same row
    - mixed: Connect to ToR devices in same rack, fallback to middle rack leafs in same row

    Features:
    - Suite-level device distribution
    - Connection fingerprinting for idempotency
    - Pre-execution validation
    - Interface type and role matching (customer ↔ access)
    - Dual-homing across consecutive device pairs
    - Uses CablingPlanner and CommonGenerator.create_cabling()
    - Speed-aware: groups by speed first and only connects matching speeds
    """

    data: EndpointDeviceData

    # Same Callable-attribute type the mixin declares (a plain def there would shadow this one in the MRO).
    _extract_device_name: Callable[[Any], str | None] = staticmethod(_endpoint_device_name)

    def _extract_cabled_switch_names(self, interfaces: list[DcimPhysicalInterface]) -> set[str]:
        """Far-end switch names of this endpoint's already-cabled interfaces."""
        return cable_endpoint_device_names(interfaces) - {self.data["name"]}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.planned_connections: set[ConnectionFingerprint] = set()
        self._free_interfaces: list[DcimPhysicalInterface] = []  # Free interfaces without cables
        self._already_connected: bool = False  # True when endpoint already has existing cables
        # Switch device names this endpoint is already cabled to (from a prior
        # run) — biases device-pair selection so additional free ports land on
        # the SAME pair rather than a re-derived (possibly different) one.
        self._existing_switch_names: set[str] = set()

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate endpoint device connectivity based on deployment type."""
        try:
            deployment_list = clean_data(data).get("DcimDevice", [])
            if not deployment_list:
                self.logger.error("No Endpoint Device data found in GraphQL response")
                return

            # Filter out empty interface nodes (interfaces matching neither the
            # PhysicalInterfaceFields nor the BondFields fragment — e.g. a virtual
            # interface) and split the server's own bonds (DcimLAGInterface, the ones
            # with a lag_id) from its DcimPhysicalInterface members.
            deployment_data = deployment_list[0]
            bonds: list[EndpointBondData] = []
            if "interfaces" in deployment_data:
                present = [intf for intf in deployment_data["interfaces"] if intf]
                bonds = [intf for intf in present if "lag_id" in intf and intf.get("role") == "lag"]
                deployment_data["interfaces"] = [intf for intf in present if "lag_id" not in intf]

            self.data = cast(EndpointDeviceData, deployment_data)
            # No Pydantic validation left to catch a malformed/partial GraphQL
            # response — force-read every field generate() treats as required
            # here, inside the try, so a missing one raises KeyError in the
            # same place the old EndpointModel(**deployment_data) construction did.
            endpoint_name = self.data["name"]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        if not self.data.get("rack"):
            self.logger.error(f"Endpoint {endpoint_name} has no rack assigned - cannot determine connectivity")
            return

        rack = self.data["rack"]
        assert rack is not None
        pod = rack.get("pod") or {}
        if "design" not in pod:
            # Same shape as RackGenerator's guard: LocationRack.pod takes any
            # TopologyRackHosting peer, and endpoint.gql's PodFields fragment is on
            # TopologyPod, so a colocation cage rack (pod = TopologyColocationZone)
            # arrives as an empty pod dict. There is no fabric to dual-home into —
            # colocation kit is cabled explicitly in the object data (see
            # data/demos/30_all/06_customer_boarding/*/colo/*/02_compute.yml). Every
            # role=endpoint device reaches this generator regardless of where it
            # sits, since trigger-endpoint-generator-on-created
            # (data/events/99_actions.yml) matches on the attribute, not the site.
            self.logger.info(
                "Endpoint %s: rack %s does not hang off a DC-fabric pod, nothing to cable",
                endpoint_name,
                rack.get("name"),
            )
            return
        deployment_type = pod_deployment_type(pod["design"])
        self.pod_name = pod["name"].lower()
        pod_id = pod["id"]
        dc = pod["parent"]
        self.deployment_id = dc["id"]
        self.fabric_name = dc["name"].lower()

        self.logger.info(f"Generating connectivity for endpoint {self.data['name']} in {deployment_type} deployment")

        # Point the endpoint device's deployment at the pod. Never tracked: the
        # server is data-loaded and is this run's target, not its output — a
        # tracked server is deleted by the first run that does not save it.
        if (self.data.get("deployment") or {}).get("id") != pod_id:
            endpoint_device = await self.client.get(kind=DcimPhysicalDevice, id=self.data["id"])
            endpoint_device.deployment = pod_id
            await endpoint_device.save(update_group_context=False)
            self.logger.info(f"Updated {self.data['name']} deployment to pod {self.pod_name}")

        # Rack-to-spine cabling avoids port collisions via a deterministic per-device
        # offset (calculate_cabling_offsets); endpoint cabling has no such offset — it
        # picks the first free port it sees. Concurrent siblings (the backend fans
        # multiple endpoint generator instances out via asyncio.gather — see
        # acquire_resource_lock's docstring) targeting the same rack/row could both
        # pick the same "free" port before either saves its cable. Lock on
        # (pod, row) rather than just the rack: the tor/mixed fallback path also
        # reaches into every rack in the same row, not just this endpoint's own.
        cabling_lock_key = f"endpoint-cabling-pod-{pod_id}-row-{rack['row_index']}"

        # LAG-based endpoints (server declares role=lag physical NICs bundled into
        # DcimLAGInterface bond(s) in its own object-load data) get switch-side
        # port-channels wired into the target pair's ManagedMLAG domain instead of
        # plain 1:1 uplink cabling — see _process_lag_endpoint_connections.
        if bonds:
            # Members come from their own query, not the bonds' peers, so their
            # cables and interface types are loaded.
            lag_nics: list[DcimPhysicalInterface] = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__ids=[self.data["id"]],
                role__value="lag",
                include=["cable"],
            )
            nic_by_id = {nic.id: nic for nic in lag_nics}
            members_by_bond = {
                bond["name"]: [nic_by_id[peer["id"]] for peer in bond["member_interfaces"] if peer["id"] in nic_by_id]
                for bond in bonds
            }
            # Remember which switches earlier bonds are already cabled to (see
            # _extract_cabled_switch_names) so additional bonds land on the SAME
            # pair rather than a freshly re-derived one.
            self._existing_switch_names = self._extract_cabled_switch_names(
                [nic for nic in lag_nics if nic.cable and nic.cable.id]
            )
            # The members stand in for the plain path's free uplinks: their
            # types narrow the switch-port search to ports of the same speed.
            self._free_interfaces = lag_nics
            cabled_before = {nic.id for nic in lag_nics if nic.cable and nic.cable.id}

            async with self.resource_lock(cabling_lock_key):
                await self._process_lag_endpoint_connections(members_by_bond, deployment_type)
            if len(cabled_before) < len(lag_nics):
                await self._fan_out_component_segments(cabled_before)
            return

        # Get all uplink interfaces from endpoint device (idempotency)
        # Note: Endpoint devices use "uplink" role, ToR/Leaf devices use "customer" role
        all_endpoint_interfaces: list[DcimPhysicalInterface] = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[self.data["id"]],
            role__value="uplink",
            status__values=FREE_OR_CABLED_INTERFACE_STATUSES,
            include=["device", "interface_type", "cable"],
        )

        # Filter to only interfaces without cables (for idempotency - only connect new interfaces)
        endpoint_interfaces: list[DcimPhysicalInterface] = [
            intf for intf in all_endpoint_interfaces if not (intf.cable and intf.cable.id)
        ]
        already_cabled_interfaces = [intf for intf in all_endpoint_interfaces if intf.cable and intf.cable.id]

        existing_connections = len(all_endpoint_interfaces) - len(endpoint_interfaces)

        # Remember which switches this endpoint is already cabled to, so
        # additional free ports (extra NICs, re-runs) land on the SAME pair
        # instead of a freshly re-derived one — see _process_endpoint_connections.
        self._existing_switch_names = self._extract_cabled_switch_names(already_cabled_interfaces)

        # Re-save the cables a prior run made: the run tracks only what it saves,
        # so a cable a rerun finds and skips is one delete_unused_nodes removes.
        # Only this generator's own cables, recognized by create_cabling's
        # naming convention: a cable loaded as data on an uplink is not this
        # run's output, so it is left alone. One batch fetch instead of one
        # get() per cable; the saves are independent (distinct cables), so they
        # run concurrently too.
        cable_ids = sorted({str(intf.cable.id) for intf in already_cabled_interfaces})
        if cable_ids:
            cables = await self.client.filters(kind=DcimCable, ids=cable_ids)
            own_ends = {f"{endpoint_name}-{intf.name.value}" for intf in already_cabled_interfaces}
            own = [cable for cable in cables if _is_generated_uplink_cable(cable, own_ends)]
            await asyncio.gather(*(cable.save(allow_upsert=True) for cable in own))

        if not endpoint_interfaces:
            if existing_connections > 0:
                self.logger.info(
                    f"Endpoint {self.data['name']} already has {existing_connections} connection(s) - "
                    "all interfaces connected, skipping"
                )
            else:
                self.logger.info(f"Endpoint {self.data['name']} has no uplink interfaces, skipping")
            return

        if existing_connections > 0:
            self.logger.info(
                f"Endpoint {self.data['name']} already has {existing_connections} connection(s) - "
                f"will create connections for {len(endpoint_interfaces)} free interface(s)"
            )

        # Store free interfaces and connection state for use in connection methods
        self._free_interfaces = endpoint_interfaces
        self._already_connected = existing_connections > 0

        async with self.resource_lock(cabling_lock_key):
            all_target_interfaces = await self._resolve_target_interfaces(deployment_type)
            if all_target_interfaces:
                await self._process_endpoint_connections(all_target_interfaces)
        if all_target_interfaces:
            await self._fan_out_component_segments({intf.id for intf in already_cabled_interfaces})

    async def _fan_out_component_segments(self, cabled_before: set[str]) -> None:
        """Dispatch add_app_component_segment for the components on this endpoint
        once this run has cabled at least one of its NICs.

        `cabled_before` holds the ids of the NICs that already had a cable when
        the run started. Only a NIC cabled by this run counts, so a rerun that
        finds everything wired (or could not wire anything) dispatches nothing.
        The components are those whose instances include this endpoint or a VM
        it hosts, limited to members of the definition's target group. Fired
        without waiting: each run takes its segment's lock itself.
        """
        endpoint_id = self.data["id"]
        nics = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[endpoint_id],
            role__values=list(ENDPOINT_NIC_ROLES),
            include=["cable"],
        )
        if not {nic.id for nic in nics if nic.cable and nic.cable.id} - cabled_before:
            return

        hosted = await asyncio.gather(
            *(self.client.filters(kind=kind, hosting_device__ids=[endpoint_id]) for kind in HOSTED_INSTANCE_KINDS)
        )
        instance_ids = [endpoint_id, *(instance.id for instances in hosted for instance in instances)]
        components = await self.client.filters(
            kind=AppComponent, instances__ids=instance_ids, include=["member_of_groups"]
        )
        if not components:
            return

        target_group = await self.client.get(kind=CoreGroup, name__value=GROUP_APP_COMPONENTS)
        component_ids = sorted(
            {
                component.id
                for component in components
                if target_group.id in {group.id for group in component.member_of_groups.peers}
            }
        )
        if len(component_ids) < len(components):
            self.logger.warning(
                f"Endpoint {self.data['name']}: {len(components) - len(component_ids)} component(s) on it are not "
                f"in {GROUP_APP_COMPONENTS}, so {_COMPONENT_SEGMENT_GENERATOR} cannot run for them"
            )
        await self.run_generator(_COMPONENT_SEGMENT_GENERATOR, component_ids, wait=False)
