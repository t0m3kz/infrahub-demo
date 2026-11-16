from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, cast

from generators.base import BaseGenerator
from generators.capabilities.bgp import (
    RoutingDevice,
    bgp_processes_ready,
    mlag_pairs_of,
    plan_routing,
    routing_devices,
)
from generators.capabilities.mlag import (
    MlagMember,
    mlag_domain_name,
    mlag_lock_key,
    peer_ports,
    plan_mlag_domains,
)
from generators.dc.rack_data import TopologyRackData
from generators.dc.rack_plan import MlagGroup, RackReads, RoutedLayer, RowLeafs, read_rack_reads
from generators.dc.rack_support import (
    EBGP_UNDERLAY_STRATEGIES,
    POD_GENERATORS,
    POD_POOL_FIELDS,
    ROW_DEPENDENT_RACK_TYPES,
    SPINE_DEPENDENT_ROLES,
    spine_bgp_readiness_role,
)
from generators.dc.routing_inputs import read_routing_inputs, routing_shared
from generators.dcim.cabling_plan import PortFacts, plan_cabling, raise_problems, template_port_facts
from generators.dcim.controllers import controllers_of, resolve_role_controller
from generators.dcim.device_plan import plan_physical_devices, role_group_name
from generators.dcim.template_interfaces import TemplatePort, template_interface_names_by_role
from generators.helpers.naming import naming_convention_of
from generators.helpers.pairing import pair_device_names
from generators.helpers.rack import RackRolesHelper, parse_rack_data, pod_row_racks
from generators.helpers.routing import RoutingOptions, loopback_prefix_length, p2p_prefix_length
from generators.model.pod_profile import (
    pod_deployment_type,
    pod_spine_slot_device_names,
    spine_slot_role,
    spine_slot_templates,
)
from generators.model.rack_model import RackFabricModel, role_compatibility_errors
from generators.plan import ExternalProducer, TopologyPlan
from generators.protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    LocationRack,
    TopologyDataCenter,
    TopologyPod,
)
from utils.limits import WAIT_FOR_PEER_GENERATOR


class RackGenerator(BaseGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    data: TopologyRackData

    @property
    def _spine_role(self) -> Literal["spine", "border-spine"]:
        """This pod's spine-slot role (set by _prepare_generation_context()); "spine" when unset."""
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

    def _model(self) -> RackFabricModel:
        """The rack's model, compiled from the current data (a refetch replaces self.data)."""
        return RackFabricModel.compile(self.data)

    def _present_roles(self) -> set[str]:
        """Role names with at least one positive-quantity fabric_templates entry on this rack."""
        return set(self._model().present_roles)

    def _role_compatibility_errors(self, deployment_type: str) -> list[str]:
        """Human-readable errors for fabric_templates roles this rack can't cable."""
        return role_compatibility_errors(self._present_roles(), deployment_type)

    def calculate_cabling_offsets(
        self,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Cabling offset from the rack's row/index position and the count of racks in previous rows; never from a
        design cap.
        """
        model = self._model()
        offset = model.cabling_offset(
            device_count=device_count, device_type=device_type, racks_in_previous_rows=racks_in_previous_rows
        )
        self.logger.info(
            f"Calculated {device_type} offset={offset} for rack {model.rack_name} "
            f"(row_index={model.row_index}, index={model.rack_index}, devices_in_rack={device_count}, "
            f"mode={model.deployment_type})"
        )
        return offset

    def _planned_previous_row_rack_slots(self) -> int:
        """Deterministic count of row-dependent rack slots before this row, from pod design capacity."""
        return self._model().planned_previous_row_rack_slots

    def _validate_profile_capacity_limits(self) -> list[str]:
        """Profile-capacity violations for this rack context (offsets are only valid within them)."""
        return list(self._model().capacity_errors)

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

        # A rack whose deployment is not a TopologyPod (e.g. a colocation zone) has no DC-fabric fields: nothing to
        # build. Checked before reading pod["id"]; every LocationRack reaches this generator (group trigger).
        pod = self.data.get("deployment") or {}
        if "design" not in pod:
            self.logger.info(
                "Rack %s: parent %s is not a DC-fabric pod, nothing to generate",
                self.data["name"],
                pod.get("id"),
            )
            return

        # Wait for an in-flight add_pod/pod_rack_cascade on our pod before reading pod-level data.
        refreshed = await self.wait_for_parent_generator_and_refetch(POD_GENERATORS, pod["id"])
        if refreshed is not None:
            if not self._load_rack_data(refreshed):
                return

        pod = self.data["deployment"]
        spine_names = await self._existing_spine_slot_names()
        spine_bgp_role = spine_bgp_readiness_role(pod.get("parent") or {})
        if spine_names and not await bgp_processes_ready(self.client, spine_names, spine_bgp_role):
            self.logger.info(
                "Rack %s: deferring bootstrap until parent POD spine %s BGP is ready",
                self.data["name"],
                spine_bgp_role,
            )
            return
        deployment_type = pod_deployment_type(pod["design"])

        # The grandparent DC's controllers (rack.gql RackPodFields), read by the device plan.
        self._controllers = controllers_of(pod.get("parent", {}))

        # A row-dependent rack's l2-leaf/access-leaf cables to its row's network rack's leafs: wait for that add_rack if
        # still running.
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

        # Endpoint-only racks (no switch templates) are handled by endpoint flows; skip here.
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

        # Names created this run, to skip duplicate templates. Not pre-populated: every object must be re-upserted each
        # run or group-context cleanup deletes it.
        self._created_device_names: set[str] = set()
        created_leaf_devices: list[str] = []
        self._layers: list[RoutedLayer] = []
        self._mlag_groups: list[MlagGroup] = []
        self._problems: list[str] = []
        self._role_of: dict[str, str] = {}
        self._leaf_ports: dict[str, list[TemplatePort]] = {}

        # Every device of the rack, its cables and its routing: one plan, written in dependency order. The spines'
        # ports are another generator's (add_pod): an external dependency the reconciler waits for. A link without ports
        # raises before anything is written.
        # The MLAG domains and their control addresses are found or created by name: the read, the plan and the write hold
        # their locks, so two overlapping runs never both create them.
        async with self.plan_locks(*(mlag_lock_key(name) for name in self._mlag_domain_names())):
            plan = self._plan_rack(await self._read_plan_inputs(), created_leaf_devices)
            await self.apply_plan(plan)

        total_devices = len(created_leaf_devices) + sum(tor_role["quantity"] for tor_role in self.data.get("tors", []))
        self.logger.info(
            f"Rack generation completed: {self.data['name']} - {total_devices} device(s) created with connectivity"
        )

        await self._fan_out_to_row_dependent_racks()

    def _plan_rack(self, reads: RackReads, created_leaf_devices: list[str]) -> TopologyPlan:
        """The plan of the rack: the leafs and ToRs with their cables to the pod's spines, the l2-leafs and access-leafs with
        theirs to the local (or the row's) leafs, and the routing of every cabled layer.
        """
        plan = TopologyPlan(root_id=self.data["id"], producers=self._producers())
        self._plan_leafs(plan, reads, created_leaf_devices)
        self._plan_tors(plan, reads)
        self._plan_local_leaf_roles(plan, reads, created_leaf_devices)
        self._plan_routing(plan, reads)
        self._plan_mlag(plan, reads)
        raise_problems(self._problems)
        return plan

    def _role_entries(self) -> list[tuple[str, dict[str, Any]]]:
        """The role entries of this rack that become devices, in the order they are planned."""
        return [
            *(("leaf", entry) for entry in self.data.get("leafs", [])),
            *(("tor", entry) for entry in self.data.get("tors", [])),
            *(("l2-leaf", entry) for entry in self.data.get("l2_leafs", [])),
            *(("access-leaf", entry) for entry in self.data.get("access_leafs", [])),
        ]

    def _routing_strategy(self) -> str | None:
        """The routing strategy of the rack's design, None when the rack has no routing design."""
        design = self._routing_options.get("design")
        return design.get("routing_strategy") if design else None

    def _producers(self) -> list[ExternalProducer]:
        """What the rack's plan references and its pod's generators create: its reads wait for them, then its reconciler."""
        return [ExternalProducer(POD_GENERATORS, self.data["deployment"]["id"])]

    async def _read_plan_inputs(self) -> RackReads:
        """Everything the rack's plan needs that exists before the run (``read_rack_reads``): none of it is written by this
        run. The leafs of the row a compute rack cables to are another rack's (waited for), the routing state is the
        spines' and those leafs'.
        """
        entries = self._role_entries()
        if not entries:
            return RackReads()
        spine_templates = spine_slot_templates(self.data["deployment"].get("fabric_templates", []))
        template_ids = {entry["template"]["id"] for _, entry in entries}
        if spine_templates and self._spine_device_names:
            template_ids.add(spine_templates[0]["template"]["id"])
        names = sorted(
            {
                name
                for role, entry in entries
                for name in self._roles.expected_names(role=role, quantity=entry["quantity"])
            }
        )
        # A compute rack (no leafs of its own) cables its l2-leafs and access-leafs to its row's leafs
        row_leafs = RowLeafs()
        if not self.data.get("leafs") and (self.data.get("l2_leafs") or self.data.get("access_leafs")):
            row_leafs = await self._read_row_leafs(self.data["deployment"]["id"], self.data["row_index"])
        existing = [*self._spine_device_names, *row_leafs.names]
        routing = None
        strategy = self._routing_strategy()
        if strategy and any(self.data.get(key) for key in ("leafs", "tors", "access_leafs")):
            routing = await read_routing_inputs(
                self,
                fabric_name=self.fabric_name,
                strategy=strategy,
                names=[*names, *existing],
                top=existing,
                producers=self._producers(),
            )
        return await read_rack_reads(
            self.client,
            rack_id=self.data["id"],
            device_names=names,
            group_names={role_group_name(role) for role, _ in entries},
            template_ids=template_ids,
            existing_names=existing,
            row_leafs=row_leafs,
            routing=routing,
            mlag_names=[name for _, names in self._mlag_groups_of_entries() for name in names],
            mlag_domains=self._mlag_domain_names(),
        )

    def _plan_spine_attached_role(
        self,
        plan: TopologyPlan,
        reads: RackReads,
        role: dict[str, Any],
        *,
        device_role: Literal["leaf", "tor", "border-leaf"],
        deployment_id: str,
        bottom_interfaces: list[str],
        offset: int,
        mlag: bool,
    ) -> list[str]:
        """Shared planner for roles that dual-home to the pod's spines: the devices (with their loopbacks), the cables to
        every spine at the given offset and, for the routing and the MLAG pairs that follow, the layer.
        """
        pod = self.data["deployment"]
        template = role["template"]
        options = self._roles.build_device_options(allocate_loopback=True, mlag=mlag)
        devices = plan_physical_devices(
            plan,
            reads=reads.devices,
            fabric_name=self.fabric_name,
            pod_name=self.pod_name or "",
            role=device_role,
            quantity=role["quantity"],
            deployment_id=deployment_id,
            template=template,
            template_ports=reads.template_ports.get(template["id"], []),
            template_owner_id=reads.template_owners.get(template["id"]),
            naming_convention=self._naming_conv,
            options=options,
            controller=resolve_role_controller(
                self._controllers, device_role=device_role, template=template, logger=self.logger
            ),
        )
        self._created_device_names.update(devices)
        self._role_of.update(dict.fromkeys(devices, device_role))
        if device_role == "leaf":
            self._leaf_ports.update(dict.fromkeys(devices, reads.template_ports.get(template["id"], [])))
        if (mlag_create := options.get("mlag_create", "no")) != "no":
            self._mlag_groups.append(MlagGroup(tuple(devices), device_role, template, mlag_create))

        spine_entries = spine_slot_templates(pod.get("fabric_templates", []))
        spine_ports = reads.template_ports.get(spine_entries[0]["template"]["id"], []) if spine_entries else []
        self._role_of.update(dict.fromkeys(self._spine_device_names, self._spine_role))
        known = len(self._problems)
        bottom_ports = template_port_facts(
            devices,
            bottom_interfaces,
            reads.template_ports.get(template["id"], []),
            label=device_role,
            problems=self._problems,
        )
        top_ports = template_port_facts(
            self._spine_device_names, self._spine_interfaces, spine_ports, label="spine", problems=self._problems
        )
        outcome = plan_cabling(
            plan,
            skip=len(self._problems) > known,
            bottom_ports=bottom_ports,
            top_ports=top_ports,
            strategy="rack",
            offset=offset,
            bottom_sorting=cast(
                Literal["top_down", "bottom_up"], pod.get("leaf_interface_sorting_method", "bottom_up")
            ),
            top_sorting=cast(Literal["top_down", "bottom_up"], pod.get("spine_interface_sorting_method", "bottom_up")),
            deployment_id=self.deployment_id,
            pool_id=self._technical_pool_id,
            p2p_prefix_length=self._p2p_prefix_length,
            positions=reads.positions,
        )
        self._problems.extend(outcome.problems)
        self._layers.append(
            RoutedLayer(
                tuple(devices), tuple(self._spine_device_names), device_role, self._spine_role, tuple(outcome.links)
            )
        )
        return devices

    def _plan_leafs(self, plan: TopologyPlan, reads: RackReads, created_leaf_devices: list[str]) -> None:
        """Plan the leaf devices and their cabling."""
        for leaf_role in self.data.get("leafs", []):
            expected_names = self._roles.expected_names(role="leaf", quantity=leaf_role["quantity"])
            if expected_names <= self._created_device_names:
                self.logger.info(
                    f"Skipping duplicate leaf template (devices already created: {sorted(expected_names)})"
                )
                continue

            leaf_devices = self._plan_spine_attached_role(
                plan,
                reads,
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

    def _plan_tors(self, plan: TopologyPlan, reads: RackReads) -> None:
        """Plan the ToR devices and their cabling (L2-only switches below leafs use l2-leaf/access-leaf)."""
        pod = self.data["deployment"]
        if not (tor_roles := self.data.get("tors", [])):
            return

        # No spine-names guard here: _derive_spine_info() raises before any device exists.

        # Invariant across tor_roles: computed once.
        tors_per_rack = sum(r["quantity"] for r in tor_roles)
        prev_row_racks = self._planned_previous_row_rack_slots()

        for tor_role in tor_roles:
            expected_names = self._roles.expected_names(role="tor", quantity=tor_role["quantity"])
            if expected_names <= self._created_device_names:
                self.logger.info(f"Skipping duplicate tor template (devices already created: {sorted(expected_names)})")
                continue

            self._plan_spine_attached_role(
                plan,
                reads,
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

    def _local_leaf_target(
        self, reads: RackReads, created_leaf_devices: list[str], devices_per_rack: int, role_label: str
    ) -> tuple[list[PortFacts], list[str], int, Literal["intra_rack_middle", "intra_rack_mixed"]] | None:
        """The leaf ports a rack's l2-leaf or access-leaf batch cables to, the leafs they belong to, the cabling offset and
        strategy: the local leaf pair (network rack) or the row's middle-rack leafs (compute rack). None when there is no
        port (a problem, raised with the others before anything is written).
        """
        if created_leaf_devices:
            ports = [
                PortFacts(device, port.name, port.interface_type)
                for device in created_leaf_devices
                for port in self._leaf_ports.get(device, [])
                if port.role == "downlink"
            ]
            if not ports:
                self._problems.append(f"No downlink interfaces on leafs — cannot cable {role_label}s.")
                return None
            return ports, created_leaf_devices, 0, "intra_rack_middle"

        # Compute rack (mixed deployment): cable to middle-rack leafs in the same row
        effective_rack_index = max(0, self.data["index"] - 1)
        if not reads.row_leafs.names:
            self._problems.append(f"No middle-rack leafs in row {self.data['row_index']} — cannot cable {role_label}s.")
            return None
        return (
            reads.row_leafs.downlinks,
            reads.row_leafs.names,
            effective_rack_index * devices_per_rack,
            "intra_rack_mixed",
        )

    def _plan_local_leaf_roles(self, plan: TopologyPlan, reads: RackReads, created_leaf_devices: list[str]) -> None:
        """The l2-leafs (L2-only: no loopback, no routing) and access-leafs (routed VTEPs: cabled to the local leaf pair, plus
        an overlay EVPN session to the spines), planned from the leafs of this plan or the row's leafs.
        """
        for role in self.data.get("l2_leafs", []):
            self._plan_local_leaf_role(
                plan, reads, role, created_leaf_devices, device_role="l2-leaf", allocate_loopback=False
            )
        for role in self.data.get("access_leafs", []):
            self._plan_local_leaf_role(
                plan, reads, role, created_leaf_devices, device_role="access-leaf", allocate_loopback=True
            )

    def _plan_local_leaf_role(
        self,
        plan: TopologyPlan,
        reads: RackReads,
        role: dict[str, Any],
        created_leaf_devices: list[str],
        *,
        device_role: Literal["l2-leaf", "access-leaf"],
        allocate_loopback: bool,
    ) -> None:
        """One l2-leaf/access-leaf role entry: its devices (MLAG-paired afterwards) and their cables to the leaf target."""
        expected_names = self._roles.expected_names(role=device_role, quantity=role["quantity"])
        if expected_names <= self._created_device_names:
            self.logger.info(
                f"Skipping duplicate {device_role} template (devices already created: {sorted(expected_names)})"
            )
            return
        template = role["template"]
        # allocate_loopback = participates in routing/EVPN: l2-leaf False (L2-only), access-leaf True (routed VTEP).
        options = self._roles.build_device_options(
            allocate_loopback=allocate_loopback, mlag=True, mlag_supports_virtual=allocate_loopback
        )
        devices = plan_physical_devices(
            plan,
            reads=reads.devices,
            fabric_name=self.fabric_name,
            pod_name=self.pod_name or "",
            role=device_role,
            quantity=role["quantity"],
            deployment_id=self.data["deployment"]["id"],
            template=template,
            template_ports=reads.template_ports.get(template["id"], []),
            template_owner_id=reads.template_owners.get(template["id"]),
            naming_convention=self._naming_conv,
            options=options,
            controller=resolve_role_controller(
                self._controllers, device_role=device_role, template=template, logger=self.logger
            ),
        )
        self._created_device_names.update(devices)
        self._role_of.update(dict.fromkeys(devices, device_role))
        if (mlag_create := options.get("mlag_create", "no")) != "no":
            self._mlag_groups.append(MlagGroup(tuple(devices), device_role, template, mlag_create, allocate_loopback))
        target = self._local_leaf_target(reads, created_leaf_devices, len(devices), device_role)
        if target is None:
            return
        top_ports, leaf_names, offset, strategy = target
        self._role_of.update(dict.fromkeys(leaf_names, "leaf"))
        # l2-leaf<->leaf links are 802.1Q trunks - no IP either end; an access-leaf's are routed
        pool_id = self._technical_pool_id if allocate_loopback else None
        known = len(self._problems)
        bottom_ports = template_port_facts(
            devices,
            template_interface_names_by_role(interfaces=template.get("interfaces", []), role="uplink"),
            reads.template_ports.get(template["id"], []),
            label=device_role,
            problems=self._problems,
        )
        outcome = plan_cabling(
            plan,
            skip=len(self._problems) > known,
            bottom_ports=bottom_ports,
            top_ports=top_ports,
            strategy=strategy,
            offset=offset,
            bottom_sorting="bottom_up",
            top_sorting="bottom_up",
            deployment_id=self.deployment_id,
            pool_id=pool_id,
            p2p_prefix_length=self._p2p_prefix_length,
            positions=reads.positions,
        )
        self._problems.extend(outcome.problems)
        if allocate_loopback:
            self._role_of.update(dict.fromkeys(self._spine_device_names, self._spine_role))
            self._layers.append(
                RoutedLayer(tuple(devices), tuple(leaf_names), "access-leaf", "leaf", tuple(outcome.links))
            )
            self._layers.append(
                RoutedLayer(
                    tuple(devices),
                    tuple(self._spine_device_names),
                    "access-leaf",
                    self._spine_role,
                    (),
                    overlay_only=True,
                )
            )

    def _plan_routing(self, plan: TopologyPlan, reads: RackReads) -> None:
        """The routing of every cabled layer, when the rack has a routing design: processes of the new devices, the peering
        of every link, the overlay peerings. An MLAG pair shares one underlay AS.
        """
        strategy = self._routing_strategy()
        if not strategy:
            if self._layers:
                self.logger.warning("No design or routing strategy provided")
            return
        if reads.routing is None or not self._layers:
            return
        inputs = reads.routing
        devices: dict[str, RoutingDevice] = routing_devices(
            plan, reads=inputs.reads, device_reads=reads.devices, roles=self._role_of
        )
        mlag_pairs = {**inputs.reads.mlag_pairs, **mlag_pairs_of([group.names for group in self._mlag_groups])}
        for layer in self._layers:
            plan_routing(
                plan,
                shared=routing_shared(
                    inputs.facts,
                    strategy=strategy,
                    asn_pool=self._routing_options.get("asn_pool"),
                    skip_underlay=layer.overlay_only,
                ),
                devices=devices,
                bottom_devices=list(layer.bottom_devices),
                top_devices=list(layer.top_devices),
                links=layer.links,
                reads=inputs.reads,
                mlag_pairs=mlag_pairs,
                logger=self.logger,
            )

    def _mlag_groups_of_entries(self) -> list[tuple[str, tuple[str, ...]]]:
        """The (role, device names) of each role entry that pairs into MLAG domains: every entry of two or more devices
        when the pod asks for MLAG. The names are those the plan gives the devices.
        """
        if self.data["deployment"].get("mlag_create", "no") == "no":
            return []
        return [
            (role, tuple(sorted(self._roles.expected_names(role=role, quantity=entry["quantity"]))))
            for role, entry in self._role_entries()
            if entry["quantity"] >= 2
        ]

    def _mlag_domain_names(self) -> list[str]:
        """The names of the MLAG domains this rack's devices pair into."""
        return sorted(
            {
                mlag_domain_name(first, second)
                for _, names in self._mlag_groups_of_entries()
                for first, second in pair_device_names(list(names))
            }
        )

    def _plan_mlag(self, plan: TopologyPlan, reads: RackReads) -> None:
        """The MLAG domain of every pair of the devices the plan creates, and its wiring (``plan_mlag_domains``)."""
        for group in self._mlag_groups:
            template = dict(group.template)
            ports = reads.template_ports.get(template["id"], [])
            platform = (template.get("platform") or {}).get("name") or ""
            plan_mlag_domains(
                plan,
                names=list(group.names),
                role=group.role,
                template=template,
                mlag_create=group.mlag_create,
                supports_virtual=group.supports_virtual,
                members={name: MlagMember(name, platform, peer_ports(name, reads.mlag, ports)) for name in group.names},
                cable_deployment_id=self.deployment_id,
                reads=reads.mlag,
                positions=reads.positions,
                logger=self.logger,
                problems=self._problems,
            )

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
        missing = [f"pod.{field}" for field in POD_POOL_FIELDS if not (pod.get(field) or {}).get("id")]
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
                pod_obj = await self.client.get(kind=TopologyPod, id=pod["id"], include=list(POD_POOL_FIELDS))
                for field in POD_POOL_FIELDS:
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
        # The pod and DC rows are filled in place when a pool is late (``_wait_for_pools``): plain dicts
        pod = cast(dict[str, Any], self.data["deployment"])
        dc = cast(dict[str, Any], pod["parent"])
        self.deployment_id = dc["id"]
        self.pod_name = pod["name"].lower()
        self.fabric_name = dc["name"].lower()

        needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in EBGP_UNDERLAY_STRATEGIES
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
            if spine_dependent := self._present_roles() & SPINE_DEPENDENT_ROLES:
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

    async def _read_row_leafs(self, pod_id: str, row_index: int) -> RowLeafs:
        """The leaf devices of the same row and their downlink ports, for the l2-leaf/access-leaf cables of a compute rack.
        Waits (explicitly): a row-dependent rack's trigger can fire before its row's network rack has created its leafs, so
        actual devices and ports are checked. A row without racks, leafs or downlink ports is an error.
        """
        # The pod projection has the racks; only a row whose network rack is missing from it (same load) is re-read per
        # attempt
        row_racks = pod_row_racks(self.data["deployment"], row_index)
        rack_ids = [rack["id"] for rack in row_racks]
        network_rack_known = any(rack["rack_type"] == "network" for rack in row_racks)
        leaf_devices: list[Any] = []
        leaf_interfaces: list[Any] = []
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
                    include=["device"],
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

        problems = []
        if not rack_ids:
            problems.append("No racks found in row")
        if not leaf_devices:
            problems.append("No leaf devices found in row")
        if not leaf_interfaces:
            problems.append("No downlink interfaces found on leaf devices in row")
        if problems:
            self.logger.error(
                f"Rack {self.data['name']}: {'; '.join(problems)} {row_index}. "
                "Cannot cable the compute rack's switches to the row leafs for mixed deployment."
            )
        names = sorted(dev.name.value for dev in leaf_devices)
        ports = [
            PortFacts(
                port.device.peer.name.value,
                port.name.value,
                getattr(getattr(port, "interface_type", None), "value", None),
            )
            for port in sorted(leaf_interfaces, key=lambda p: p.name.value)
        ]
        self.logger.info(
            f"Found {len(names)} leaf devices in row {row_index} with {len({p.port_name for p in ports})} unique downlink interfaces"
        )
        return RowLeafs(names=names, downlinks=ports)
