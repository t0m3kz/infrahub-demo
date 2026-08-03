from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from infrahub_sdk.protocols import CoreStandardGroup

from ..common import CablingOptions, CommonGenerator
from ..helpers.rack import RackPlanner, RackRolesHelper, parse_rack_data
from ..models import DeviceRole, RackModel, Template
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, LocationRack, ManagedMLAG
from ..rack import (
    MUTUALLY_EXCLUSIVE_ROLE_GROUPS,
    ROLES_BY_DEPLOYMENT_TYPE,
    ROW_DEPENDENT_RACK_TYPES,
    RackMixin,
)

_ROW_LEAF_MAX_RETRIES = 10
_ROW_LEAF_RETRY_DELAY = 3.0
_ROW_LEAF_RETRY_CAP = 20.0
_ROW_LEAF_RETRY_JITTER = 0.25


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
    def _spine_role(self) -> Literal["spine", "border-spine"]:
        """This pod's spine-slot role, set by _prepare_generation_context().
        Defaults to "spine" when unset (e.g. a unit test driving a generator
        method directly without going through that setup)."""
        return getattr(self, "_spine_role_value", "spine")

    @_spine_role.setter
    def _spine_role(self, value: Literal["spine", "border-spine"]) -> None:
        self._spine_role_value = value

    @property
    def _roles(self) -> RackRolesHelper:
        """Lazily initialized helper for role-specific generation pipelines."""
        helper = getattr(self, "_roles_helper", None)
        if helper is None:
            helper = RackRolesHelper(self)
            self._roles_helper = helper
        return helper

    def _has_role_templates(self, roles: list[DeviceRole]) -> bool:
        """Return True when at least one role template has positive quantity."""
        return any(role.quantity > 0 for role in roles)

    def _has_tor_like_templates(self) -> bool:
        """ToR-side switch templates (physical ToR, l2-leaf, access-leaf)."""
        return (
            self._has_role_templates(self.data.tors)
            or self._has_role_templates(self.data.l2_leafs)
            or self._has_role_templates(self.data.access_leafs)
        )

    def _has_any_switch_templates(self) -> bool:
        """Any switch template this generator can materialize."""
        return self._has_role_templates(self.data.leafs) or self._has_tor_like_templates()

    def _present_roles(self) -> set[str]:
        """Role names with at least one positive-quantity fabric_templates entry on this rack."""
        role_templates = {
            "leaf": self.data.leafs,
            "tor": self.data.tors,
            "l2_leaf": self.data.l2_leafs,
            "access_leaf": self.data.access_leafs,
        }
        return {role for role, templates in role_templates.items() if self._has_role_templates(templates)}

    def _role_compatibility_errors(self, deployment_type: str) -> list[str]:
        """Return human-readable errors for fabric_templates roles this rack can't actually cable.

        Two independent checks:
        - ROLES_BY_DEPLOYMENT_TYPE: is each present role valid for this pod's deployment_type
          at all (e.g. "tor" has no cabling strategy under middle_rack).
        - MUTUALLY_EXCLUSIVE_ROLE_GROUPS: "tor" (cables to a spine) and "l2_leaf"/"access_leaf"
          (cable to a local leaf pair) can't coexist on one rack regardless of deployment_type.
        """
        present = self._present_roles()
        errors: list[str] = []

        allowed = ROLES_BY_DEPLOYMENT_TYPE.get(deployment_type, frozenset())
        incompatible = sorted(present - allowed)
        if incompatible:
            errors.append(
                f"role(s) {incompatible} are not valid for deployment_type={deployment_type!r} "
                f"(allowed: {sorted(allowed)})"
            )

        for group in MUTUALLY_EXCLUSIVE_ROLE_GROUPS:
            conflicting = sorted(present & group)
            if len(conflicting) > 1:
                errors.append(f"role(s) {conflicting} are mutually exclusive on the same rack")

        return errors

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

        Uses self.data context (pod + row) when ``rack`` is not given.
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

    async def _fan_out_to_row_dependent_racks(self) -> None:
        """Fan out to the row-dependent (tor/compute) racks' own add_rack runs (mixed mode only).

        Row-dependent racks have no local leaf — they cable to this network rack's
        leafs, which this method only runs after they exist.

        wait=False, not wait=True: a row-dependent rack's own generate() waits for
        an in-flight add_rack task on this network rack (self-nesting guard — same
        generator name on both sides). Blocking here would keep this task RUNNING,
        which that guard would see as "still in flight" and wait on — deadlock.
        """
        deployment_type = self.data.pod.deployment_type

        # Only network racks in mixed deployment feed row-dependent racks.
        if deployment_type != "mixed" or self.data.rack_type != "network":
            return

        # Skip racks that have no leaf template — e.g. border-leaf-only racks.
        # These are valid network racks but have no leafs to cascade from.
        if not self.data.leafs:
            self.logger.warning(f"Rack {self.data.name} has no leaf template — skipping row-dependent rack fan-out")
            return

        row_racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.pod.id],
            row_index__value=self.data.row_index,
        )
        row_dependent_racks = sorted(
            (r for r in row_racks if r.rack_type.value in ROW_DEPENDENT_RACK_TYPES),
            key=self._rack_sort_key,
        )

        await self.run_generator("add_rack", [rack.id for rack in row_dependent_racks], wait=False)

    async def _get_leaf_devices_in_row(self, pod_id: str, row_index: int) -> tuple[list[str], list[str]]:
        """Query leaf devices in same row and their downlink interfaces, for ToR-to-leaf cabling.

        Retries: a row-dependent (compute/tor) rack's created-trigger can fire
        and reach this before its row's network rack has created its leaf
        devices — nesting racks under their pod in one object-load mutation
        removed the incidental ordering that made this reliable in practice.
        Checking the actual devices/interfaces (not a task list) closes that
        race regardless of trigger-dispatch timing.
        """
        racks_in_row: list[Any] = []
        leaf_devices: list[Any] = []
        leaf_interfaces: list[Any] = []
        for attempt in range(_ROW_LEAF_MAX_RETRIES):
            racks_in_row = await self.client.filters(
                kind=LocationRack,
                pod__ids=[pod_id],
                row_index__value=row_index,
            )
            leaf_devices = await self.client.filters(
                kind=DcimPhysicalDevice,
                role__value="leaf",
                rack__ids=[rack.id for rack in racks_in_row],
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
            if racks_in_row and leaf_devices and leaf_interfaces:
                break
            if attempt < _ROW_LEAF_MAX_RETRIES - 1:
                delay = self._retry_delay(
                    _ROW_LEAF_RETRY_DELAY, attempt, cap=_ROW_LEAF_RETRY_CAP, jitter=_ROW_LEAF_RETRY_JITTER
                )
                self.logger.info(
                    f"Rack {self.data.name}: row {row_index} leaf devices/interfaces not ready yet "
                    f"(racks={len(racks_in_row)}, leafs={len(leaf_devices)}, interfaces={len(leaf_interfaces)}) — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_ROW_LEAF_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)

        if not racks_in_row:
            self.logger.error(
                f"Rack {self.data.name}: No racks found in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )

        if not leaf_devices:
            self.logger.error(
                f"Rack {self.data.name}: No leaf devices found in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )

        device_names = [dev.name.value for dev in leaf_devices]

        if not leaf_interfaces:
            self.logger.error(
                f"Rack {self.data.name}: No downlink interfaces found on leaf devices in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
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
        """Resolve the leaf devices/interfaces a rack's l2-leaf or access-leaf batch
        cables to: the local leaf pair created earlier this run (network rack), or
        the middle-rack leafs in the same row (compute rack, mixed deployment).
        Returns None if unresolved (already logged) — caller should ``continue``.
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
            leaf_interfaces = sorted({iface.name.value for iface in leaf_interfaces_objects})
            return created_leaf_devices, leaf_interfaces, 0, "intra_rack_middle"

        # Compute rack (mixed deployment): cable to middle-rack leafs in same row
        rack_base = self.data.pod.rack_numbering_base_offset
        leaf_base = self.data.pod.leaf_link_base_offset
        effective_rack_index = max(0, self.data.index - 1 - rack_base)
        cabling_offset = leaf_base + (effective_rack_index * devices_per_rack)

        if leaf_row_cache is None:
            leaf_row_cache = await self._get_leaf_devices_in_row(pod_id=self.data.pod.id, row_index=self.data.row_index)
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
    ) -> int:
        """Thin wrapper around helper function for readability and compatibility."""
        return self._plan.calculate_cabling_offsets(
            data=self.data,
            logger=self.logger,
            device_count=device_count,
            device_type=device_type,
            racks_in_previous_rows=racks_in_previous_rows,
        )

    def _planned_previous_row_rack_slots(self) -> int:
        """Deterministic count of row-dependent rack slots before this row.

        Uses pod layout capacity rather than live object count so offsets remain
        stable even when racks are created incrementally or in parallel.
        """
        pod = self.data.pod
        if self.data.row_index <= 1:
            return 0
        profile = pod.profile
        if profile.deployment_type not in ("tor", "mixed"):
            return 0

        # In both tor and mixed modes, compute racks are the row-dependent slots.
        racks_per_row = max(0, profile.row_dependent_rack_slots_per_row)
        return (self.data.row_index - 1) * racks_per_row

    def _validate_profile_capacity_limits(self) -> list[str]:
        """Return profile-capacity violations for this rack context.

        Profile maxima define allowed topology shape. Offsets are only valid
        when the rack/request stays within those limits.
        """
        pod = self.data.pod
        profile = pod.profile
        errors: list[str] = []

        if self.data.row_index > profile.rows:
            errors.append(
                f"row_index={self.data.row_index} exceeds profile rows={profile.rows} "
                f"for layout={profile.profile_template}"
            )

        max_rack_slots = profile.compute_racks_per_row + profile.network_racks_per_row
        if max_rack_slots > 0 and self.data.index > max_rack_slots:
            errors.append(
                f"rack index={self.data.index} exceeds per-row rack slots={max_rack_slots} "
                f"(compute={profile.compute_racks_per_row}, network={profile.network_racks_per_row})"
            )

        if pod.deployment_type in ("middle_rack", "mixed") and self.data.rack_type == "network":
            leaf_count = sum(role.quantity for role in self.data.leafs)
            if leaf_count > profile.max_leafs_per_network_rack:
                errors.append(
                    f"leaf count={leaf_count} exceeds profile max_leafs_per_network_rack="
                    f"{profile.max_leafs_per_network_rack}"
                )

        if pod.deployment_type in ("tor", "mixed") and self.data.rack_type in ROW_DEPENDENT_RACK_TYPES:
            tor_count = sum(role.quantity for role in self.data.tors)
            if tor_count > profile.max_tors_per_compute_rack:
                errors.append(
                    f"tor count={tor_count} exceeds profile max_tors_per_compute_rack="
                    f"{profile.max_tors_per_compute_rack}"
                )

        return errors

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

        if self.data.pod.parent.is_managed_by_controller:
            self.logger.info(
                f"Rack {self.data.name}: parent DC management_mode=managed_by_controller — skipping generator"
            )
            return

        # Wait for an in-flight add_pod/pod_rack_cascade on our pod before reading
        # pod-level data (spine devices, ASN/loopback pools) — avoid partial data.
        for parent_generator in ("add_pod", "pod_rack_cascade"):
            refreshed = await self.wait_for_parent_generator_and_refetch(parent_generator, self.data.pod.id)
            if refreshed is not None:
                data = refreshed
                try:
                    self.data = parse_rack_data(data)
                except (ValueError, KeyError, IndexError) as exc:
                    self.logger.error(f"Generation failed due to {exc}")
                    return

        pod = self.data.pod
        deployment_type = pod.deployment_type

        # A row-dependent (tor/compute) rack cables to its row's network rack's
        # leafs. If triggered independently while that rack's add_rack is still
        # running, wait for it rather than cabling against leafs that don't exist yet.
        if deployment_type == "mixed" and self.data.rack_type in ROW_DEPENDENT_RACK_TYPES:
            network_racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[pod.id],
                row_index__value=self.data.row_index,
                rack_type__value="network",
            )
            for network_rack in network_racks:
                refreshed = await self.wait_for_parent_generator_and_refetch("add_rack", network_rack.id)
                if refreshed is not None:
                    data = refreshed
                    try:
                        self.data = parse_rack_data(data)
                    except (ValueError, KeyError, IndexError) as exc:
                        self.logger.error(f"Generation failed due to {exc}")
                        return
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

        role_errors = self._role_compatibility_errors(deployment_type)
        if role_errors:
            for error in role_errors:
                self.logger.error(
                    f"Rack {self.data.name}: {error} — fix the rack's fabric_templates instead of running the generator."
                )
            return

        capacity_errors = self._validate_profile_capacity_limits()
        if capacity_errors:
            for error in capacity_errors:
                self.logger.error(
                    f"Rack {self.data.name}: {error} — profile limits are exceeded, adjust layout/templates first."
                )
            return

        self.logger.info(f"Generating topology for rack {self.data.name}")

        await self._prepare_generation_context()

        # Names created THIS run, to skip duplicate templates within this invocation.
        # Do NOT pre-populate with existing devices — every object must be re-upserted
        # each run or the generator's group-context cleanup deletes it as "unused".
        self._created_device_names: set[str] = set()
        self._leaf_row_cache: tuple[list[str], list[str]] | None = None
        created_leaf_devices: list[str] = []

        await self._generate_leafs(created_leaf_devices)
        await self._generate_tors()
        await self._generate_l2_leafs(created_leaf_devices)
        await self._generate_access_leafs(created_leaf_devices)

        # Generation completion summary
        total_devices = len(created_leaf_devices) + sum(tor_role.quantity for tor_role in self.data.tors)
        self.logger.info(
            f"Rack generation completed: {self.data.name} - {total_devices} device(s) created with connectivity"
        )

        await self._fan_out_to_row_dependent_racks()

    async def _ensure_mlag_pairs(
        self, device_names: list[str], *, role_label: str, template: Template, supports_virtual: bool = True
    ) -> None:
        """Pair same-role devices two-at-a-time (sorted, odd one unpaired) into MLAG
        domains, per pod.mlag_create ("no" / "back-to-back" / "virtual").

        back-to-back needs a role=mlag-peer interface on the template — the mlag
        generator (triggered on ManagedMLAG created/updated) does the actual wiring.
        virtual anchors on a loopback (mlag.py's _ensure_virtual_peer_link) — only
        L3/routed roles (leaf, access-leaf) can use it. supports_virtual=False
        (l2-leaf: no loopback, L2-only by design) forces back-to-back instead of
        erroring out — mlag_create is one pod-wide setting shared by every role,
        so a pod with both leaf (L3) and l2-leaf (L2-only) roles must fall back
        for the L2-only ones regardless of what's configured for the pod.
        """
        mlag_create = self.data.pod.mlag_create
        if mlag_create == "no" or len(device_names) < 2:
            return

        if mlag_create == "virtual" and not supports_virtual:
            self.logger.info(
                f"Rack {self.data.name}: {role_label} has no routing/loopback — "
                f"using back-to-back MLAG instead of the pod's virtual setting (L2-only role)."
            )
            mlag_create = "back-to-back"

        if mlag_create == "back-to-back" and not self._roles.template_interfaces(template, role="mlag-peer"):
            self.logger.error(
                f"Rack {self.data.name}: template {template.id} has no mlag-peer interface — "
                f"cannot create back-to-back MLAG for {role_label}s."
            )
            return

        sorted_names = sorted(device_names)
        mlag_group = await self.client.get(kind=CoreStandardGroup, name__value="mlag_domains")
        for pair_index, (first, second) in enumerate(zip(sorted_names[0::2], sorted_names[1::2]), start=1):
            mlag_name = f"{first}-{second}-mlag"
            existing = await self.client.filters(kind=ManagedMLAG, name__value=mlag_name)
            if existing:
                existing_mlag = existing[0]
                self.client.group_context.related_node_ids.append(existing_mlag.id)
                # mlag_create may have changed since this domain was created (e.g.
                # back-to-back <-> virtual) — mlag.py's peer-link wiring branches on
                # this flag, so it must reflect the current setting, not the one at
                # creation time, or a re-run would silently keep wiring the old mode.
                wants_virtual = mlag_create == "virtual"
                if existing_mlag.virtual_peer_link.value != wants_virtual:
                    existing_mlag.virtual_peer_link.value = wants_virtual
                    await existing_mlag.save(allow_upsert=True)
                    self.logger.info(
                        f"Rack {self.data.name}: updated MLAG domain {mlag_name} to {mlag_create} peer-link"
                    )
                continue

            devices = await self.client.filters(kind=DcimPhysicalDevice, name__values=[first, second])
            if len(devices) != 2:
                self.logger.error(f"MLAG pair {first}/{second}: could not resolve both devices.")
                continue

            mlag_obj = await self.client.create(
                kind=ManagedMLAG,
                data={
                    "name": mlag_name,
                    "domain_id": pair_index,
                    "virtual_peer_link": mlag_create == "virtual",
                    "status": "active",
                    "capabilities": [{"id": dev.id} for dev in devices],
                    "member_of_groups": [{"id": mlag_group.id}],
                },
            )
            await mlag_obj.save(allow_upsert=True)
            self.logger.info(
                f"Rack {self.data.name}: created MLAG domain {mlag_name} ({mlag_create}) for {role_label}s"
            )

    async def _create_devices_for_role(
        self,
        role: DeviceRole,
        *,
        device_role: str,
        deployment_id: str,
        allocate_loopback: bool,
        group_name: str | None = None,
    ) -> list[str]:
        """create_devices() + _created_device_names bookkeeping shared by every
        role generator method (leaf/tor/border-leaf/firewall/load-balancer)."""
        devices = await self.create_devices(
            deployment_id=deployment_id,
            device_role=device_role,
            quantity=role.quantity,
            template=role.template.model_dump(),
            naming_convention=self._naming_conv,
            options=self._roles.build_device_options(allocate_loopback=allocate_loopback, group_name=group_name),
        )
        self._created_device_names.update(devices)
        return devices

    async def _generate_spine_attached_role(
        self,
        role: DeviceRole,
        *,
        device_role: Literal["leaf", "tor", "border-leaf"],
        deployment_id: str,
        bottom_interfaces: list[str],
        offset: int,
        mlag: bool,
    ) -> list[str]:
        """Shared executor for every role that dual-homes to the pod's spines:
        create devices, optionally MLAG-pair them, cable+route against
        self._spine_device_names at the given offset. Offset formula and
        bottom_interfaces selection differ per role (leaf/tor/border-leaf each
        compute them differently — see their own methods) and are passed in
        rather than recomputed here.
        """
        pod = self.data.pod
        devices = await self._create_devices_for_role(
            role, device_role=device_role, deployment_id=deployment_id, allocate_loopback=True
        )
        if mlag:
            await self._ensure_mlag_pairs(devices, role_label=device_role, template=role.template)

        await self._cable_and_route(
            bottom_devices=devices,
            bottom_interfaces=bottom_interfaces,
            top_devices=self._spine_device_names,
            top_interfaces=self._spine_interfaces,
            strategy="rack",
            offset=offset,
            bottom_role=device_role,
            top_role=self._spine_role,
            bottom_sorting=pod.leaf_interface_sorting_method,
            top_sorting=pod.spine_interface_sorting_method,
        )
        return devices

    async def _generate_leafs(self, created_leaf_devices: list[str]) -> None:
        """Create -> cable -> route leaf devices."""
        for leaf_role in self.data.leafs:
            expected_names = self._roles.expected_names(role="leaf", quantity=leaf_role.quantity)
            if expected_names <= self._created_device_names:
                self.logger.info(
                    f"Skipping duplicate leaf template (devices already created: {sorted(expected_names)})"
                )
                continue

            leaf_devices = await self._generate_spine_attached_role(
                leaf_role,
                device_role="leaf",
                deployment_id=self.data.pod.id,
                bottom_interfaces=self._roles.template_interfaces(leaf_role.template, role="uplink"),
                offset=self.calculate_cabling_offsets(device_count=leaf_role.quantity, device_type="leaf"),
                mlag=True,
            )
            created_leaf_devices.extend(leaf_devices)

    async def _generate_tors(self) -> None:
        """Create -> cable -> route ToR devices.

        L2-only aggregation switches below leafs use role "l2-leaf"/"access-leaf" instead.
        """
        pod = self.data.pod
        if not (tor_roles := self.data.tors):
            return

        if not self._spine_device_names:
            self.logger.error(f"Rack {self.data.name}: No spine devices found in pod - cannot cable ToRs to spines.")

        # Both invariant across tor_roles (not derived from any single role) —
        # computed once rather than once per role template.
        tors_per_rack = sum(r.quantity for r in tor_roles)
        prev_row_racks = self._planned_previous_row_rack_slots()

        for tor_role in tor_roles:
            expected_names = self._roles.expected_names(role="tor", quantity=tor_role.quantity)
            if expected_names <= self._created_device_names:
                self.logger.info(f"Skipping duplicate tor template (devices already created: {sorted(expected_names)})")
                continue

            await self._generate_spine_attached_role(
                tor_role,
                device_role="tor",
                deployment_id=pod.id,
                bottom_interfaces=self._roles.template_interfaces(tor_role.template, role="uplink"),
                offset=self.calculate_cabling_offsets(
                    device_count=tors_per_rack, device_type="tor", racks_in_previous_rows=prev_row_racks
                ),
                mlag=True,
            )

    async def _create_local_leaf_role_devices(
        self,
        role: DeviceRole,
        *,
        device_role: Literal["l2-leaf", "access-leaf"],
        allocate_loopback: bool,
        created_leaf_devices: list[str],
    ) -> (
        tuple[
            list[str],
            list[str],
            tuple[list[str], list[str], int, Literal["intra_rack_middle", "intra_rack_mixed"]],
        ]
        | None
    ):
        """Create + MLAG-pair one l2-leaf/access-leaf role entry, and resolve its
        local-leaf cabling target — shared by _generate_l2_leafs/_generate_access_leafs.
        Returns None if unresolved (already logged) — caller should ``continue``.
        """
        devices = await self._create_devices_for_role(
            role, device_role=device_role, deployment_id=self.data.pod.id, allocate_loopback=allocate_loopback
        )
        # allocate_loopback doubles as "participates in routing/EVPN" here:
        # l2-leaf (False) is L2-only by design, access-leaf (True) is a routed VTEP.
        await self._ensure_mlag_pairs(
            devices, role_label=device_role, template=role.template, supports_virtual=allocate_loopback
        )
        interfaces = self._roles.template_interfaces(role.template, role="uplink")

        target = await self._resolve_local_leaf_cabling_target(
            created_leaf_devices=created_leaf_devices,
            leaf_row_cache=self._leaf_row_cache,
            devices_per_rack=len(devices),
            role_label=device_role,
        )
        if target is None:
            return None
        leaf_device_names, leaf_interfaces, _, _ = target
        if not created_leaf_devices:
            self._leaf_row_cache = (leaf_device_names, leaf_interfaces)

        return devices, interfaces, target

    async def _generate_l2_leafs(self, created_leaf_devices: list[str]) -> None:
        """Create l2-leaf devices and cable them to local leafs. No routing - L2-only."""
        for l2_leaf_role in self.data.l2_leafs:
            result = await self._create_local_leaf_role_devices(
                l2_leaf_role,
                device_role="l2-leaf",
                allocate_loopback=False,
                created_leaf_devices=created_leaf_devices,
            )
            if result is None:
                continue
            devices, interfaces, (leaf_device_names, leaf_interfaces, cabling_offset, strategy) = result

            await self.create_cabling(
                bottom_devices=devices,
                bottom_interfaces=interfaces,
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
        for access_leaf_role in self.data.access_leafs:
            result = await self._create_local_leaf_role_devices(
                access_leaf_role,
                device_role="access-leaf",
                allocate_loopback=True,
                created_leaf_devices=created_leaf_devices,
            )
            if result is None:
                continue
            devices, interfaces, (leaf_device_names, leaf_interfaces, cabling_offset, strategy) = result

            await self._cable_and_route(
                bottom_devices=devices,
                bottom_interfaces=interfaces,
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
                    bottom_devices=devices,
                    top_devices=self._spine_device_names,
                    options=overlay_only_options,
                    p2p_interfaces=[],
                    bottom_role="access-leaf",
                    top_role=self._spine_role,
                )
