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

from typing import Any, Literal, TypedDict, cast

from netutils.interface import sort_interface_list

from utils.data_cleaning import clean_data

from ..common import CablingOptions, CommonGenerator
from ..endpoint import EndpointUplinkMixin
from ..helpers.cabling import pick_matched_switch_port_name
from ..helpers.interface_naming import get_lag_name
from ..protocols import DcimCable, DcimLAGInterface, DcimPhysicalDevice, DcimPhysicalInterface, LocationRack
from ..types import ConnectionFingerprint


class EndpointInterfaceData(TypedDict, total=False):
    """One interface projection (``PhysicalInterfaceFields`` in endpoint.gql)."""

    id: str
    name: str
    interface_type: str | None
    role: str | None
    status: str | None
    cable: dict[str, Any] | None


class EndpointPodData(TypedDict, total=False):
    """Pod projection (``PodFields`` in endpoint.gql)."""

    id: str
    name: str
    deployment_type: Literal["middle_rack", "tor", "mixed"]
    index: int
    parent: dict[str, Any]


class EndpointRackDeviceData(TypedDict, total=False):
    """ToR/Leaf device in the endpoint's rack (``RackDeviceFields`` in endpoint.gql)."""

    id: str
    name: str
    role: str | None
    rack: dict[str, Any]
    interfaces: list[EndpointInterfaceData]


class EndpointRackData(TypedDict, total=False):
    """Rack projection (``RackFields`` in endpoint.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str
    pod: EndpointPodData
    devices: list[EndpointRackDeviceData]


class EndpointDeviceData(TypedDict, total=False):
    """Top-level endpoint device (``DcimDevice`` in endpoint.gql)."""

    id: str
    name: str
    role: str | None
    rack: EndpointRackData | None
    interfaces: list[EndpointInterfaceData]


class EndpointConnectivityGenerator(EndpointUplinkMixin, CommonGenerator):
    """Generate connectivity for endpoint devices based on deployment patterns.

    Deployment strategies:
    - middle_rack: Connect to Leaf switches in network rack in same row
    - tor: Connect to ToR switches in same rack, fallback to same row
    - mixed: Connect to ToR devices in same rack, fallback to middle rack leafs in same row

    Features:
    - Suite-level device distribution
    - Flexible speed handling (speed-aware or validation-only modes)
    - Connection fingerprinting for idempotency
    - Pre-execution validation
    - Interface type and role matching (customer ↔ access)
    - Dual-homing across consecutive device pairs
    - Uses CablingPlanner and CommonGenerator.create_cabling()

    Speed Configuration (matching CablingPlanner pattern):
    - speed_aware=True (default): Group by speed first, only connect matching speeds
    - speed_aware=False: Connect all interfaces, validate speeds afterward
    - validate_speeds=True (default): Check speed compatibility, log warnings
    - strict_speed_validation=False (default): Log warnings only (True=skip mismatches)
    """

    data: EndpointDeviceData

    @staticmethod
    def _extract_device_name(intf: Any) -> str | None:
        """Extract the device name from a RelatedNode interface object."""
        if hasattr(intf, "_device_name_for_grouping"):
            return str(intf._device_name_for_grouping)
        if hasattr(intf.device, "peer"):
            device_obj = intf.device.peer
            if device_obj and hasattr(device_obj, "name"):
                return str(device_obj.name.value if hasattr(device_obj.name, "value") else device_obj.name)
        if hasattr(intf.device, "name"):
            return str(intf.device.name.value if hasattr(intf.device.name, "value") else intf.device.name)
        return None

    def _extract_cabled_switch_names(self, interfaces: list[DcimPhysicalInterface]) -> set[str]:
        """Extract the far-end switch device names from this endpoint's already-cabled
        interfaces, using the cable name convention ("device-interface__device-interface",
        set by create_cabling) — same pattern as CablingPlanner._extract_connected_peer_devices.
        """
        switch_names: set[str] = set()
        for intf in interfaces:
            cable = getattr(intf, "cable", None)
            if cable is None:
                continue
            cable_peer = getattr(cable, "_peer", None) or cable
            raw_name = getattr(cable_peer, "name", None)
            cable_name = getattr(raw_name, "value", None) or raw_name
            if not isinstance(cable_name, str) or "__" not in cable_name:
                continue
            for endpoint_label in cable_name.split("__"):
                if "-" not in endpoint_label:
                    continue
                device_name, _ = endpoint_label.rsplit("-", 1)
                if device_name != self.data["name"]:
                    switch_names.add(device_name)
        return switch_names

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.planned_connections: set[ConnectionFingerprint] = set()
        self._free_interfaces: list[DcimPhysicalInterface] = []  # Free interfaces without cables
        self._already_connected: bool = False  # True when endpoint already has existing cables
        # Switch device names this endpoint is already cabled to (from a prior
        # run) — biases device-pair selection so additional free ports land on
        # the SAME pair rather than a re-derived (possibly different) one.
        self._existing_switch_names: set[str] = set()

        # Speed validation configuration (matching CablingPlanner pattern)
        self.speed_aware: bool = kwargs.get("speed_aware", True)  # Group by speed first (default: True)
        self.validate_speeds: bool = kwargs.get("validate_speeds", True)  # Check speed compatibility
        self.strict_speed_validation: bool = kwargs.get(
            "strict_speed_validation", False
        )  # Skip mismatches (False=warnings only)

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate endpoint device connectivity based on deployment type."""
        try:
            deployment_list = clean_data(data).get("DcimDevice", [])
            if not deployment_list:
                self.logger.error("No Endpoint Device data found in GraphQL response")
                return

            # Filter out empty interface nodes (interfaces not matching the
            # PhysicalInterfaceFields fragment — e.g. a server's own
            # DcimLAGInterface bond alongside its DcimPhysicalInterface members)
            deployment_data = deployment_list[0]
            if "interfaces" in deployment_data:
                deployment_data["interfaces"] = [intf for intf in deployment_data["interfaces"] if intf]
            if "rack" in deployment_data and "devices" in (deployment_data.get("rack") or {}):
                for device in deployment_data["rack"]["devices"]:
                    if "interfaces" in device:
                        # Remove empty dicts ({}) which are virtual interfaces not matching the fragment
                        device["interfaces"] = [intf for intf in device["interfaces"] if intf]

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
        pod = rack["pod"]
        # deployment_type is derived from pod.design's layout (EndpointPod.deployment_type)
        deployment_type = pod["deployment_type"]
        self.pod_name = pod["name"].lower()
        pod_id = pod["id"]
        dc = pod["parent"]
        self.deployment_id = dc["id"]
        self.fabric_name = dc["name"].lower()

        self.logger.info(f"Generating connectivity for endpoint {self.data['name']} in {deployment_type} deployment")

        # Update endpoint device to set deployment to pod. Always saved (even when
        # deployment is already correct) so this run's tracking group always
        # includes the device — a conditional save here would leave the device
        # untracked on a no-op re-run, and delete_unused_nodes would then delete
        # the still-valid device as "unused" (same failure mode create_devices()
        # in common.py works around by always re-upserting every run).
        endpoint_device = await self.client.get(kind=DcimPhysicalDevice, id=self.data["id"])
        current_deployment = endpoint_device.deployment.id
        if current_deployment != pod_id:
            endpoint_device.deployment = pod_id
            self.logger.info(f"Updated {self.data['name']} deployment to pod {self.pod_name}")
        await endpoint_device.save(allow_upsert=True)

        # LAG-based endpoints (server declares role=lag physical NICs bundled into
        # DcimLAGInterface bond(s) in its own object-load data) get switch-side
        # port-channels wired into the target pair's ManagedMLAG domain instead of
        # plain 1:1 uplink cabling — see _process_lag_endpoint_connections.
        server_bonds: list[DcimLAGInterface] = await self.client.filters(
            kind=DcimLAGInterface,
            device__ids=[self.data["id"]],
            role__value="lag",
            include=["member_interfaces"],
        )
        if server_bonds:
            # Remember which switches earlier bonds are already cabled to (see
            # _extract_cabled_switch_names) so additional bonds land on the SAME
            # pair rather than a freshly re-derived one.
            already_cabled_members = [
                peer.peer
                for bond in server_bonds
                for peer in bond.member_interfaces.peers
                if peer.peer and getattr(peer.peer, "cable", None) and peer.peer.cable.id
            ]
            self._existing_switch_names = self._extract_cabled_switch_names(already_cabled_members)

            lock_id = await self.acquire_resource_lock(f"endpoint-cabling-pod-{pod['id']}-row-{rack['row_index']}")
            try:
                await self._process_lag_endpoint_connections(server_bonds, deployment_type)
            finally:
                await self.release_resource_lock(lock_id)
            return

        # Get all uplink interfaces from endpoint device (idempotency)
        # Note: Endpoint devices use "uplink" role, ToR/Leaf devices use "customer" role
        all_endpoint_interfaces: list[DcimPhysicalInterface] = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[self.data["id"]],
            role__value="uplink",
            status__values=["free", "planned", "active"],
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

        # Rack-to-spine cabling avoids port collisions via a deterministic per-device
        # offset (calculate_cabling_offsets); endpoint cabling has no such offset — it
        # picks the first free port it sees. Concurrent siblings (the backend fans
        # multiple endpoint generator instances out via asyncio.gather — see
        # acquire_resource_lock's docstring) targeting the same rack/row could both
        # pick the same "free" port before either saves its cable. Lock on
        # (pod, row) rather than just the rack: the tor/mixed fallback path also
        # reaches into every rack in the same row, not just this endpoint's own.
        lock_id = await self.acquire_resource_lock(f"endpoint-cabling-pod-{pod['id']}-row-{rack['row_index']}")
        try:
            all_target_interfaces = await self._resolve_target_interfaces(deployment_type)
            if all_target_interfaces:
                await self._process_endpoint_connections(all_target_interfaces)
        finally:
            await self.release_resource_lock(lock_id)

    async def _resolve_target_interfaces(self, deployment_type: str) -> list[DcimPhysicalInterface]:
        """Resolve the target switch interfaces for this endpoint's deployment type.

        Shared by both the plain uplink flow (_process_endpoint_connections)
        and the LAG flow (_process_lag_endpoint_connections) — deployment-type
        routing (which racks/roles to search) is identical for both; only
        what's done with the resulting interfaces differs.
        """
        if deployment_type == "middle_rack":
            return await self._connect_middle_rack_deployment()
        if deployment_type == "tor":
            return await self._connect_tor_deployment()
        if deployment_type == "mixed":
            return await self._connect_mixed_deployment()
        self.logger.error(f"Unknown deployment type '{deployment_type}' for endpoint {self.data['name']}")
        return []

    async def _connect_middle_rack_deployment(self) -> list[DcimPhysicalInterface]:
        """Resolve target interfaces for middle_rack deployment.

        Strategy: Server in compute rack connects to switches in the middle rack (network rack) in same row.
        Middle_rack topology has one network rack per row containing ToR, L2-leaf/access-leaf (dedicated
        L2 aggregation, when present), or Leaf switches that serve compute racks. Prefers ToR, then the
        L2 aggregation layer (l2-leaf/access-leaf) — the layer servers are meant to attach to when one
        exists — falling back to Leaf only when neither is present.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.get("rack") is not None, "Rack must be assigned"
        rack = self.data["rack"]
        assert rack is not None
        pod_id = rack["pod"]["id"]

        self.logger.info(
            f"Endpoint {self.data['name']} is in {rack['rack_type']} rack "
            f"(row {rack['row_index']}), searching for ToR/L2-leaf/access-leaf/Leaf switches "
            "in middle rack (network rack) in same row"
        )

        # Query interfaces directly on ToR, l2-leaf/access-leaf, or Leaf devices in network rack
        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[pod_id],
            row_index__value=rack["row_index"],
            rack_type__value="network",
        )

        if not racks:
            self.logger.error(
                f"Endpoint {self.data['name']}: No network rack found in row {rack['row_index']} for middle_rack deployment."
            )
            return []

        # Try ToR devices first in network rack (preferred for aggregation)
        rack_ids = [r.id for r in racks]
        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self._free_interfaces,
        )

        # Fallback to the L2 aggregation layer (l2-leaf/access-leaf) — servers
        # attach here when a rack has one, ahead of the routed Leaf layer.
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces found in network rack for {self.data['name']}, trying l2-leaf/access-leaf switches"
            )
            all_target_interfaces = await self._query_l2_aggregation_layer(rack_ids)

        # Fallback to Leaf devices in network rack if nothing else found
        if not all_target_interfaces:
            self.logger.info(f"No l2-leaf/access-leaf interfaces found for {self.data['name']}, trying Leaf switches")
            all_target_interfaces = await self._query_interfaces_by_location(
                rack_ids=rack_ids,
                device_role="leaf",
                endpoint_interfaces=self._free_interfaces,
            )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No free interfaces found on ToR, l2-leaf/access-leaf, or Leaf "
                "devices in middle rack. Cannot create endpoint connectivity."
            )

        return all_target_interfaces

    async def _query_l2_aggregation_layer(self, rack_ids: list[str]) -> list[DcimPhysicalInterface]:
        """Try l2-leaf, then access-leaf, in the given racks — the dedicated L2
        aggregation layer servers attach to when a rack provisions one.
        """
        for device_role in ("l2-leaf", "access-leaf"):
            interfaces = await self._query_interfaces_by_location(
                rack_ids=rack_ids,
                device_role=device_role,
                endpoint_interfaces=self._free_interfaces,
            )
            if interfaces:
                return interfaces
        return []

    async def _connect_tor_deployment(self) -> list[DcimPhysicalInterface]:
        """Resolve target interfaces for tor deployment.

        Strategy: Connect to ToR switches in same rack, fallback to same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.get("rack") is not None, "Rack must be assigned"
        rack = self.data["rack"]
        assert rack is not None

        # First try to query interfaces in same rack
        rack_ids = [rack["id"]]

        # Query free interfaces on ToR devices in same rack
        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self._free_interfaces,
        )

        # Fallback to same row if no interfaces found in rack
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces in same rack for {self.data['name']}, searching same row {rack['row_index']}"
            )

            racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[rack["pod"]["id"]],
                row_index__value=rack["row_index"],
            )

            if racks:
                all_target_interfaces = await self._query_interfaces_by_location(
                    rack_ids=[r.id for r in racks],
                    device_role="tor",
                    endpoint_interfaces=self._free_interfaces,
                )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No ToR interfaces found in tor deployment. "
                "Cannot create endpoint connectivity."
            )

        return all_target_interfaces

    async def _connect_mixed_deployment(self) -> list[DcimPhysicalInterface]:
        """Resolve target interfaces for mixed deployment.

        Strategy: Connect to ToR devices in same rack first, then the L2 aggregation layer
        (l2-leaf/access-leaf) in the same rack, falling back to middle rack l2-leaf/access-leaf/leaf
        in the same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.get("rack") is not None, "Rack must be assigned"
        rack = self.data["rack"]
        assert rack is not None

        # First try ToR interfaces in same rack
        rack_ids = [rack["id"]]

        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self._free_interfaces,
        )

        # Then the L2 aggregation layer in the same rack, if this rack has one
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces in same rack for {self.data['name']}, trying l2-leaf/access-leaf in same rack"
            )
            all_target_interfaces = await self._query_l2_aggregation_layer(rack_ids)

        # If nothing local, try the middle rack (network rack) in same row:
        # l2-leaf/access-leaf first, then Leaf as last resort.
        if not all_target_interfaces:
            self.logger.info(
                f"No local ToR/l2-leaf/access-leaf for {self.data['name']}, "
                f"trying middle rack switches in same row {rack['row_index']}"
            )

            racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[rack["pod"]["id"]],
                row_index__value=rack["row_index"],
                rack_type__value="network",
            )

            if racks:
                network_rack_ids = [r.id for r in racks]
                all_target_interfaces = await self._query_l2_aggregation_layer(network_rack_ids)
                if not all_target_interfaces:
                    all_target_interfaces = await self._query_interfaces_by_location(
                        rack_ids=network_rack_ids,
                        device_role="leaf",
                        endpoint_interfaces=self._free_interfaces,
                    )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No ToR, l2-leaf/access-leaf, or Leaf interfaces found "
                "in mixed deployment. Cannot create endpoint connectivity."
            )

        return all_target_interfaces

    async def _query_interfaces_by_location(
        self,
        rack_ids: list[str],
        device_role: Literal["tor", "leaf", "l2-leaf", "access-leaf"],
        endpoint_interfaces: list[Any],
    ) -> list[DcimPhysicalInterface]:
        """Query free interfaces on devices in specific racks.

        Args:
            rack_ids: List of rack IDs to search
            device_role: Device role to filter (tor, leaf, l2-leaf, or access-leaf)
            endpoint_interfaces: Endpoint interface models for type matching

        Returns:
            List of free interfaces on target devices
        """
        # First, query devices in the specified racks
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            rack__ids=rack_ids,
            role__value=device_role,
            status__values=["active", "free", "provisioning"],
        )

        if not devices:
            self.logger.info(f"No {device_role} devices found in {len(rack_ids)} rack(s)")
            return []

        # Extract interface types - handle both string and object attributes
        endpoint_types = []
        for intf in endpoint_interfaces:
            if intf.interface_type:
                intf_type = (
                    intf.interface_type.value if hasattr(intf.interface_type, "value") else str(intf.interface_type)
                )
                if intf_type:
                    endpoint_types.append(intf_type)

        # Query interfaces on those devices
        # ToR/Leaf devices have "customer" interfaces that connect to server's "uplink" interfaces
        acceptable_roles = ["downlink", "customer"]
        device_ids = [dev.id for dev in devices]
        # On the first connection attempt only look at genuinely unoccupied ports.
        # When the endpoint already has cables (re-run) be permissive so that
        # partially-connected endpoints can still have remaining ports wired up.
        status_filter: dict[str, Any] = (
            {"status__values": ["free", "planned", "active"]} if self._already_connected else {"status__value": "free"}
        )
        intf_filters: dict[str, Any] = {
            "kind": DcimPhysicalInterface,
            "device__ids": device_ids,
            **status_filter,
            "role__values": acceptable_roles,
            "include": ["device", "interface_type", "cable"],
        }
        if endpoint_types:
            intf_filters["interface_type__values"] = endpoint_types
        all_interfaces = await self.client.filters(**intf_filters)

        # Debug logging
        self.logger.debug(f"Query returned {len(all_interfaces)} interfaces before cable filter")
        self.logger.debug(f"Device IDs: {device_ids}")
        self.logger.debug(f"Endpoint types: {endpoint_types}")
        self.logger.debug(f"Acceptable roles: {acceptable_roles}")

        # Filter out interfaces that already have cables
        free_interfaces = [intf for intf in all_interfaces if not (intf.cable and intf.cable.id)]

        # Debug: check what's being filtered out
        filtered_count = len(all_interfaces) - len(free_interfaces)
        if filtered_count > 0:
            self.logger.debug(f"Filtered out {filtered_count} interfaces with cables")
            # Show sample of filtered interfaces
            for intf in all_interfaces[:3]:
                has_cable = (
                    f"cable={intf.cable.id if (intf.cable and hasattr(intf.cable, 'id') and intf.cable.id) else 'None'}"
                )
                self.logger.debug(
                    f"  Interface {intf.name}: {has_cable}, status={intf.status.value if hasattr(intf.status, 'value') else intf.status}"
                )

        self.logger.info(
            f"Found {len(free_interfaces)} free interfaces on {len(devices)} {device_role} device(s) in {len(rack_ids)} rack(s) "
            f"(interface_types={endpoint_types or 'any'}, roles={acceptable_roles})"
        )

        return free_interfaces

    async def _process_lag_endpoint_connections(
        self, server_bonds: list[DcimLAGInterface], deployment_type: str
    ) -> None:
        """Wire a LAG-based endpoint's bond(s) to a switch pair's ManagedMLAG domain.

        Each server bond fans out to one port-channel on EACH target switch —
        every switch owns its own port-channel (member = the physical port this
        bond's cable lands on), and both port-channels reference the same
        mlag_domain so they act as one logical vPC/MLAG-attached-host link.
        Mirrors mlag.py's peer-link pattern one level down (server<->switch
        instead of switch<->switch).

        Requires the target pair to already share exactly one ManagedMLAG
        domain — per user direction, a pair with no shared domain is skipped
        with a warning rather than auto-provisioned here (unlike
        rack.py's _ensure_mlag_pairs, which creates the domain for switch
        pairs it itself creates).
        """
        all_target_interfaces = await self._resolve_target_interfaces(deployment_type)
        if not all_target_interfaces:
            return

        device_groups: dict[str, list[DcimPhysicalInterface]] = {}
        for intf in all_target_interfaces:
            device_name = self._extract_device_name(intf)
            if device_name:
                device_groups.setdefault(device_name, []).append(intf)

        device_names = list(device_groups.keys())
        if len(device_names) < 2:
            self.logger.error(
                f"Endpoint {self.data['name']}: Need 2 switches for MLAG-attached bonds, found {len(device_names)}."
            )
            return

        # Prefer the pair this endpoint's earlier bonds already attached to
        # (see _extract_cabled_switch_names) over an arbitrary "first two" —
        # a later bond must land on the SAME pair as earlier ones.
        sticky_devices = sorted(name for name in device_names if name in self._existing_switch_names)
        remaining_devices = [name for name in device_names if name not in self._existing_switch_names]
        switch_a_name, switch_b_name = (sticky_devices + remaining_devices)[:2]

        switches = await self.client.filters(
            kind=DcimPhysicalDevice, name__values=[switch_a_name, switch_b_name], include=["capabilities", "platform"]
        )
        switch_by_name = {s.name.value: s for s in switches}
        mlag_ids_by_switch: dict[str, set[str]] = {}
        for name, switch in switch_by_name.items():
            caps = switch.capabilities
            mlag_ids_by_switch[name] = {peer.id for peer in caps.peers if peer.typename == "ManagedMLAG"}

        shared_domains = mlag_ids_by_switch.get(switch_a_name, set()) & mlag_ids_by_switch.get(switch_b_name, set())
        if len(shared_domains) != 1:
            self.logger.error(
                f"Endpoint {self.data['name']}: {switch_a_name}/{switch_b_name} share "
                f"{len(shared_domains)} MLAG domain(s) (need exactly 1) — cannot wire LAG bond(s). "
                "Pair the switches into a ManagedMLAG domain first."
            )
            return
        mlag_domain_id = next(iter(shared_domains))

        existing_lag_ids: set[int] = set()
        for name in (switch_a_name, switch_b_name):
            lags = await self.client.filters(kind=DcimLAGInterface, device__ids=[switch_by_name[name].id])
            existing_lag_ids.update(lag.lag_id.value for lag in lags)

        for bond in server_bonds:
            member_ifaces = bond.member_interfaces
            member_peers = [peer.peer for peer in member_ifaces.peers]
            bond_name = bond.name.value

            if len(member_peers) < 2:
                self.logger.error(f"Bond {bond_name} on {self.data['name']} has < 2 member interfaces — cannot wire it")
                continue

            member_names = sort_interface_list([m.name.value for m in member_peers])
            member_by_name = {m.name.value: m for m in member_peers}

            # Resolve each switch's port for this bond: reuse the existing one if
            # a prior run already cabled this member (read off the far end of its
            # cable), else claim a free port. Reusing — rather than
            # skipping outright — keeps the cable/port-channel/switch port
            # re-touched (create_cabling + the LAG save below are both
            # allow_upsert=True) so this run's tracking group re-includes them;
            # otherwise delete_unused_nodes would delete still-valid prior-run
            # objects that nothing in THIS run touched again.
            free_ports_by_switch: dict[str, list[DcimPhysicalInterface]] = {}
            for name in (switch_a_name, switch_b_name):
                free_ports = [p for p in device_groups[name] if not (p.cable and p.cable.id)]
                free_port_by_name = {p.name.value: p for p in free_ports}
                sorted_names = sort_interface_list(list(free_port_by_name.keys()))
                free_ports_by_switch[name] = [free_port_by_name[n] for n in sorted_names]

            switch_port_by_name: dict[str, DcimPhysicalInterface] = {}
            members_needing_fresh_port: dict[str, str] = {}
            for switch_name, server_interface_name in zip((switch_a_name, switch_b_name), member_names):
                member = member_by_name[server_interface_name]
                existing_cable = getattr(member, "cable", None)
                if existing_cable and existing_cable.id:
                    cable_obj = await self.client.get(kind=DcimCable, id=existing_cable.id, include=["endpoints"])
                    far_ends = [p for p in cable_obj.endpoints.peers if p.id != member.id]
                    if far_ends:
                        switch_port_by_name[switch_name] = await self.client.get(
                            kind=DcimPhysicalInterface, id=far_ends[0].id, include=["lag"]
                        )
                        continue
                members_needing_fresh_port[switch_name] = server_interface_name

            # When BOTH members need a fresh port, prefer the SAME port name on
            # both switches (e.g. Ethernet1/1/8 on both) over independently
            # picking each switch's own first-free port — falls back to
            # independent picks when no common free name exists.
            matched_name = None
            if switch_a_name in members_needing_fresh_port and switch_b_name in members_needing_fresh_port:
                free_names_by_switch = {
                    name: [p.name.value for p in ports] for name, ports in free_ports_by_switch.items()
                }
                matched_name = pick_matched_switch_port_name(free_names_by_switch, (switch_a_name, switch_b_name))

            for switch_name, server_interface_name in members_needing_fresh_port.items():
                if not free_ports_by_switch[switch_name]:
                    self.logger.error(f"Bond {bond_name} on {self.data['name']}: no free port on {switch_name}")
                    continue
                if matched_name is not None:
                    switch_port = next(p for p in free_ports_by_switch[switch_name] if p.name.value == matched_name)
                else:
                    switch_port = free_ports_by_switch[switch_name][0]
                free_ports_by_switch[switch_name].remove(switch_port)
                device_groups[switch_name].remove(switch_port)
                switch_port_by_name[switch_name] = switch_port

            if switch_a_name not in switch_port_by_name or switch_b_name not in switch_port_by_name:
                continue

            existing_lag_objs = [getattr(switch_port_by_name[n], "lag", None) for n in (switch_a_name, switch_b_name)]
            lag_id = next(
                (lag.peer.lag_id.value for lag in existing_lag_objs if lag and lag.id and lag.peer),
                None,
            )
            if lag_id is None:
                lag_id = self._next_free_lag_id(existing_lag_ids)
                existing_lag_ids.add(lag_id)

            for switch_name, server_interface_name in zip((switch_a_name, switch_b_name), member_names):
                switch = switch_by_name[switch_name]
                switch_port = switch_port_by_name[switch_name]

                fingerprint = ConnectionFingerprint(
                    server_name=self.data["name"],
                    server_interface=server_interface_name,
                    switch_name=switch_name,
                    switch_interface=switch_port.name.value,
                )
                if fingerprint not in self.planned_connections:
                    self.planned_connections.add(fingerprint)
                    await self.create_cabling(
                        bottom_devices=[self.data["name"]],
                        bottom_interfaces=[server_interface_name],
                        top_devices=[switch_name],
                        top_interfaces=[switch_port.name.value],
                        strategy="intra_rack",
                        options=CablingOptions(cabling_offset=0, pool=None),
                    )

                platform = switch.platform
                platform_name = platform.peer.name.value if platform.peer else ""
                lag_obj = await self.client.create(
                    kind=DcimLAGInterface,
                    data={
                        "name": get_lag_name(platform_name, lag_id),
                        "description": f"{self.data['name']}:{bond_name}",
                        "device": {"id": switch.id},
                        "status": "active",
                        "role": "lag",
                        "lag_id": lag_id,
                        "lacp_mode": "active",
                        "mlag_domain": {"id": mlag_domain_id},
                        "member_interfaces": [{"id": switch_port.id}],
                    },
                )
                await lag_obj.save(allow_upsert=True)
                self.logger.info(
                    f"{self.data['name']}: bond {bond_name} → {switch_name}:{get_lag_name(platform_name, lag_id)} "
                    f"(member {switch_port.name.value}, mlag_domain={mlag_domain_id})"
                )

    @staticmethod
    def _next_free_lag_id(existing_ids: set[int]) -> int:
        """Smallest positive lag_id not already in use (100 reserved for MLAG peer-link, see mlag.py)."""
        candidate = 1
        taken = existing_ids | {100}
        while candidate in taken:
            candidate += 1
        return candidate
