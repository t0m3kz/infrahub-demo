from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from ..common import CablingOptions, CommonGenerator
from ..helpers.rack import RackPlanner, RackRolesHelper, parse_rack_data
from ..models import RackModel
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, LocationRack
from ..rack import RackMixin


class RackGenerator(RackMixin, CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    @property
    def _plan(self) -> RackPlanner:
        """Lazily initialized helper for deterministic rack planning primitives."""
        helper = getattr(self, "_planning_helper", None)
        if helper is None:
            helper = RackPlanner()
            self._planning_helper = helper
        return helper

    @property
    def _roles(self) -> RackRolesHelper:
        """Lazily initialized helper for role-specific generation pipelines."""
        helper = getattr(self, "_roles_helper", None)
        if helper is None:
            helper = RackRolesHelper(self)
            self._roles_helper = helper
        return helper

    def _has_role_templates(self, roles: list | None) -> bool:
        """Return True when at least one role template has positive quantity."""
        if not roles:
            return False
        return any((getattr(role, "quantity", 0) or 0) > 0 for role in roles)

    def _has_tor_like_templates(self) -> bool:
        """ToR-side switch templates (physical ToR, l2-leaf, access-leaf)."""
        return (
            self._has_role_templates(self.data.tors)
            or self._has_role_templates(self.data.l2_leafs)
            or self._has_role_templates(self.data.access_leafs)
        )

    def _has_any_switch_templates(self) -> bool:
        """Any switch template that rack generator can materialize."""
        return (
            self._has_role_templates(self.data.leafs)
            or self._has_tor_like_templates()
            or self._has_role_templates(self.data.border_leafs)
        )

    @staticmethod
    def _rack_sort_key(rack: LocationRack) -> tuple[int, int, str]:
        """Compatibility wrapper retained for existing tests/callers."""
        return RackPlanner.rack_sort_key(rack)

    @staticmethod
    def _parse_rack_data(data: dict) -> RackModel:
        """Compatibility wrapper retained for existing tests/callers."""
        return RackPlanner.parse_rack_data(data)

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
        network_racks = sorted(
            (r for r in row_racks if r.rack_type.value == "network"),
            key=self._rack_sort_key,
        )
        tor_racks = sorted(
            (r for r in row_racks if r.rack_type.value == "tor"),
            key=self._rack_sort_key,
        )

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
        """Thin wrapper around helper function for readability and compatibility."""
        return self._plan.calculate_cabling_offsets(
            data=self.data,
            logger=self.logger,
            device_count=device_count,
            device_type=device_type,
            racks_in_previous_rows=racks_in_previous_rows,
            leafs_per_rack=leafs_per_rack,
            total_tors_in_pod=total_tors_in_pod,
        )

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
            # Keep module-level wrapper usage for compatibility with existing tests
            # that patch generators.topology.rack.parse_rack_data.
            self.data = parse_rack_data(data)
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

        # Endpoint-only racks (no switch templates) are handled by endpoint flows.
        # Skip rack generation here to avoid unnecessary checksum/dependency gating.
        if not self._has_any_switch_templates():
            self.logger.info(
                f"Rack {self.data.name} has no switch templates — skipping rack generator (endpoint-only rack)."
            )
            return

        if not await self._checksum_ready():
            return

        if not await self._earlier_rows_ready():
            return

        if (
            deployment_type == "mixed"
            and self.data.rack_type == "tor"
            and self._has_tor_like_templates()
            and not await self._tor_leafs_ready()
        ):
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

    async def _generate_leafs(self, created_leaf_devices: list[str]) -> bool:
        """Create -> cable -> route leaf devices.

        Returns False on a fatal error (already logged).
        """
        pod = self.data.pod
        for leaf_role in self.data.leafs or []:
            expected_names = self._roles.expected_names(role="leaf", quantity=leaf_role.quantity)
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
                options=self._roles.build_device_options(allocate_loopback=True),
            )

            self._created_device_names.update(leaf_devices)
            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = self._roles.template_interfaces(leaf_role.template)
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
        """Create -> cable -> route ToR devices.

        L2-only aggregation switches below leafs use role "l2-leaf"/"access-leaf" instead.
        Returns False on a fatal error (already logged).
        """
        pod = self.data.pod
        for tor_role in self.data.tors or []:
            expected_names = self._roles.expected_names(role="tor", quantity=tor_role.quantity)
            if expected_names <= self._created_device_names:
                self.logger.info(f"Skipping duplicate tor template (devices already created: {sorted(expected_names)})")
                continue

            tor_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="tor",
                amount=tor_role.quantity,
                template=tor_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=self._roles.build_device_options(allocate_loopback=True),
            )

            self._created_device_names.update(tor_devices)

            tor_interfaces = self._roles.template_interfaces(tor_role.template, role="uplink")

            tors_per_rack = sum(r.quantity or 0 for r in self.data.tors or [])
            # Live count, not design.compute_racks_per_row (a max capacity) - a pod with
            # fewer racks per row than its design allows would otherwise get an inflated
            # offset that overflows past the real spine downlink interfaces.
            sibling_racks = await self.client.filters(kind=LocationRack, pod__ids=[pod.id])
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
                    f"Rack {self.data.name}: No spine devices found in pod - cannot cable ToRs to spines."
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
        """Create l2-leaf devices and cable them to local leafs. No routing - L2-only."""
        pod = self.data.pod
        for l2_leaf_role in self.data.l2_leafs or []:
            l2_leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="l2-leaf",
                amount=l2_leaf_role.quantity,
                template=l2_leaf_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=self._roles.build_device_options(allocate_loopback=False),
            )

            self._created_device_names.update(l2_leaf_devices)

            l2_leaf_interfaces = self._roles.template_interfaces(l2_leaf_role.template, role="uplink")

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
                    pool=None,  # l2-leaf<->leaf links are 802.1Q trunks - no IP either end.
                ),
            )

    async def _generate_access_leafs(self, created_leaf_devices: list[str]) -> None:
        """Create access-leaf devices: routed VTEPs in the same position as l2-leafs.

        Cabled to the local leaf pair (underlay eBGP/OSPF), plus a second,
        underlay-less overlay EVPN session straight to the pod's spines.
        """
        pod = self.data.pod
        for access_leaf_role in self.data.access_leafs or []:
            access_leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="access-leaf",
                amount=access_leaf_role.quantity,
                template=access_leaf_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=self._roles.build_device_options(allocate_loopback=True),
            )

            self._created_device_names.update(access_leaf_devices)

            access_leaf_interfaces = self._roles.template_interfaces(access_leaf_role.template, role="uplink")

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
                overlay_only_options = self._roles.overlay_only_routing_options()
                await self.create_routing(
                    bottom_devices=access_leaf_devices,
                    top_devices=self._spine_device_names,
                    options=overlay_only_options,
                    p2p_interfaces=[],
                    bottom_role="access-leaf",
                    top_role="spine",
                )

    async def _generate_border_leafs(self, deployment_type: str) -> None:
        """Create border-leaf devices and cable uplinks to pod spines (always, same as leafs)."""
        pod = self.data.pod
        dc = pod.parent
        for bl_role in self.data.border_leafs or []:
            bl_devices = await self.create_devices(
                deployment_id=dc.id,
                device_role="border-leaf",
                amount=bl_role.quantity,
                template=bl_role.template.model_dump(),
                naming_convention=self._naming_conv,
                options=self._roles.build_device_options(allocate_loopback=True),
            )

            self._created_device_names.update(bl_devices)

            all_bl_uplinks = self._roles.template_interfaces(bl_role.template, role="uplink")
            if not all_bl_uplinks:
                self.logger.warning(
                    f"Rack {self.data.name}: border-leaf template has no uplink interfaces - skipping fabric cabling"
                )
                continue

            max_super_spines = dc.amount_of_super_spines
            spine_count = pod.amount_of_spines
            dci_reserved, bl_uplink_interfaces = self._roles.split_border_leaf_uplinks(
                uplink_interfaces=all_bl_uplinks,
                reserved_dci_count=max_super_spines,
                spine_count=spine_count,
            )
            if not bl_uplink_interfaces:
                self.logger.warning(
                    f"Rack {self.data.name}: not enough border-leaf uplinks for pod spines "
                    f"(uplinks={len(all_bl_uplinks)}, reserved_dci={max_super_spines}, "
                    f"needed={spine_count}) - skipping fabric cabling"
                )
                continue

            self.logger.info(
                f"Rack {self.data.name}: border-leaf fabric uplinks {bl_uplink_interfaces} "
                f"(DCI reserved: {dci_reserved})"
            )

            leafs_per_rack = self._roles.border_leafs_per_rack()

            total_tors_in_pod: int | None = None
            if deployment_type == "tor":
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
