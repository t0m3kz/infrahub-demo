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

    def _derive_super_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive super-spine device names and uplink interface names from query data.

        The DC generator always creates super-spines with indexes=[dc.index], so names
        are deterministic — no API call needed.  Super-spine template interfaces are
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
                f"Rack {self.data.name}: Cannot derive super-spine info — "
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

    async def _resolve_local_leaf_cabling_target(
        self,
        *,
        created_leaf_devices: list[str],
        leaf_row_cache: tuple[list[str], list[str]] | None,
        devices_per_rack: int,
        role_label: str,
    ) -> tuple[list[str], list[str], int, Literal["intra_rack_middle", "intra_rack_mixed"]] | None:
        """Resolve the leaf devices/interfaces a rack's l2-leaf or access-leaf batch cables to.

        Both roles sit below leafs (not spines) with the identical two cases:
        network rack — cable to the local leaf pair created earlier in this same
        run; compute rack (mixed deployment) — cable to the middle-rack leafs in
        the same row, looked up once per rack and cached by the caller.

        Returns (leaf_devices, leaf_interfaces, cabling_offset, strategy), or
        None if the target couldn't be resolved (already logged) — the caller
        should ``continue`` to the next role template.
        """
        if created_leaf_devices:
            leaf_interfaces_objects = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=created_leaf_devices,
                role__value="downlink",
            )
            if not leaf_interfaces_objects:
                self.logger.error(
                    f"Rack {self.data.name}: No downlink interfaces on leafs — cannot cable {role_label}s."
                )
                return None
            leaf_interfaces = sorted(set(iface.name.value for iface in leaf_interfaces_objects))
            return created_leaf_devices, leaf_interfaces, 0, "intra_rack_middle"

        # Compute rack (mixed deployment): cable to middle-rack leafs in same row
        cabling_offset = (self.data.index - 1) * devices_per_rack

        if leaf_row_cache is None:
            try:
                leaf_row_cache = await self._get_leaf_devices_in_row(
                    pod_id=self.data.pod.id, row_index=self.data.row_index
                )
            except RuntimeError as exc:
                self.logger.error(str(exc))
                return None
        leaf_device_names, leaf_interfaces = leaf_row_cache

        if not leaf_device_names:
            self.logger.error(
                f"Rack {self.data.name}: No middle-rack leafs in row {self.data.row_index} — "
                f"cannot cable {role_label}s."
            )
            return None

        return leaf_device_names, leaf_interfaces, cabling_offset, "intra_rack_mixed"

    def calculate_cabling_offsets(
        self,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
        leafs_per_rack: int = 0,
        total_tors_in_pod: int | None = None,
    ) -> int:
        """Calculate cabling offset using simple formula based on rack position."""

        current_index = self.data.index

        # deployment_type and max_tors_per_row are both derived from pod.design
        pod = self.data.pod
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

        # For mixed deployment ToRs: static offset based on row + rack index
        # Formula: (row_index - 1) × tors_per_row + (rack_index - 1) × tors_per_rack
        elif deployment_type == "mixed" and device_type == "tor":
            tors_per_row = max_tors_per_row if pod.design else device_count
            offset = (self.data.row_index - 1) * tors_per_row + (current_index - 1) * device_count
            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row_index={self.data.row_index}, index={current_index}, tors_per_rack={device_count}, "
                f"tors_per_row={tors_per_row}, mode=mixed)"
            )

        # For mixed/middle_rack deployment leafs: calculate offset based on row position
        # Middle rack leafs serve all ToRs in their row
        elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            offset = (self.data.row_index - 1) * device_count

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row_index={self.data.row_index}, leafs_per_rack={device_count}, mode={deployment_type})"
            )

        # Border-leafs connect to pod spines after all regular leafs across all rows.
        # Formula: total_rows × leafs_per_rack + (row_index - 1) × border_leaf_count
        elif deployment_type in ("mixed", "middle_rack") and device_type == "border_leaf":
            total_rows = pod.design.rows if pod.design else 1
            base_bl_offset = total_rows * leafs_per_rack
            offset = base_bl_offset + (self.data.row_index - 1) * device_count

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row_index={self.data.row_index}, total_rows={total_rows}, "
                f"leafs_per_rack={leafs_per_rack}, base_bl_offset={base_bl_offset}, mode={deployment_type})"
            )

        # For tor deployment border-leafs: offset past all ToRs, then row-based position.
        # Formula: total_tors_in_pod + (row_index - 1) × border_leaf_count
        # Uses the actual deployed ToR count (passed in), not design.rows ×
        # design-max tors_per_row — a pod deployed with fewer racks/tors per row
        # than its design allows would otherwise get an inflated offset that
        # overflows past the real number of spine downlink interfaces.
        elif deployment_type == "tor" and device_type == "border_leaf":
            if total_tors_in_pod is not None:
                base_bl_offset = total_tors_in_pod
            else:
                total_rows = pod.design.rows if pod.design else 1
                tors_per_row = max_tors_per_row if pod.design else 0
                base_bl_offset = total_rows * tors_per_row
            offset = base_bl_offset + (self.data.row_index - 1) * device_count

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row_index={self.data.row_index}, base_bl_offset={base_bl_offset}, mode={deployment_type})"
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

    async def _checksum_ready(self) -> bool:
        """Return True if this rack has a usable checksum; False if generation should abort.

        When the checksum is missing the method attempts to inherit one from a sibling
        rack (middle → ToR in mixed, sibling network → network in middle_rack).
        A successful inheritance saves the rack and returns False so the generator
        framework re-fires this rack with the new checksum.
        """
        if self.data.checksum:
            return True

        deployment_type = self.data.pod.deployment_type

        if deployment_type == "mixed" and self.data.rack_type == "tor":
            middle_racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[self.data.pod.id],
                row_index__value=self.data.row_index,
                rack_type__value="network",
            )
            if middle_racks and middle_racks[0].checksum.value:
                rack_obj = await self.client.get(kind=LocationRack, id=self.data.id)
                rack_obj.checksum.value = middle_racks[0].checksum.value
                await rack_obj.save(allow_upsert=True)
                self.logger.info(
                    f"ToR rack {self.data.name} inherited checksum {middle_racks[0].checksum.value} "
                    f"from middle rack {middle_racks[0].name.value}. "
                    "Checksum update will trigger generator again to create devices."
                )
            else:
                self.logger.warning(
                    f"ToR rack {self.data.name} has no checksum and no middle rack found "
                    f"in row {self.data.row_index} - skipping generation."
                )
            return False

        if deployment_type == "middle_rack" and self.data.rack_type == "network":
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
            else:
                self.logger.warning(
                    f"Network rack {self.data.name} has no checksum and no sibling racks found in pod "
                    f"to inherit from — run pod generator first."
                )
            return False

        self.logger.warning(
            f"Rack {self.data.name} has no checksum set - skipping generation. "
            "Checksum will be set by pod or middle rack generator."
        )
        return False

    async def _cable_and_route(
        self,
        *,
        bottom_devices: list[str],
        bottom_interfaces: list[str],
        top_devices: list[str],
        top_interfaces: list[str],
        strategy: Literal["pod", "rack", "intra_rack", "intra_rack_middle", "intra_rack_mixed"],
        offset: int,
        bottom_role: str,
        top_role: str,
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
    ) -> None:
        p2p_pairs = await self.create_cabling(
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
        if self._routing_options.get("design"):
            await self.create_routing(
                bottom_devices=bottom_devices,
                top_devices=top_devices,
                options=self._routing_options,
                p2p_interfaces=p2p_pairs,
                bottom_role=bottom_role,
                top_role=top_role,
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

        pod = self.data.pod
        deployment_type = pod.deployment_type

        self.logger.info(
            f"Starting rack generation: {self.data.name} [type={self.data.rack_type}, deployment={deployment_type}]"
        )

        if not await self._checksum_ready():
            return

        if deployment_type == "mixed" and self.data.rack_type == "tor" and not await self._tor_leafs_ready():
            return

        self.logger.info(f"Generating topology for rack {self.data.name}")

        if not self._prepare_generation_context():
            return

        # Names created THIS run, to skip duplicate templates within this invocation.
        # Do NOT pre-populate with existing devices — every object must be re-upserted
        # each run or the generator's group-context cleanup deletes it as "unused".
        self._created_device_names: set[str] = set()
        self._leaf_row_cache: tuple[list[str], list[str]] | None = None
        created_leaf_devices: list[str] = []

        if not await self._generate_leafs(created_leaf_devices):
            return
        if not await self._generate_tors():
            return
        await self._generate_l2_leafs(created_leaf_devices)
        await self._generate_access_leafs(created_leaf_devices)
        await self._generate_border_leafs(deployment_type)

        # Generation completion summary
        total_devices = len(created_leaf_devices) + sum(tor_role.quantity for tor_role in (self.data.tors or []))
        self.logger.info(
            f"Rack generation completed: {self.data.name} - {total_devices} device(s) created with connectivity"
        )

        # For mixed deployment with network rack that has leafs: trigger ToR rack checksum updates
        # This ensures ToR racks in the same row are generated after network rack completes.
        # Border-leaf-only racks are skipped — they have no leafs to cascade from.
        if deployment_type == "mixed" and self.data.rack_type == "network" and self.data.leafs:
            await self.update_checksum()

    async def _tor_leafs_ready(self) -> bool:
        """Mixed deployment: ToR racks wait for the network rack's leafs to exist first."""
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
            return False

        leaf_data = await self.fetch_rack_devices_with_interfaces(
            rack=network_racks[0],
            role_filter="leaf",
        )

        if not leaf_data:
            self.logger.info(
                f"ToR rack {self.data.name} waiting for leafs to be generated in row {self.data.row_index} - skipping this run."
            )
            return False

        self.logger.info(
            f"ToR rack {self.data.name} found {len(leaf_data)} leaf devices in row {self.data.row_index} "
            "- proceeding with ToR generation"
        )
        return True

    def _prepare_generation_context(self) -> bool:
        """Compute and store shared context needed by every per-role generation method.

        Returns False if a prerequisite is missing (already logged) and generation should abort.
        """
        pod = self.data.pod
        dc = pod.parent
        self.deployment_id = dc.id  # Store for cable linking
        self.pod_name = pod.name.lower()
        self.fabric_name = dc.name.lower()

        # Validate pools exist - they should be created by pod generator
        # Failing fast prevents race conditions when multiple racks are created simultaneously
        if not pod.loopback_pool or not pod.prefix_pool:
            self.logger.error(
                f"Rack {self.data.name}: Pod {pod.name} pools not found. "
                f"Run pod generator first: infrahubctl generator generate_pod name={pod.name}"
            )
            return False

        # Pass pool IDs from query data — _resolve_pool resolves by ID directly,
        # avoiding name-based fallback lookups
        self._management_pool_id = dc.management_pool.id if dc.management_pool else None
        self._loopback_pool_id = pod.loopback_pool.id if pod.loopback_pool else None

        # Indexes for leaf/ToR/l2-leaf/access-leaf naming, e.g. dc1-fab1-pod1-suite1-row1-rack5-leaf-01
        suite = self.data.parent  # LocationSuite
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

        # Get naming convention from DC design (wired through GQL query)
        dc_design = dc.design
        self._naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            dc.naming_convention,
        )
        self._is_ipv6 = dc_design.is_ipv6 if dc_design else False

        # Derive spine info once from query data (no API calls)
        try:
            self._spine_device_names, self._spine_interfaces = self._derive_spine_info()
        except RuntimeError as exc:
            self.logger.error(str(exc))
            return False

        # Prepare routing options once for all create_routing calls
        routing_options: RoutingOptions = RoutingOptions(design=dc_design)
        if pod.asn_pool and pod.asn_pool.id:
            routing_options["asn_pool"] = pod.asn_pool.id

        # Store shared cabling/routing context for _cable_and_route calls
        self._technical_pool_id = pod.prefix_pool.id if pod.prefix_pool else None
        # P2P prefix length: /127 for IPv6/dual-stack (default), /31 for IPv4 (exception)
        self._p2p_prefix_length = 127 if dc_design and getattr(dc_design, "p2p_ipv6", False) else 31
        self._routing_options = routing_options
        return True

    def _expected_device_names(self, role: str, quantity: int) -> set[str]:
        """Device names a role template of the given quantity would create."""
        return {
            DeviceNamingConfig(strategy=self._naming_conv).format_device_name(
                self.fabric_name,
                role,
                index=idx,
                fabric_name=self.fabric_name,
                indexes=self._device_indexes,
            )
            for idx in range(1, quantity + 1)
        }

    async def _generate_leafs(self, created_leaf_devices: list[str]) -> bool:
        """Create → cable → route leaf devices. Appends to created_leaf_devices in place.

        Returns False on a fatal error (already logged).
        """
        pod = self.data.pod
        for leaf_role in self.data.leafs or []:
            expected_names = self._expected_device_names("leaf", leaf_role.quantity)
            if expected_names <= self._created_device_names:
                self.logger.info(
                    f"Skipping duplicate leaf template (devices already created: {sorted(expected_names)})"
                )
                continue

            leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="leaf",
                amount=leaf_role.quantity,
                template=leaf_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=DeviceOptions(
                    indexes=self._device_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=self._loopback_pool_id,
                    loopback_prefix_length=128 if self._is_ipv6 else 32,
                    management_pool=self._management_pool_id,
                ),
            )

            self._created_device_names.update(leaf_devices)
            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = [interface.name for interface in leaf_role.template.interfaces]
            try:
                cabling_offset = self.calculate_cabling_offsets(device_count=leaf_role.quantity, device_type="leaf")
            except RuntimeError as exc:
                self.logger.error(str(exc))
                return False

            await self._cable_and_route(
                bottom_devices=leaf_devices,
                bottom_interfaces=leaf_interfaces,
                top_devices=self._spine_device_names,
                top_interfaces=self._spine_interfaces,
                strategy="rack",
                offset=cabling_offset,
                bottom_role="leaf",
                top_role="spine",
                bottom_sorting=pod.leaf_interface_sorting_method,
                top_sorting=pod.spine_interface_sorting_method,
            )
        return True

    async def _generate_tors(self) -> bool:
        """Create → cable → route ToR devices (always VTEPs, cabled directly to spines).

        L2-only aggregation switches below leafs use role "l2-leaf"/"access-leaf" instead.
        Returns False on a fatal error (already logged).
        """
        pod = self.data.pod
        for tor_role in self.data.tors or []:
            expected_names = self._expected_device_names("tor", tor_role.quantity)
            if expected_names <= self._created_device_names:
                self.logger.info(f"Skipping duplicate tor template (devices already created: {sorted(expected_names)})")
                continue

            tor_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="tor",
                amount=tor_role.quantity,
                template=tor_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=DeviceOptions(
                    indexes=self._device_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=self._loopback_pool_id,
                    loopback_prefix_length=128 if self._is_ipv6 else 32,
                    management_pool=self._management_pool_id,
                ),
            )

            self._created_device_names.update(tor_devices)

            tor_interfaces = [
                interface.name for interface in tor_role.template.interfaces if interface.role == "uplink"
            ]

            tors_per_rack = sum(r.quantity or 0 for r in self.data.tors or [])
            # Live count, not design.compute_racks_per_row (a max capacity) — a pod with
            # fewer racks per row than its design allows would otherwise get an inflated
            # offset that overflows past the real spine downlink interfaces.
            sibling_racks = await self.client.filters(kind="LocationRack", pod__ids=[pod.id])
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
                return False

            if not self._spine_device_names:
                self.logger.error(
                    f"Rack {self.data.name}: No spine devices found in pod — cannot cable ToRs to spines."
                )
                return False

            await self._cable_and_route(
                bottom_devices=tor_devices,
                bottom_interfaces=tor_interfaces,
                top_devices=self._spine_device_names,
                top_interfaces=self._spine_interfaces,
                strategy="rack",
                offset=cabling_offset,
                bottom_role="tor",
                top_role="spine",
                bottom_sorting=pod.leaf_interface_sorting_method,
                top_sorting=pod.spine_interface_sorting_method,
            )
        return True

    async def _generate_l2_leafs(self, created_leaf_devices: list[str]) -> None:
        """Create l2-leaf devices and cable them to local leafs. No routing — L2-only."""
        pod = self.data.pod
        for l2_leaf_role in self.data.l2_leafs or []:
            l2_leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="l2-leaf",
                amount=l2_leaf_role.quantity,
                template=l2_leaf_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=DeviceOptions(
                    indexes=self._device_indexes,
                    allocate_loopback=False,
                    rack=self.data.id,
                    management_pool=self._management_pool_id,
                ),
            )

            self._created_device_names.update(l2_leaf_devices)

            l2_leaf_interfaces = [
                interface.name for interface in l2_leaf_role.template.interfaces if interface.role == "uplink"
            ]

            target = await self._resolve_local_leaf_cabling_target(
                created_leaf_devices=created_leaf_devices,
                leaf_row_cache=self._leaf_row_cache,
                devices_per_rack=len(l2_leaf_devices),
                role_label="l2-leaf",
            )
            if target is None:
                continue
            leaf_device_names, leaf_interfaces, cabling_offset, strategy = target
            if not created_leaf_devices:
                self._leaf_row_cache = (leaf_device_names, leaf_interfaces)

            await self.create_cabling(
                bottom_devices=l2_leaf_devices,
                bottom_interfaces=l2_leaf_interfaces,
                top_devices=leaf_device_names,
                top_interfaces=leaf_interfaces,
                strategy=strategy,
                options=CablingOptions(
                    cabling_offset=cabling_offset,
                    pool=None,  # l2-leaf<->leaf links are 802.1Q trunks — no IP either end.
                ),
            )

    async def _generate_access_leafs(self, created_leaf_devices: list[str]) -> None:
        """Create access-leaf devices: routed VTEPs in the same position as l2-leafs.

        Cabled to the local leaf pair (underlay eBGP/OSPF), plus a second,
        underlay-less overlay EVPN session straight to the pod's spines (mirrors
        pod.py's skip_underlay=True spine pre-seed).
        """
        pod = self.data.pod
        for access_leaf_role in self.data.access_leafs or []:
            access_leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="access-leaf",
                amount=access_leaf_role.quantity,
                template=access_leaf_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=DeviceOptions(
                    indexes=self._device_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=self._loopback_pool_id,
                    loopback_prefix_length=128 if self._is_ipv6 else 32,
                    management_pool=self._management_pool_id,
                ),
            )

            self._created_device_names.update(access_leaf_devices)

            access_leaf_interfaces = [
                interface.name for interface in access_leaf_role.template.interfaces if interface.role == "uplink"
            ]

            target = await self._resolve_local_leaf_cabling_target(
                created_leaf_devices=created_leaf_devices,
                leaf_row_cache=self._leaf_row_cache,
                devices_per_rack=len(access_leaf_devices),
                role_label="access-leaf",
            )
            if target is None:
                continue
            leaf_device_names, leaf_interfaces, cabling_offset, strategy = target
            if not created_leaf_devices:
                self._leaf_row_cache = (leaf_device_names, leaf_interfaces)

            await self._cable_and_route(
                bottom_devices=access_leaf_devices,
                bottom_interfaces=access_leaf_interfaces,
                top_devices=leaf_device_names,
                top_interfaces=leaf_interfaces,
                strategy=strategy,
                offset=cabling_offset,
                bottom_role="access-leaf",
                top_role="leaf",
            )
            if self._routing_options.get("design"):
                # No physical link to spines — skip underlay planning for this call.
                overlay_only_options: RoutingOptions = {**self._routing_options, "skip_underlay": True}
                await self.create_routing(
                    bottom_devices=access_leaf_devices,
                    top_devices=self._spine_device_names,
                    options=overlay_only_options,
                    p2p_interfaces=[],
                    bottom_role="access-leaf",
                    top_role="spine",
                )

    async def _generate_border_leafs(self, deployment_type: str) -> None:
        """Create border-leaf devices and cable uplinks to pod spines (always, same as leafs).

        Endpoint generator only handles FW/LB → border-leaf connections.
        """
        pod = self.data.pod
        dc = pod.parent
        for bl_role in self.data.border_leafs or []:
            bl_devices = await self.create_devices(
                deployment_id=dc.id,
                device_role="border-leaf",
                amount=bl_role.quantity,
                template=bl_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=DeviceOptions(
                    indexes=self._device_indexes,
                    allocate_loopback=True,
                    rack=self.data.id,
                    loopback_pool=self._loopback_pool_id,
                    loopback_prefix_length=128 if self._is_ipv6 else 32,
                    management_pool=self._management_pool_id,
                ),
            )

            # Uplinks sorted by name: [0..max_super_spines) reserved for DCI, then pod spines.
            all_bl_uplinks = sorted([iface.name for iface in bl_role.template.interfaces if iface.role == "uplink"])
            if not all_bl_uplinks:
                self.logger.warning(
                    f"Rack {self.data.name}: border-leaf template has no uplink interfaces — skipping fabric cabling"
                )
                continue

            max_super_spines = dc.amount_of_super_spines
            spine_count = pod.amount_of_spines
            bl_uplink_interfaces = all_bl_uplinks[max_super_spines : max_super_spines + spine_count]
            if not bl_uplink_interfaces:
                self.logger.warning(
                    f"Rack {self.data.name}: not enough border-leaf uplinks for pod spines "
                    f"(uplinks={len(all_bl_uplinks)}, reserved_dci={max_super_spines}, "
                    f"needed={spine_count}) — skipping fabric cabling"
                )
                continue

            self.logger.info(
                f"Rack {self.data.name}: border-leaf fabric uplinks {bl_uplink_interfaces} "
                f"(DCI reserved: {all_bl_uplinks[:max_super_spines]})"
            )

            # Border-leafs connect to pod spines after regular leafs. Use the design's
            # max_leafs_per_network_rack (not this rack's own leaf count) so the base
            # offset stays consistent across rows with different leaf counts.
            design_max_leafs = pod.design.max_leafs_per_network_rack if pod.design else 0
            leafs_per_rack = max(design_max_leafs, sum(r.quantity or 0 for r in self.data.leafs or []))

            total_tors_in_pod: int | None = None
            if deployment_type == "tor":
                # Live ToR-rack count (not design.rows * design-max, a whole-pod max
                # capacity) so a pod with fewer racks per row than its design allows
                # doesn't get an inflated offset. This rack has no ToR templates of its
                # own, so max_tors_per_compute_rack stands in for its per-rack quantity.
                max_tors_per_compute_rack = pod.design.max_tors_per_compute_rack if pod.design else 0
                sibling_tor_racks = await self.client.filters(
                    kind=LocationRack, pod__ids=[pod.id], rack_type__value="tor"
                )
                total_tors_in_pod = len(sibling_tor_racks) * max_tors_per_compute_rack

            cabling_offset = self.calculate_cabling_offsets(
                device_count=bl_role.quantity,
                device_type="border_leaf",
                leafs_per_rack=leafs_per_rack,
                total_tors_in_pod=total_tors_in_pod,
            )

            await self._cable_and_route(
                bottom_devices=bl_devices,
                bottom_interfaces=bl_uplink_interfaces,
                top_devices=self._spine_device_names,
                top_interfaces=self._spine_interfaces,
                strategy="rack",
                offset=cabling_offset,
                bottom_role="border-leaf",
                top_role="spine",
                bottom_sorting=pod.leaf_interface_sorting_method,
                top_sorting=pod.spine_interface_sorting_method,
            )
