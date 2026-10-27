from __future__ import annotations

from typing import Any, Literal, cast

from typing_extensions import TypedDict

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..helpers.rack import RackRolesHelper, parse_rack_data, pod_row_racks
from ..helpers.template_interfaces import template_interface_names_by_role
from ..pod_config import pod_deployment_type, pod_profile, spine_downlink_layout
from ..pools import PoolMixin
from ..rack import (
    MUTUALLY_EXCLUSIVE_ROLE_GROUPS,
    POD_GENERATORS,
    ROLES_BY_DEPLOYMENT_TYPE,
    ROW_DEPENDENT_RACK_TYPES,
    RackMixin,
    spine_bgp_readiness_role,
)
from ..routing import RoutingMixin
from ..types import CablingOptions


class TopologyRackParentData(TypedDict, total=False):
    """Shape of the nested TopologyDataCenter projection under
    TopologyPod.parent (see queries/topology/add/rack.gql's PodFields
    fragment) — declared here, at the read site, instead of a shared
    Pydantic model file."""

    id: str
    name: str
    index: int
    naming_convention: str
    fabric_interface_sorting_method: str
    connectivity_mode: str
    routing_strategy: str
    underlay_protocol: str
    management_pool: dict[str, Any] | None
    fabric_asn_pool: dict[str, Any] | None
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


class TopologyRackPodRackData(TypedDict):
    """One rack of the pod (``racks`` of the PodFields fragment in rack.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str


class TopologyRackPodData(TypedDict, total=False):
    """Shape of the nested TopologyPod projection under LocationRack.deployment (see
    queries/topology/add/rack.gql's PodFields fragment)."""

    id: str
    name: str
    index: int
    design: dict[str, Any]
    parent: TopologyRackParentData
    fabric_templates: list[dict[str, Any]]
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    mlag_create: Literal["no", "back-to-back", "virtual"]
    loopback_pool: dict[str, Any] | None
    prefix_pool: dict[str, Any] | None
    racks: list[TopologyRackPodRackData]


class TopologyRackParentSuiteData(TypedDict, total=False):
    """Shape of the nested LocationSuite projection under LocationRack.parent
    (see queries/topology/add/rack.gql)."""

    id: str | None
    index: int
    name: str | None
    shortname: str | None
    suite_name: str | None


class TopologyRackData(TypedDict, total=False):
    """Shape of one clean_data()-processed LocationRack entry (see
    queries/topology/add/rack.gql) — declared here, at the read site,
    instead of a shared Pydantic model file. No runtime validation: a
    missing/mistyped key surfaces as ``KeyError``/``AttributeError`` at
    first read, caught by generate()'s own except clause below."""

    id: str
    name: str
    index: int
    rack_type: str
    row_index: int
    parent: TopologyRackParentSuiteData
    leafs: list[dict[str, Any]]
    tors: list[dict[str, Any]]
    l2_leafs: list[dict[str, Any]]
    access_leafs: list[dict[str, Any]]
    deployment: TopologyRackPodData


class RackGenerator(RackMixin, PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    data: TopologyRackData

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

    def _has_role_templates(self, roles: list[dict[str, Any]]) -> bool:
        """Return True when at least one role template has positive quantity."""
        return any(role.get("quantity", 0) > 0 for role in roles)

    def _present_roles(self) -> set[str]:
        """Role names with at least one positive-quantity fabric_templates entry on this rack."""
        role_templates = {
            "leaf": self.data.get("leafs", []),
            "tor": self.data.get("tors", []),
            "l2_leaf": self.data.get("l2_leafs", []),
            "access_leaf": self.data.get("access_leafs", []),
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

    def calculate_cabling_offsets(
        self,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Calculate cabling offset using simple formula based on rack position.

        Entirely derived from live rack position (row_index/index) and the
        caller-supplied count of sibling racks already deployed in previous
        rows of this pod — never from a design's declared capacity. A design
        cap describes what's ALLOWED, not what's actually built, and sizing
        offsets off it either wastes spine ports (cap larger than reality)
        or overflows them (cap smaller, e.g. a partially-built pod using
        fewer racks per row than its design allows)."""
        data = self.data
        current_index = data["index"]
        pod = data["deployment"]
        deployment_type = pod_deployment_type(pod["design"])
        effective_rack_index = max(0, current_index - 1)

        # For middle_rack deployment ToRs: always offset=0 (ToRs connect to leafs in same rack)
        if deployment_type == "middle_rack" and device_type == "tor":
            offset = 0
            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {data['name']} (mode=middle_rack) - intra-rack cabling"
            )

        # For mixed/middle_rack deployment leafs: calculate offset based on row position
        # Middle rack leafs serve all ToRs in their row
        elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            offset = (data["row_index"] - 1) * device_count

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {data['name']} "
                f"(row_index={data['row_index']}, leafs_per_rack={device_count}, mode={deployment_type})"
            )

        # For mixed/tor deployment ToRs: cumulative offset across the pod's ToR
        # block on the spines (spine_downlink_layout: from port 0 in a tor pod,
        # past the leafs and border-leafs in a mixed one), plus the rack slots
        # of previous rows the caller passes in.
        elif deployment_type in ("mixed", "tor") and device_type == "tor":
            tor_block_start = spine_downlink_layout(pod["design"])["tor"]
            tors_in_previous_rows = (racks_in_previous_rows or 0) * device_count
            offset_in_current_row = device_count * effective_rack_index
            offset = tor_block_start + tors_in_previous_rows + offset_in_current_row

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {data['name']} "
                f"(row={data['row_index']}, index={current_index}, tors_in_rack={device_count}, "
                f"tor_block_start={tor_block_start}, tors_in_previous_rows={tors_in_previous_rows}, "
                f"mode={deployment_type})"
            )

        else:
            # Other cases: no offset needed
            offset = 0
            self.logger.info(f"No offset needed for {device_type} in rack {data['name']} (mode={deployment_type})")

        return offset

    def _pod_profile(self) -> dict[str, Any]:
        """This rack's pod's rack grid, from the pod's design."""
        return pod_profile(self.data["deployment"]["design"])

    def _planned_previous_row_rack_slots(self) -> int:
        """Deterministic count of row-dependent rack slots before this row.

        Uses pod design capacity rather than live object count so offsets remain
        stable even when racks are created incrementally or in parallel.
        """
        if self.data["row_index"] <= 1:
            return 0
        profile = self._pod_profile()
        if profile["deployment_type"] not in ("tor", "mixed"):
            return 0

        # In both tor and mixed modes, compute racks are the row-dependent slots.
        racks_per_row = max(0, profile["row_dependent_rack_slots_per_row"])
        return (self.data["row_index"] - 1) * racks_per_row

    def _validate_profile_capacity_limits(self) -> list[str]:
        """Return profile-capacity violations for this rack context.

        Profile maxima define allowed topology shape. Offsets are only valid
        when the rack/request stays within those limits.
        """
        profile = self._pod_profile()
        errors: list[str] = []

        if self.data["row_index"] > profile["max_rows"]:
            errors.append(
                f"row_index={self.data['row_index']} exceeds profile rows={profile['max_rows']} "
                f"for design={profile['profile_template']}"
            )

        max_rack_slots = profile["max_compute_racks_per_row"] + profile["max_network_racks_per_row"]
        if max_rack_slots > 0 and self.data["index"] > max_rack_slots:
            errors.append(
                f"rack index={self.data['index']} exceeds per-row rack slots={max_rack_slots} "
                f"(compute={profile['max_compute_racks_per_row']}, network={profile['max_network_racks_per_row']})"
            )

        if profile["deployment_type"] in ("middle_rack", "mixed") and self.data["rack_type"] == "network":
            leaf_count = sum(role["quantity"] for role in self.data.get("leafs", []))
            if leaf_count > profile["max_leafs_per_network_rack"]:
                errors.append(
                    f"leaf count={leaf_count} exceeds profile max_leafs_per_network_rack="
                    f"{profile['max_leafs_per_network_rack']}"
                )

        if profile["deployment_type"] in ("tor", "mixed") and self.data["rack_type"] in ROW_DEPENDENT_RACK_TYPES:
            tor_count = sum(role["quantity"] for role in self.data.get("tors", []))
            if tor_count > profile["max_tors_per_compute_rack"]:
                errors.append(
                    f"tor count={tor_count} exceeds profile max_tors_per_compute_rack="
                    f"{profile['max_tors_per_compute_rack']}"
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

    def _load_rack_data(self, raw: dict) -> bool:
        """Parse a GraphQL response into self.data; False (logged) when it is malformed."""
        try:
            self.data = cast(TopologyRackData, parse_rack_data(raw))
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return False
        return True

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        if not data:
            self.logger.error("Generator received empty data")
            return
        if not self._load_rack_data(data):
            return

        shape = "direct node data" if "name" in data and isinstance(data.get("name"), dict) else "query result"
        self.logger.info(f"Processing {shape}")

        # LocationRack.deployment accepts any TopologyRackHosting peer, not just TopologyPod
        # (e.g. a colocation rack's pod is a TopologyColocationZone). Those don't carry
        # DC-fabric fields like design — nothing for this generator to build.
        # Checked before anything reads pod["id"]: rack.gql's PodFields fragment is on
        # TopologyPod, so a colocation zone comes back as an empty pod dict and every
        # later access raises. Every LocationRack reaches this generator, since
        # trigger-rack-generator-on-created (data/events/99_actions.yml) fires on the
        # node kind and cannot filter on which topology the rack hangs off.
        pod = self.data.get("deployment") or {}
        if "design" not in pod:
            self.logger.info(
                "Rack %s: parent %s is not a DC-fabric pod, nothing to generate",
                self.data["name"],
                pod.get("id"),
            )
            return

        # Wait for an in-flight add_pod/pod_rack_cascade on our pod before reading
        # pod-level data (spine devices, ASN/loopback pools) — avoid partial data.
        refreshed = await self.wait_for_parent_generator_and_refetch(POD_GENERATORS, pod["id"])
        if refreshed is not None:
            if not self._load_rack_data(refreshed):
                return

        pod = self.data["deployment"]
        spine_names = await self._existing_spine_slot_names()
        spine_bgp_role = spine_bgp_readiness_role(pod.get("parent") or {})
        if spine_names and not await self.bgp_processes_ready(spine_names, spine_bgp_role):
            self.logger.info(
                "Rack %s: deferring bootstrap until parent POD spine %s BGP is ready",
                self.data["name"],
                spine_bgp_role,
            )
            return
        deployment_type = pod_deployment_type(pod["design"])

        # The grandparent DC's controllers (queries/topology/add/rack.gql's
        # PodFields fragment), read by create_devices().
        self.set_controllers_from(pod.get("parent", {}))

        # A row-dependent (tor/compute) rack's l2-leaf/access-leaf switches cable
        # to its row's network rack's leafs. If triggered independently while that rack's add_rack is still
        # running, wait for it rather than cabling against leafs that don't exist yet.
        if deployment_type == "mixed" and self.data["rack_type"] in ROW_DEPENDENT_RACK_TYPES:
            for network_rack in pod_row_racks(pod, self.data["row_index"], {"network"}):
                refreshed = await self.wait_for_parent_generator_and_refetch("add_rack", network_rack["id"])
                if refreshed is not None:
                    if not self._load_rack_data(refreshed):
                        return
                    pod = self.data["deployment"]
                    deployment_type = pod_deployment_type(pod["design"])

        self.logger.info(
            f"Starting rack generation: {self.data['name']} "
            f"[type={self.data['rack_type']}, deployment={deployment_type}]"
        )

        # Endpoint-only racks (no switch templates) are handled by endpoint flows.
        # Skip rack generation here to avoid unnecessary checksum/dependency gating.
        if not self._present_roles():
            self.logger.info(
                f"Rack {self.data['name']} has no switch templates — skipping rack generator (endpoint-only rack)."
            )
            return

        role_errors = self._role_compatibility_errors(deployment_type)
        if role_errors:
            for error in role_errors:
                self.logger.error(
                    f"Rack {self.data['name']}: {error} — fix the rack's fabric_templates instead of running the generator."
                )
            return

        capacity_errors = self._validate_profile_capacity_limits()
        if capacity_errors:
            for error in capacity_errors:
                self.logger.error(
                    f"Rack {self.data['name']}: {error} — profile limits are exceeded, adjust design/templates first."
                )
            return

        self.logger.info(f"Generating topology for rack {self.data['name']}")

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
        total_devices = len(created_leaf_devices) + sum(tor_role["quantity"] for tor_role in self.data.get("tors", []))
        self.logger.info(
            f"Rack generation completed: {self.data['name']} - {total_devices} device(s) created with connectivity"
        )

        await self._fan_out_to_row_dependent_racks()

    async def _create_devices_for_role(
        self,
        role: dict[str, Any],
        *,
        device_role: str,
        deployment_id: str,
        allocate_loopback: bool,
        mlag: bool = False,
        mlag_supports_virtual: bool = True,
    ) -> list[str]:
        """create_devices() + _created_device_names bookkeeping shared by every
        role generator method (leaf/tor/border-leaf/firewall/load-balancer).

        mlag=True has create_devices() itself pair the created devices into
        MLAG domains, per the pod's mlag_create setting — see
        RackRolesHelper.build_device_options."""
        devices = await self.create_devices(
            deployment_id=deployment_id,
            device_role=device_role,
            quantity=role["quantity"],
            template=role["template"],
            naming_convention=self._naming_conv,
            options=self._roles.build_device_options(
                allocate_loopback=allocate_loopback,
                mlag=mlag,
                mlag_supports_virtual=mlag_supports_virtual,
            ),
        )
        self._created_device_names.update(devices)
        return devices

    async def _generate_spine_attached_role(
        self,
        role: dict[str, Any],
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
        pod = self.data["deployment"]
        devices = await self._create_devices_for_role(
            role, device_role=device_role, deployment_id=deployment_id, allocate_loopback=True, mlag=mlag
        )

        await self._cable_and_route(
            bottom_devices=devices,
            bottom_interfaces=bottom_interfaces,
            top_devices=self._spine_device_names,
            top_interfaces=self._spine_interfaces,
            strategy="rack",
            offset=offset,
            bottom_role=device_role,
            top_role=self._spine_role,
            bottom_sorting=cast(
                Literal["top_down", "bottom_up"], pod.get("leaf_interface_sorting_method", "bottom_up")
            ),
            top_sorting=cast(Literal["top_down", "bottom_up"], pod.get("spine_interface_sorting_method", "bottom_up")),
        )
        return devices

    async def _generate_leafs(self, created_leaf_devices: list[str]) -> None:
        """Create -> cable -> route leaf devices."""
        for leaf_role in self.data.get("leafs", []):
            expected_names = self._roles.expected_names(role="leaf", quantity=leaf_role["quantity"])
            if expected_names <= self._created_device_names:
                self.logger.info(
                    f"Skipping duplicate leaf template (devices already created: {sorted(expected_names)})"
                )
                continue

            leaf_devices = await self._generate_spine_attached_role(
                leaf_role,
                device_role="leaf",
                deployment_id=self.data["deployment"]["id"],
                bottom_interfaces=template_interface_names_by_role(
                    interfaces=leaf_role["template"].get("interfaces", []), role="uplink"
                ),
                offset=self.calculate_cabling_offsets(device_count=leaf_role["quantity"], device_type="leaf"),
                mlag=True,
            )
            created_leaf_devices.extend(leaf_devices)

    async def _generate_tors(self) -> None:
        """Create -> cable -> route ToR devices.

        L2-only aggregation switches below leafs use role "l2-leaf"/"access-leaf" instead.
        """
        pod = self.data["deployment"]
        if not (tor_roles := self.data.get("tors", [])):
            return

        # The "if not self._spine_device_names" guard that used to sit here is
        # gone: _derive_spine_info() now raises on every way of ending up with
        # no spine names, during _prepare_generation_context() and therefore
        # before any device exists. Guarding here only ever covered ToRs, and
        # only after _generate_leafs() had already run without a guard of its own.

        # Both invariant across tor_roles (not derived from any single role) —
        # computed once rather than once per role template.
        tors_per_rack = sum(r["quantity"] for r in tor_roles)
        prev_row_racks = self._planned_previous_row_rack_slots()

        for tor_role in tor_roles:
            expected_names = self._roles.expected_names(role="tor", quantity=tor_role["quantity"])
            if expected_names <= self._created_device_names:
                self.logger.info(f"Skipping duplicate tor template (devices already created: {sorted(expected_names)})")
                continue

            await self._generate_spine_attached_role(
                tor_role,
                device_role="tor",
                deployment_id=pod["id"],
                bottom_interfaces=template_interface_names_by_role(
                    interfaces=tor_role["template"].get("interfaces", []), role="uplink"
                ),
                offset=self.calculate_cabling_offsets(
                    device_count=tors_per_rack, device_type="tor", racks_in_previous_rows=prev_row_racks
                ),
                mlag=True,
            )

    async def _create_local_leaf_role_devices(
        self,
        role: dict[str, Any],
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
        # allocate_loopback doubles as "participates in routing/EVPN" here:
        # l2-leaf (False) is L2-only by design, access-leaf (True) is a routed VTEP.
        devices = await self._create_devices_for_role(
            role,
            device_role=device_role,
            deployment_id=self.data["deployment"]["id"],
            allocate_loopback=allocate_loopback,
            mlag=True,
            mlag_supports_virtual=allocate_loopback,
        )
        interfaces = template_interface_names_by_role(interfaces=role["template"].get("interfaces", []), role="uplink")

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
        for l2_leaf_role in self.data.get("l2_leafs", []):
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
        for access_leaf_role in self.data.get("access_leafs", []):
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
