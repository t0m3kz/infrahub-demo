from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, cast

from utils.data_cleaning import clean_data

from ..common import CablingOptions, CommonGenerator, DeviceOptions, RoutingOptions
from ..helpers import DeviceNamingConfig
from ..models import RackModel
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, LocationRack


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def fetch_rack_devices_with_interfaces(
        self,
        rack: LocationRack | None = None,
        role_filter: str | None = None,
        interface_role: str = "downlink",
    ) -> list[dict]:
        """Fetch devices and their interfaces from a rack using the registered GQL query.

        Args:
            rack: Optional SDK rack object. If None, uses self.data context (pod + row)
            role_filter: Optional device role to filter by (e.g., "leaf", "spine")
            interface_role: Interface role filter (default: "downlink")

        Returns:
            List of dicts with device name, interfaces list, and role
        """
        if rack:
            rack_obj = await self.client.get(kind=LocationRack, id=rack.id)
            pod_id = rack_obj.pod.id
            row_index = rack_obj.row_index.value
        else:
            pod_id = self.data.pod.id
            row_index = self.data.row_index

        _gql_path = Path(__file__).parent.parent.parent / "queries/topology/add/rack_devices_with_interfaces.gql"
        result = await self.client.execute_graphql(
            query=_gql_path.read_text(),
            variables={
                "pod_id": pod_id,
                "row_index": row_index,
                "role_filter": role_filter,
                "interface_role": interface_role,
            },
        )

        devices_with_interfaces = []
        for rack_edge in result.get("LocationRack", {}).get("edges", []):
            rack_node = rack_edge.get("node", {})
            for device_edge in rack_node.get("devices", {}).get("edges", []):
                device_node = device_edge.get("node", {})
                interfaces = [
                    iface_edge.get("node", {}).get("name", {}).get("value")
                    for iface_edge in device_node.get("interfaces", {}).get("edges", [])
                ]
                devices_with_interfaces.append(
                    {
                        "device_id": device_node.get("id"),
                        "device_name": device_node.get("name", {}).get("value"),
                        "role": device_node.get("role", {}).get("value"),
                        "interfaces": interfaces,
                        "interface_count": len(interfaces),
                    }
                )

        return devices_with_interfaces

    async def update_checksum(self) -> None:
        """Update checksum for ToR racks in same row (mixed mode only).

        Verifies middle rack leafs exist before updating ToR checksums.
        Queries network rack checksum to handle cases where ToR racks are added later.
        """
        deployment_type = self.data.pod.deployment_type

        # Only update ToR racks in mixed deployment mode, and only from middle/network racks
        if deployment_type != "mixed" or self.data.rack_type != "network":
            return

        # Skip racks that have no leaf template — e.g. border-leaf-only racks.
        # These are valid network racks but have no leafs to cascade from.
        if not self.data.leafs:
            self.logger.warning(f"Rack {self.data.name} has no leaf template — skipping ToR checksum cascade")
            return

        # Verify leafs were created in this rack before cascading to ToR racks
        leaf_data = await self.fetch_rack_devices_with_interfaces(role_filter="leaf")

        if not leaf_data:
            # Graceful degradation: ToR cascade is optional enhancement in mixed deployment
            self.logger.warning(f"Middle rack {self.data.name} has no leafs - skipping ToR cascade (non-critical)")
            return

        # Single query for all racks in same pod/row, then split by type
        row_racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.pod.id],
            row_index__value=self.data.row_index,
        )
        network_racks = [r for r in row_racks if r.rack_type.value == "network"]
        tor_racks = [r for r in row_racks if r.rack_type.value == "tor"]

        if not network_racks:
            self.logger.warning(
                f"No network rack found in row {self.data.row_index} - skipping ToR cascade (non-critical)"
            )
            return

        network_checksum = network_racks[0].checksum.value if network_racks[0].checksum else self.data.checksum

        for i, rack in enumerate(tor_racks):
            # Stagger ToR rack triggers so the middle rack generator finishes creating leafs
            # before each ToR rack generator fires. ToR racks depend on leaf devices existing.
            if i > 0:
                await asyncio.sleep(5)
            rack.checksum.value = network_checksum
            await rack.save(allow_upsert=True)
            self.logger.info(
                f"Rack {rack.name.value} (type={rack.rack_type.value}) has been updated to checksum {network_checksum}"
            )

    def _derive_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive spine device names and interface names from query data.

        The rack.gql query already fetches pod.amount_of_spines,
        pod.spine_template.interfaces(role="downlink"), and all naming
        indexes (dc.index, pod.index).  The pod generator always creates
        spines with strategy="standard" and indexes=[dc.index, pod.index],
        so spine names are deterministic — no API call needed.

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

        # Pod generator uses naming convention from DC with indexes=[dc.index, pod.index]
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

        # Interface names from spine template — query pre-filters to role=downlink
        interface_names = sorted(iface.name for iface in spine_template.interfaces)

        if not interface_names:
            raise RuntimeError(f"Rack {self.data.name}: Spine template has no downlink interfaces")

        self.logger.info(
            f"Derived {len(device_names)} spine device names with "
            f"{len(interface_names)} downlink interface(s) from query data"
        )
        return device_names, interface_names

    async def _get_leaf_devices_in_row(self, pod_id: str, row_index: int) -> tuple[list[str], list[str]]:
        """Query leaf devices in same row and their interfaces for ToR-to-leaf cabling.

        Args:
            pod_id: Pod ID to filter devices
            row_index: Row index to filter leaf devices by same row

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        # Step 1: Query racks in same row
        racks_in_row = await self.client.filters(
            kind=LocationRack,
            pod__ids=[pod_id],
            row_index__value=row_index,
        )

        if not racks_in_row:
            self.logger.error(
                f"Rack {self.data.name}: No racks found in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )
            raise RuntimeError(f"Rack {self.data.name}: Cannot cable ToRs - no racks in row {row_index}")

        # Step 2: Query leaf devices and their downlink interfaces
        leaf_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            role__value="leaf",
            rack__ids=[rack.id for rack in racks_in_row],
        )

        if not leaf_devices:
            self.logger.error(
                f"Rack {self.data.name}: No leaf devices found in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )
            raise RuntimeError(f"Rack {self.data.name}: Cannot cable ToRs - no leaf devices in row {row_index}")

        device_names = [dev.name.value for dev in leaf_devices]
        leaf_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=device_names,
            role__value="downlink",
        )

        if not leaf_interfaces:
            self.logger.error(
                f"Rack {self.data.name}: No downlink interfaces found on leaf devices in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )
            raise RuntimeError(
                f"Rack {self.data.name}: Cannot cable ToRs - no downlink interfaces on leafs in row {row_index}"
            )

        # Extract unique interface names
        interface_names = sorted(set(iface.name.value for iface in leaf_interfaces))

        self.logger.info(
            f"Found {len(device_names)} leaf devices in row {row_index} with {len(interface_names)} unique downlink interfaces"
        )
        return device_names, interface_names

    def calculate_cabling_offsets(
        self,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Calculate cabling offset using simple formula based on rack position."""

        current_index = self.data.index

        # Get deployment_type from pod and max_tors from design
        pod = self.data.pod

        # deployment_type is on pod, max_tors_per_row can be calculated from design
        deployment_type = pod.deployment_type
        if pod.design is None:
            self.logger.warning(
                f"Rack {self.data.name}: pod '{pod.name}' has no design set — "
                "falling back to max_tors_per_row=8 for offset calculation. "
                "Run pod generator first for accurate cabling."
            )
            max_tors_per_row = 8
        else:
            max_tors_per_row = pod.design.compute_racks_per_row * pod.design.max_tors_per_compute_rack

        # For middle_rack deployment ToRs: always offset=0 (ToRs connect to leafs in same rack)
        if deployment_type == "middle_rack" and device_type == "tor":
            offset = 0
            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(mode=middle_rack) - intra-rack cabling"
            )

        # For mixed deployment ToRs: static offset based on rack index
        # Formula: (rack_index - 1) × tors_per_rack
        elif deployment_type == "mixed" and device_type == "tor":
            offset = (current_index - 1) * device_count
            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(index={current_index}, tors_per_rack={device_count}, mode=mixed)"
            )

        # For mixed/middle_rack deployment leafs: calculate offset based on row position
        # Middle rack leafs serve all ToRs in their row
        elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            offset = (self.data.row_index - 1) * device_count

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row_index={self.data.row_index}, leafs_per_rack={device_count}, mode={deployment_type})"
            )

        # For tor deployment ToRs: calculate cumulative offset across pod
        # ToRs connect to spines, need cumulative offset across all rows
        # Uses actual racks in previous rows (passed in) to avoid exceeding spine port capacity
        elif deployment_type == "tor" and device_type == "tor":
            if racks_in_previous_rows is not None:
                tors_in_previous_rows = racks_in_previous_rows * device_count
            else:
                # Fallback to design max if actual count not provided
                max_tors_int = int(max_tors_per_row)
                tors_in_previous_rows = max_tors_int * (self.data.row_index - 1)

            # Offset from previous racks in current row
            offset_in_current_row = device_count * (current_index - 1)

            offset = tors_in_previous_rows + offset_in_current_row

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row={self.data.row_index}, index={current_index}, tors_in_rack={device_count}, "
                f"tors_in_previous_rows={tors_in_previous_rows}, mode={deployment_type})"
            )

        else:
            # Other cases: no offset needed
            offset = 0
            self.logger.info(f"No offset needed for {device_type} in rack {self.data.name} (mode={deployment_type})")

        return offset

    @staticmethod
    def _parse_rack_data(data: dict) -> RackModel:
        """Normalize trigger/query data into a RackModel.

        Raises:
            ValueError: when data has an unknown shape or contains no rack edges.
        """
        if "name" in data and isinstance(data.get("name"), dict):
            return RackModel(**data)
        if "LocationRack" in data:
            raw = data["LocationRack"]
            if isinstance(raw, dict) and "edges" in raw and not raw["edges"]:
                raise ValueError(
                    "GraphQL query returned no edges for LocationRack — "
                    "rack may not exist or query parameters may be incorrect."
                )
            deployment_list = clean_data(data).get("LocationRack", [])
            if not deployment_list:
                raise ValueError("No rack found after clean_data — rack exists but has an invalid data structure.")
            return RackModel(**deployment_list[0])
        raise ValueError(f"Unknown data structure. Keys: {list(data.keys())}")

    async def _cable_and_route(
        self,
        *,
        bottom_devices: list[str],
        bottom_interfaces: list[str],
        top_devices: list[str],
        top_interfaces: list[str],
        strategy: Literal["pod", "rack", "intra_rack", "intra_rack_middle", "intra_rack_mixed"],
        offset: int,
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
    ) -> None:
        """Create cabling then routing between two device layers.

        Reads shared context stored on self by generate():
        ``_technical_pool_id``, ``_p2p_prefix_length``, ``_routing_options``, ``_dc_design``.
        """
        await self.create_cabling(
            bottom_devices=bottom_devices,
            bottom_interfaces=bottom_interfaces,
            top_devices=top_devices,
            top_interfaces=top_interfaces,
            strategy=strategy,
            options=CablingOptions(
                cabling_offset=offset,
                pool=self._technical_pool_id,
                p2p_prefix_length=self._p2p_prefix_length,
            ),
            bottom_sorting=bottom_sorting,
            top_sorting=top_sorting,
        )
        if self._dc_design:
            await self.create_routing(
                bottom_devices=bottom_devices,
                top_devices=top_devices,
                options=self._routing_options,
            )

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        if not data:
            self.logger.error("Generator received empty data")
            return
        try:
            self.data = self._parse_rack_data(data)
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        shape = "direct node data" if "name" in data and isinstance(data.get("name"), dict) else "query result"
        self.logger.info(f"Processing {shape}")

        # Get deployment_type from design or fall back to legacy pod attribute
        pod = self.data.pod

        # deployment_type is on the pod, not the design
        # PodDesign contains physical layout, pod contains deployment decisions
        deployment_type = pod.deployment_type

        self.logger.info(
            f"Starting rack generation: {self.data.name} [type={self.data.rack_type}, deployment={deployment_type}]"
        )

        # Validate checksum is set (required for proper generation ordering in mixed deployments)
        if not self.data.checksum:
            # Special case: ToR racks in mixed deployments can inherit checksum from middle rack
            if deployment_type == "mixed" and self.data.rack_type == "tor":
                # Query middle rack in same row to get checksum
                middle_racks = await self.client.filters(
                    kind=LocationRack,
                    pod__ids=[self.data.pod.id],
                    row_index__value=self.data.row_index,
                    rack_type__value="network",  # Middle racks have network type
                )

                if middle_racks and middle_racks[0].checksum.value:
                    # Inherit checksum from middle rack
                    rack_obj = await self.client.get(kind=LocationRack, id=self.data.id)
                    rack_obj.checksum.value = middle_racks[0].checksum.value
                    await rack_obj.save(allow_upsert=True)
                    self.logger.info(
                        f"ToR rack {self.data.name} inherited checksum {middle_racks[0].checksum.value} "
                        f"from middle rack {middle_racks[0].name.value}. "
                        "Checksum update will trigger generator again to create devices."
                    )
                    # Return here - checksum update will trigger this generator again
                    return
                else:
                    self.logger.warning(
                        f"ToR rack {self.data.name} has no checksum and no middle rack found "
                        f"in row {self.data.row_index} - skipping generation."
                    )
                    return

            # Special case: new network racks in middle_rack deployments inherit checksum
            # from a sibling rack in the same pod (all pod racks share the same pod checksum)
            elif deployment_type == "middle_rack" and self.data.rack_type == "network":
                sibling_racks = await self.client.filters(
                    kind=LocationRack,
                    pod__ids=[self.data.pod.id],
                    rack_type__value="network",
                )
                sibling_with_checksum = [r for r in sibling_racks if r.id != self.data.id and r.checksum.value]

                if sibling_with_checksum:
                    rack_obj = await self.client.get(kind=LocationRack, id=self.data.id)
                    rack_obj.checksum.value = sibling_with_checksum[0].checksum.value
                    await rack_obj.save(allow_upsert=True)
                    self.logger.info(
                        f"Network rack {self.data.name} inherited checksum {sibling_with_checksum[0].checksum.value} "
                        f"from sibling rack {sibling_with_checksum[0].name.value}. "
                        "Checksum update will trigger generator again to create devices."
                    )
                    return
                else:
                    self.logger.warning(
                        f"Network rack {self.data.name} has no checksum and no sibling racks found in pod "
                        f"to inherit from — run pod generator first."
                    )
                    return

            else:
                self.logger.warning(
                    f"Rack {self.data.name} has no checksum set - skipping generation. "
                    "Checksum will be set by pod or middle rack generator."
                )
                return

        # In mixed deployment, ToR racks should wait for middle rack to generate leafs first
        if deployment_type == "mixed" and self.data.rack_type == "tor":
            # Query network rack(s) in same pod and row
            network_racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[self.data.pod.id],
                row_index__value=self.data.row_index,
                rack_type__value="network",
            )

            if not network_racks:
                self.logger.info(
                    f"ToR rack {self.data.name} waiting for network rack in row {self.data.row_index} - skipping this run."
                )
                return

            # Fetch leaf devices with interfaces from network rack
            leaf_data = await self.fetch_rack_devices_with_interfaces(
                rack=network_racks[0],
                role_filter="leaf",
            )

            if leaf_data:
                # Leafs exist with interfaces ready - proceed with ToR generation
                self.logger.info(
                    f"ToR rack {self.data.name} found {len(leaf_data)} leaf devices in row {self.data.row_index} "
                    "- proceeding with ToR generation"
                )
            else:
                # No leafs yet - wait for middle rack to generate them
                self.logger.info(
                    f"ToR rack {self.data.name} waiting for leafs to be generated in row {self.data.row_index} - skipping this run."
                )
                return

        self.logger.info(f"Generating topology for rack {self.data.name}")

        dc = self.data.pod.parent
        self.deployment_id = dc.id  # Store for cable linking
        pod = self.data.pod
        self.pod_name = pod.name.lower()
        self.fabric_name = dc.name.lower()
        # Use pool ID from GraphQL — _resolve_pool resolves by ID directly
        technical_pool_id = pod.prefix_pool.id if pod.prefix_pool else None

        # Validate pools exist - they should be created by pod generator
        # Failing fast prevents race conditions when multiple racks are created simultaneously
        if not self.data.pod.loopback_pool or not self.data.pod.prefix_pool:
            self.logger.error(
                f"Rack {self.data.name}: Pod {pod.name} pools not found. "
                f"Run pod generator first: infrahubctl generator generate_pod name={pod.name}"
            )
            return

        # Pass pool IDs from query data — _resolve_pool resolves by ID directly,
        # avoiding name-based fallback lookups
        management_pool_id = dc.management_pool.id if dc.management_pool else None
        loopback_pool_id = pod.loopback_pool.id if pod.loopback_pool else None

        # Indexes for leaf devices (include suite, row, rack for unique naming across pod)
        # Device name pattern: dc1-fab1-pod1-suite1-row1-rack5-leaf-01
        suite = self.data.parent  # LocationSuite
        leaf_indexes: list[int] = [
            dc.index,
            pod.index,
            suite.index,
            self.data.row_index,
            self.data.index,
        ]

        # Get deployment type once for reuse
        deployment_type = pod.deployment_type

        # Indexes for ToR devices (include suite, row, rack for unique naming across all deployment types)
        # Device name pattern: dc1-fab1-pod2-suite1-row1-rack1-tor-01
        tor_indexes: list[int] = [
            dc.index,
            pod.index,
            suite.index,
            self.data.row_index,
            self.data.index,
        ]

        if deployment_type == "tor":
            self.logger.info(
                f"ToR rack {self.data.name}: using suite={suite.index}, row={self.data.row_index}, rack_index={self.data.index}"
            )

        # Get naming convention from DC design (wired through GQL query)
        dc_design = dc.design
        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            dc.naming_convention,
        )
        # P2P prefix length: /127 for IPv6/dual-stack (default), /31 for IPv4 (exception)
        p2p_prefix_length = 127 if dc_design and getattr(dc_design, "p2p_ipv6", False) else 31
        is_ipv6 = dc_design.is_ipv6 if dc_design else False

        created_leaf_devices: list[str] = []
        created_tor_devices: list[str] = []
        # Track device names created in THIS run to avoid duplicate upserts from
        # multiple templates with the same role (which generate identical device names).
        # Duplicate upserts corrupt cardinality-one relationships in v1.8.0.
        # NOTE: Do NOT pre-populate with existing devices — the generator must always
        # re-create (upsert) all objects so they're registered with the group context.
        # Skipping existing devices leaves their related objects (IPs, cables, routing)
        # unregistered, causing group cleanup to attempt deletion.
        _created_device_names: set[str] = set()

        # Derive spine info once from query data (no API calls)
        try:
            spine_device_names, spine_interfaces = self._derive_spine_info()
        except RuntimeError as exc:
            self.logger.error(str(exc))
            return

        # Prepare routing options once for all create_routing calls
        routing_options: RoutingOptions = RoutingOptions(design=dc_design)
        if pod and pod.asn_pool and pod.asn_pool.id:
            routing_options["asn_pool"] = pod.asn_pool.id

        # Store shared cabling context for _cable_and_route calls
        self._technical_pool_id = technical_pool_id
        self._p2p_prefix_length = p2p_prefix_length
        self._routing_options = routing_options
        self._dc_design = dc_design

        leaf_row_cache: tuple[list[str], list[str]] | None = None

        # Process leaf devices: create → cable → route
        for leaf_role in self.data.leafs or []:
            # Skip if this template would create devices already created by a previous template
            expected_names = set(
                DeviceNamingConfig(strategy=naming_conv).format_device_name(
                    self.fabric_name,
                    "leaf",
                    index=idx,
                    fabric_name=self.fabric_name,
                    indexes=leaf_indexes,
                )
                for idx in range(1, leaf_role.quantity + 1)
            )
            if expected_names <= _created_device_names:
                self.logger.info(
                    f"Skipping duplicate leaf template (devices already created: {sorted(expected_names)})"
                )
                continue

            leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="leaf",
                amount=leaf_role.quantity,
                template=leaf_role.template.model_dump(),
                naming_convention=naming_conv,
                options=DeviceOptions(
                    indexes=leaf_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=loopback_pool_id,
                    loopback_prefix_length=128 if is_ipv6 else 32,
                    management_pool=management_pool_id,
                ),
            )

            _created_device_names.update(leaf_devices)
            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = [interface.name for interface in leaf_role.template.interfaces]
            try:
                cabling_offset = self.calculate_cabling_offsets(device_count=leaf_role.quantity, device_type="leaf")
            except RuntimeError as exc:
                self.logger.error(str(exc))
                return

            await self._cable_and_route(
                bottom_devices=leaf_devices,
                bottom_interfaces=leaf_interfaces,
                top_devices=spine_device_names,
                top_interfaces=spine_interfaces,
                strategy="rack",
                offset=cabling_offset,
                bottom_sorting=pod.leaf_interface_sorting_method,
                top_sorting=pod.spine_interface_sorting_method,
            )

        # Process ToR devices: create → cable → route (per deployment type)
        for tor_role in self.data.tors or []:
            # Skip if this template would create devices already created by a previous template
            expected_names = set(
                DeviceNamingConfig(strategy=naming_conv).format_device_name(
                    self.fabric_name,
                    "tor",
                    index=idx,
                    fabric_name=self.fabric_name,
                    indexes=tor_indexes,
                )
                for idx in range(1, tor_role.quantity + 1)
            )
            if expected_names <= _created_device_names:
                self.logger.info(f"Skipping duplicate tor template (devices already created: {sorted(expected_names)})")
                continue

            tor_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="tor",
                amount=tor_role.quantity,
                template=tor_role.template.model_dump(),
                naming_convention=naming_conv,
                options=DeviceOptions(
                    indexes=tor_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=loopback_pool_id,
                    loopback_prefix_length=128 if is_ipv6 else 32,
                    management_pool=management_pool_id,
                ),
            )

            _created_device_names.update(tor_devices)
            created_tor_devices.extend(tor_devices)

            # Get only uplink interfaces from ToR template (role="uplink")
            tor_interfaces = [
                interface.name for interface in tor_role.template.interfaces if interface.role == "uplink"
            ]

            # Deployment type: middle_rack - ToRs connect to local leafs in same rack
            if deployment_type == "middle_rack":
                cabling_offset = 0

                if created_leaf_devices:
                    leaf_interfaces_objects = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__name__values=created_leaf_devices,
                        role__value="downlink",
                    )

                    if leaf_interfaces_objects:
                        leaf_interfaces = sorted(set(iface.name.value for iface in leaf_interfaces_objects))

                        await self._cable_and_route(
                            bottom_devices=tor_devices,
                            bottom_interfaces=tor_interfaces,
                            top_devices=created_leaf_devices,
                            top_interfaces=leaf_interfaces,
                            strategy="intra_rack_middle",
                            offset=cabling_offset,
                        )
                    else:
                        self.logger.error(
                            f"middle_rack deployment for {self.data.name}: No downlink interfaces found on leaf devices. "
                            "Cannot create ToR-to-leaf cabling."
                        )
                        return
                else:
                    self.logger.error(
                        f"middle_rack deployment for {self.data.name} has ToRs but no leafs. "
                        "Cannot create ToR-to-leaf cabling."
                    )
                    return

            # Deployment type: tor - ToRs connect directly to spines
            elif deployment_type == "tor":
                tors_per_rack = sum(tor_role.quantity or 0 for tor_role in self.data.tors or [])
                sibling_racks = await self.client.filters(
                    kind="LocationRack",
                    pod__ids=[pod.id],
                )
                prev_row_racks = sum(
                    1
                    for r in sibling_racks
                    if hasattr(r, "row_index") and r.row_index and r.row_index.value < self.data.row_index
                )
                try:
                    cabling_offset = self.calculate_cabling_offsets(
                        device_count=tors_per_rack,
                        device_type="tor",
                        racks_in_previous_rows=prev_row_racks,
                    )
                except RuntimeError as exc:
                    self.logger.error(str(exc))
                    return

                if spine_device_names:
                    await self._cable_and_route(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=spine_device_names,
                        top_interfaces=spine_interfaces,
                        strategy="rack",
                        offset=cabling_offset,
                        bottom_sorting=pod.leaf_interface_sorting_method,
                        top_sorting=pod.spine_interface_sorting_method,
                    )
                else:
                    self.logger.error(
                        f"tor deployment for {self.data.name}: No spine devices found in pod. "
                        "Cannot create ToR-to-spine cabling."
                    )
                    return

            # Deployment type: mixed - ToRs connect to local leafs if present, otherwise middle rack leafs
            elif deployment_type == "mixed":
                if created_leaf_devices:
                    cabling_offset = 0
                    leaf_device_names = created_leaf_devices
                    leaf_interfaces = [
                        iface.name
                        for leaf_role in self.data.leafs or []
                        for iface in leaf_role.template.interfaces
                        if iface.role == "downlink"
                    ]

                    await self._cable_and_route(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=leaf_device_names,
                        top_interfaces=leaf_interfaces,
                        strategy="rack",
                        offset=cabling_offset,
                        bottom_sorting=pod.leaf_interface_sorting_method,
                        top_sorting=pod.leaf_interface_sorting_method,
                    )
                else:
                    # ToR-only rack - connect to middle rack leafs in same row
                    tors_per_rack = len(tor_devices)
                    cabling_offset = (self.data.index - 1) * tors_per_rack

                    if leaf_row_cache is None:
                        try:
                            leaf_row_cache = await self._get_leaf_devices_in_row(
                                pod_id=pod.id, row_index=self.data.row_index
                            )
                        except RuntimeError as exc:
                            self.logger.error(str(exc))
                            return
                    leaf_device_names, leaf_interfaces = leaf_row_cache

                    if leaf_device_names:
                        await self._cable_and_route(
                            bottom_devices=tor_devices,
                            bottom_interfaces=tor_interfaces,
                            top_devices=leaf_device_names,
                            top_interfaces=leaf_interfaces,
                            strategy="intra_rack_mixed",
                            offset=cabling_offset,
                        )
                    else:
                        self.logger.error(
                            f"Mixed deployment for rack {self.data.name}: No middle rack leafs found in row {self.data.row_index}. "
                            "Cannot create ToR-to-leaf cabling."
                        )
                        return

            else:
                self.logger.warning(f"Unknown deployment_type '{deployment_type}' for rack {self.data.name}")

        # Process border-leaf devices: create with loopback + management only.
        # Cabling to the fabric happens via the endpoint generator (which detects
        # role=border-leaf and cables to the appropriate uplink/DCI interfaces).
        for bl_role in self.data.border_leafs or []:
            bl_indexes: list[int] = [
                dc.index,
                suite.index,
                self.data.row_index,
                self.data.index,
            ]
            await self.create_devices(
                deployment_id=dc.id,
                device_role="border-leaf",
                amount=bl_role.quantity,
                template=bl_role.template.model_dump(),
                naming_convention=naming_conv,
                options=DeviceOptions(
                    indexes=bl_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=loopback_pool_id,
                    loopback_prefix_length=128 if is_ipv6 else 32,
                    management_pool=management_pool_id,
                ),
            )

        # Generation completion summary
        total_devices = len(created_leaf_devices or []) + sum(tor_role.quantity for tor_role in (self.data.tors or []))
        self.logger.info(
            f"Rack generation completed: {self.data.name} - {total_devices} device(s) created with connectivity"
        )

        # For mixed deployment with network rack that has leafs: trigger ToR rack checksum updates
        # This ensures ToR racks in the same row are generated after network rack completes.
        # Border-leaf-only racks are skipped — they have no leafs to cascade from.
        if deployment_type == "mixed" and self.data.rack_type == "network" and self.data.leafs:
            await self.update_checksum()
