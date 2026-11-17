"""Infrastructure generator for pod topology creation."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from generators.base import BaseGenerator
from generators.capabilities.bgp import bgp_processes_ready, plan_routing, plan_shared_as, routing_devices
from generators.capabilities.ha import (
    DC_GENERATORS,
    ServiceReads,
    plan_fabric_services,
    read_service_reads,
    service_entries,
    service_lock_keys,
    service_names,
)
from generators.dc.pod_mesh import (
    SiblingSpines,
    mesh_offsets,
    mesh_slot_width,
    read_sibling_spines,
)
from generators.dc.routing_inputs import RoutingInputs, read_routing_inputs, routing_shared
from generators.dcim.cabling_plan import PortFacts, plan_cabling, raise_problems, template_port_facts
from generators.dcim.controllers import controllers_of, resolve_role_controller
from generators.dcim.device_plan import (
    DeviceOptions,
    DeviceReads,
    plan_physical_devices,
    planned_device_names,
    read_device_state,
    role_group_name,
)
from generators.dcim.rack_layout import read_device_positions
from generators.dcim.template_interfaces import TemplateReads, read_template_ports
from generators.helpers.cable_attributes import RackPosition
from generators.helpers.naming import DeviceNamingConfig, naming_convention_of
from generators.helpers.rack import expected_device_names
from generators.helpers.routing import RoutingStrategy, loopback_prefix_length, p2p_prefix_length
from generators.ipam.pools import (
    ADDRESS_POOL_KIND,
    PREFIX_POOL_KIND,
    fabric_pool_nodes,
    plan_pool_refs,
    pod_pool_attributes,
    pool_key,
    read_pool_ids,
    slot_pool_name,
)
from generators.model.errors import FabricModelError
from generators.model.pod_model import PodFabricModel
from generators.model.pod_profile import (
    spine_downlink_layout,
    templates_by_role,
)
from generators.plan import ExternalProducer, NodeRef, TopologyPlan
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface, RoutingAutonomousSystem, TopologyPod
from utils.data_cleaning import clean_data
from utils.limits import BORDER_LEAF_WAIT, WAIT_FOR_PEER_GENERATOR

# Key of the pod's shared spine AS in its plan
POD_SPINE_AS = "pod-spine-as"

# FW/LB "uplink" interfaces face border-spine's "firewall"/"load-balancer" ports (BORDER_ROLE_FOR_SERVICES).


class TopologyPodParentData(TypedDict, total=False):
    """Nested TopologyDataCenter projection under TopologyPod.parent."""

    id: str
    name: str
    index: int
    naming_convention: str
    fabric_interface_sorting_method: str
    connectivity_mode: str
    routing_strategy: str
    underlay_protocol: str
    fabric_templates: list[dict[str, Any]]
    design: dict[str, Any]
    children: list[dict[str, Any]]
    fabric_asn_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    devices: list[Any]
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


class TopologyPodData(TypedDict, total=False):
    """One clean_data()-processed TopologyPod entry; a missing key raises KeyError at first read."""

    id: str
    name: str
    index: int
    design: dict[str, Any]
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    fabric_templates: list[dict[str, Any]]
    parent: TopologyPodParentData
    loopback_pool: dict[str, Any] | None
    prefix_pool: dict[str, Any] | None


@dataclass(frozen=True)
class BorderLeafs:
    """The DC's border-leafs a pod hosts and their uplink ports, as they exist."""

    names: list[str] = field(default_factory=list)
    uplinks: list[PortFacts] = field(default_factory=list)


@dataclass(frozen=True)
class PodReads:
    """What exists before the run and the pod's plan needs; ``services``: the HA state, positions and ports of the pod's own
    firewalls and load balancers (None: the pod has none)."""

    devices: DeviceReads
    templates: TemplateReads
    pool_ids: Mapping[str, str]
    shared_as_id: str | None
    siblings: list[SiblingSpines]
    border_leafs: BorderLeafs
    routing: RoutingInputs
    positions: Mapping[str, RackPosition | None]
    services: ServiceReads | None


class PodTopologyGenerator(BaseGenerator):
    """Generate pod topology: resource pools and spine devices; never fans out to add_rack. Waits for an in-flight
    add_dc/dc_pod_cascade.
    """

    data: TopologyPodData

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure."""

        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod Deployment data found in GraphQL response")
                return

            # Wait for add_dc/dc_pod_cascade on the parent DC, then re-parse, rather than run on partial data.
            dc_id = deployment_list[0].get("parent", {}).get("id")
            if dc_id:
                refreshed = await self.wait_for_parent_generator_and_refetch(DC_GENERATORS, dc_id)
                if refreshed is not None:
                    deployment_list = clean_data(refreshed).get("TopologyPod", [])
                    if not deployment_list:
                        self.logger.error("No Pod Deployment data found in GraphQL response")
                        return

            self.data = cast(TopologyPodData, deployment_list[0])
            # Force-read every required field here so a malformed response raises KeyError inside the try.
            _ = self.data["id"]
            pod_name = self.data["name"]
            pod_index = self.data["index"]
            dc = self.data["parent"]
            dc_id = dc["id"]
            dc_name = dc["name"]

            # add_dc's task can finish before its writes are visible: poll the DC for what is still needed.
            needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in (
                RoutingStrategy.EBGP_EBGP.value,
                RoutingStrategy.EBGP_IBGP.value,
            )
            for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
                asn_pool_ready = not needs_asn_pool or dc.get("fabric_asn_pool")
                super_spines_expected = templates_by_role(dc.get("fabric_templates", []), "super-spine")
                super_spine_ready = not super_spines_expected or dc.get("devices")
                if asn_pool_ready and super_spine_ready:
                    break
                if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                    delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                    self.logger.info(
                        f"Pod {pod_name}: parent DC's fabric_asn_pool/super-spine devices not visible yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                    )
                    await asyncio.sleep(delay)
                    data = await self.collect_data()
                    deployment_list = clean_data(data).get("TopologyPod", [])
                    if not deployment_list:
                        self.logger.error("No Pod Deployment data found in GraphQL response")
                        return
                    self.data = cast(TopologyPodData, deployment_list[0])
                    dc = self.data["parent"]
            # This pod's and its DC's design: the envelope every check and pool size reads.
            design = self.data["design"]
            dc_design = dc["design"]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {pod_name}")

        # Parent DC controllers, read by the device plan.
        self._controllers = controllers_of(dc)

        self.deployment_id = dc_id
        self.pod_name = pod_name.lower()
        self.fabric_name = dc_name.lower()

        try:
            model = PodFabricModel.compile(self.data)
        except FabricModelError as exc:
            self.logger.error(str(exc))
            return
        deployment_type = model.deployment_type
        spine_role = model.spine_role
        siblings = list(model.siblings)

        super_spine_devices = [device["name"] for device in dc.get("devices", [])]
        dc_super_spine_entries = templates_by_role(dc.get("fabric_templates", []), "super-spine")
        super_spine_interfaces = (
            [iface["name"] for iface in dc_super_spine_entries[0]["template"].get("interfaces", [])]
            if dc_super_spine_entries
            else []
        )

        # Check every precondition before the first save: returning after a save leaves a partial tracked set.
        if super_spine_devices and not await self._wait_for_super_spine_overlay(super_spine_devices, dc_id, pod_name):
            return
        # No super-spine tier to cable to: the pods mesh back-to-back.
        mesh_cabling = not super_spine_devices or not super_spine_interfaces
        slot_width = 0
        if mesh_cabling:
            try:
                slot_width = mesh_slot_width(
                    design, dc_design, [sibling for sibling in siblings if sibling["index"] < pod_index]
                )
            except ValueError as exc:
                self.logger.error(f"Pod {pod_name}: {exc}")
                return

        # Pool sizes come from the pod's design envelope (fabric_sizing.py), not live counts.
        underlay_protocol = model.underlay_protocol
        pool_sizes = model.pools_to_allocate

        self.logger.info(
            f"Pod {pod_name} pool sizes: technical=/{pool_sizes['technical']}, "
            f"loopback=/{pool_sizes['loopback']} (p2p=/{p2p_prefix_length(underlay_protocol)}, "
            f"underlay={underlay_protocol}, deployment={deployment_type})"
        )

        # Reference the DC-level ASN pool (one per DC, created by the DC generator).
        dc_asn_pool_id: str | None = None
        dc_fabric_asn_pool = dc.get("fabric_asn_pool")
        if dc_fabric_asn_pool and dc_fabric_asn_pool.get("name"):
            dc_asn_pool_id = dc_fabric_asn_pool["id"]
            self.logger.info(f"Pod {pod_name}: using DC ASN pool '{dc_fabric_asn_pool['name']}'")

        spine_groups = self._spine_groups(model)
        # Per-pod FW/LB, only with spine_role border-spine
        entries = service_entries(self.data.get("fabric_templates", [])) if spine_role == "border-spine" else []
        names = self._service_names(entries)
        async with self.plan_locks(
            f"pool-alloc-pod-{model.pod_id}",
            f"shared-as-{self._spine_as_description()}",
            *service_lock_keys(entries, names),
        ):
            reads = await self.read_plan_inputs(
                model,
                dc=dc,
                spine_groups=spine_groups,
                super_spine_devices=super_spine_devices,
                mesh_cabling=mesh_cabling,
                slot_width=slot_width,
                entries=entries,
                service_device_names=names,
            )
            plan = self.build_plan(
                model,
                dc=dc,
                dc_asn_pool_id=dc_asn_pool_id,
                super_spine_devices=super_spine_devices,
                super_spine_interfaces=super_spine_interfaces,
                mesh_cabling=mesh_cabling,
                slot_width=slot_width,
                spine_groups=spine_groups,
                reads=reads,
                entries=entries,
            )
            await self.apply_plan(plan)

        # A pod added after its DC declared border-leaf/FW/LB templates needs an explicit dc_pod_cascade; not
        # auto-triggered here, it would race the DC bootstrap.

    def _pod_pool(self, slot: str) -> str:
        """The name of this pod's pool for ``slot`` (set from the pod's name by generate())."""
        return slot_pool_name(self.pod_name or "", slot)

    def _spine_as_description(self) -> str:
        """The description that keys the AS the pod's spines share."""
        return f"{self.pod_name} spine underlay ASN"

    def _spine_options(self, model: PodFabricModel, dc: Any, start: int) -> DeviceOptions:
        # Management pool ID from the DC parent
        options = DeviceOptions(
            indexes=[model.dc_index, model.pod_index],
            allocate_loopback=True,
            loopback_prefix_length=loopback_prefix_length(model.underlay_protocol),
            role_index_start=start,
        )
        if dc_management_pool := dc.get("management_pool"):
            options["management_pool"] = dc_management_pool["id"]
        return options

    def _spine_groups(self, model: PodFabricModel) -> list[tuple[dict[str, Any], list[str]]]:
        """The spine-slot entries with the names of their devices: ``role_index`` continues across entries so a second entry
        never re-creates names.
        """
        groups: list[tuple[dict[str, Any], list[str]]] = []
        planned = 0
        for entry in model.spine_entries:
            names = planned_device_names(
                fabric_name=self.fabric_name,
                pod_name=None,
                device_role=model.spine_role,
                quantity=entry["quantity"],
                naming_convention=model.naming_convention,
                options=self._spine_options(model, self.data["parent"], planned + 1),
            )
            groups.append((entry, names))
            planned += entry["quantity"]
        return groups

    @staticmethod
    def _producers(model: PodFabricModel) -> list[ExternalProducer]:
        """What the pod's plan references and the DC's generators create: its reads wait for them, then its reconciler."""
        return [ExternalProducer(DC_GENERATORS, model.dc_id)]

    async def read_plan_inputs(
        self,
        model: PodFabricModel,
        *,
        dc: Any,
        spine_groups: list[tuple[dict[str, Any], list[str]]],
        super_spine_devices: list[str],
        mesh_cabling: bool,
        slot_width: int,
        entries: list[tuple[str, dict[str, Any]]],
        service_device_names: list[list[str]],
    ) -> PodReads:
        """Everything the plan needs that exists before the run: the pod's earlier devices and pools, the template ports,
        the lower-index sibling pods' spines (the mesh) and the DC's border-leafs, and the routing state of every device
        the pod routes with. Waits for the siblings' spines and the border-leafs (another generator's output).
        """
        spines = [name for _, names in spine_groups for name in names]
        template_ids = {entry["template"]["id"] for entry, _ in spine_groups} | {
            entry["template"]["id"] for _, entry in entries
        }
        service_devices = [name for entry_names in service_device_names for name in entry_names]
        dc_super_spine_entries = templates_by_role(dc.get("fabric_templates", []), "super-spine")
        if dc_super_spine_entries:
            template_ids.add(dc_super_spine_entries[0]["template"]["id"])
        siblings: list[SiblingSpines] = []
        if mesh_cabling:
            lower = [s for s in model.siblings if s["index"] < model.pod_index]
            siblings = await read_sibling_spines(
                self,
                siblings=lower,
                dc=dc,
                fabric_name=self.fabric_name,
                spine_role=model.spine_role,
                slot_width=slot_width,
                pod_name=self.data["name"],
            )
        border_leafs = await self._read_border_leafs(dc)
        sibling_spines = [name for sibling in siblings for name in sibling.spines]
        names = [*spines, *super_spine_devices, *sibling_spines, *border_leafs.names]
        pool_names = {
            self._pod_pool("technical"): PREFIX_POOL_KIND,
            self._pod_pool("loopback"): ADDRESS_POOL_KIND,
        }
        devices = await read_device_state(
            self.client,
            [*spines, *service_devices],
            {role_group_name(model.spine_role), *(role_group_name(role) for role, _ in entries)},
        )
        templates = await read_template_ports(self.client, template_ids)
        pool_ids = await read_pool_ids(self.client, pool_names)
        shared_as = await self.client.filters(
            kind=RoutingAutonomousSystem, description__value=self._spine_as_description()
        )
        routing = await read_routing_inputs(
            self,
            fabric_name=self.fabric_name,
            strategy=dc.get("routing_strategy", RoutingStrategy.EBGP_EBGP.value),
            names=names,
            top=[*super_spine_devices, *sibling_spines],
            producers=self._producers(model),
        )
        positions = await read_device_positions(
            self.client, [*super_spine_devices, *sibling_spines, *border_leafs.names]
        )
        services = (
            await read_service_reads(self.client, entries=entries, names=service_device_names, border_names=spines)
            if entries
            else None
        )
        return PodReads(
            devices=devices,
            templates=templates,
            pool_ids=pool_ids,
            shared_as_id=shared_as[0].id if shared_as else None,
            siblings=siblings,
            border_leafs=border_leafs,
            routing=routing,
            positions=positions,
            services=services,
        )

    def _service_names(self, entries: list[tuple[str, dict[str, Any]]]) -> list[list[str]]:
        """The names of the devices of each of the pod's own service entries."""
        dc = self.data["parent"]
        return service_names(
            entries,
            fabric_name=self.fabric_name,
            naming_convention=naming_convention_of(dc),
            indexes=[dc["index"], self.data["index"]],
        )

    def build_plan(
        self,
        model: PodFabricModel,
        *,
        dc: Any,
        dc_asn_pool_id: str | None,
        super_spine_devices: list[str],
        super_spine_interfaces: list[str],
        mesh_cabling: bool,
        slot_width: int,
        spine_groups: list[tuple[dict[str, Any], list[str]]],
        reads: PodReads,
        entries: list[tuple[str, dict[str, Any]]],
    ) -> TopologyPlan:
        """The pod's own fabric: its pools (a slice each of the DC's pools, the loopback and technical ones attached to
        the pod), its spines (a border-spine is a DC-level fabric tier: it deploys to the DC even though created here),
        their BGP and uplinks to the super-spines or, without a super-spine tier, the mesh to the lower-index pods, and
        the cables and routing of the DC's border-leafs to the spines, and, with border-spines, its own firewalls and load balancers
        (their HA domains and their cables to the spines). The DC's generators create what the plan only
        references (the parent pools, the super-spine ports): the reconciler waits for them. A layer that cannot be
        cabled raises, naming every missing port, before anything is written.
        """
        plan = TopologyPlan(root_id=model.pod_id, producers=self._producers(model))
        problems: list[str] = []
        pools = fabric_pool_nodes(
            plan, strategy="pod", pools=model.pools_to_allocate, fabric_name=self.fabric_name, pod_name=self.pod_name
        )
        attributes = pod_pool_attributes(self.pod_name or "", model.pools_to_allocate)
        plan_pool_refs(
            plan,
            kind=TopologyPod,
            node_id=model.pod_id,
            refs={attribute: reads.pool_ids.get(name) or pools[name] for attribute, name in attributes.items()},
            current={attribute: (self.data.get(attribute) or {}).get("id") for attribute in attributes},
        )

        spines: list[str] = []
        spine_ports: dict[str, list[Any]] = {}
        technical = NodeRef(pool_key(self._pod_pool("technical")))
        for entry, _ in spine_groups:
            options = self._spine_options(model, dc, 1 + len(spines))
            options["loopback_pool"] = pools[self._pod_pool("loopback")]
            template = entry["template"]
            names = plan_physical_devices(
                plan,
                reads=reads.devices,
                fabric_name=self.fabric_name,
                pod_name=self.pod_name or "",
                role=model.spine_role,
                quantity=entry["quantity"],
                deployment_id=model.dc_id if model.spine_role == "border-spine" else model.pod_id,
                template=template,
                template_ports=reads.templates.ports.get(template["id"], []),
                template_owner_id=reads.templates.owners.get(template["id"]),
                naming_convention=model.naming_convention,
                options=options,
                controller=resolve_role_controller(
                    self._controllers, device_role=model.spine_role, template=template, logger=self.logger
                ),
            )
            spines.extend(names)
            for name in names:
                spine_ports[name] = reads.templates.ports.get(template["id"], [])

        strategy = dc.get("routing_strategy", RoutingStrategy.EBGP_EBGP.value)
        shared_as = self._plan_spine_as(plan, dc_asn_pool_id, strategy, reads.shared_as_id)
        spine_sorting = cast(
            Literal["top_down", "bottom_up"], self.data.get("spine_interface_sorting_method", "bottom_up")
        )
        p2p_length = p2p_prefix_length(model.underlay_protocol)
        sibling_spines = [name for sibling in reads.siblings for name in sibling.spines]
        roles = {
            **dict.fromkeys(spines, model.spine_role),
            **dict.fromkeys(super_spine_devices, "super-spine"),
            **dict.fromkeys(sibling_spines, model.spine_role),
            **dict.fromkeys(reads.border_leafs.names, "border-leaf"),
        }
        devices = routing_devices(plan, reads=reads.routing.reads, device_reads=reads.devices, roles=roles)

        def route(
            bottom: list[str],
            top: list[str],
            links: Any = (),
            *,
            shared_underlay_as: Any = None,
            skip_underlay: bool = False,
        ) -> None:
            plan_routing(
                plan,
                shared=routing_shared(
                    reads.routing.facts,
                    strategy=strategy,
                    asn_pool=dc_asn_pool_id,
                    shared_underlay_as=shared_underlay_as,
                    skip_underlay=skip_underlay,
                ),
                devices=devices,
                bottom_devices=bottom,
                top_devices=top,
                links=list(links),
                reads=reads.routing.reads,
                mlag_pairs=reads.routing.reads.mlag_pairs,
                logger=self.logger,
            )

        # Pre-seed the eBGP of the spines (one shared underlay ASN) so parallel rack generators find it (OSPF_IBGP overlay
        # BGP follows post-cable)
        if shared_as is not None:
            route(spines, super_spine_devices, shared_underlay_as=shared_as)
        if mesh_cabling:
            # No super-spine tier: pre-seed spine overlay BGP (skip_underlay) so rack routing calls find it, then mesh
            route(spines, [], skip_underlay=True)
            self._plan_mesh(plan, model, reads, spines, spine_ports, shared_as, slot_width, route, problems)
        else:
            # Every pod owns the widest pod design's spine count of super-spine ports.
            top_template = templates_by_role(dc.get("fabric_templates", []), "super-spine")
            top_ports = reads.templates.ports.get(top_template[0]["template"]["id"], []) if top_template else []
            known = len(problems)
            bottom_ports = [
                port
                for name in spines
                for port in template_port_facts(
                    [name], model.spine_uplink_interfaces, spine_ports[name], label=model.spine_role, problems=problems
                )
            ]
            top_ports = template_port_facts(
                super_spine_devices, super_spine_interfaces, top_ports, label="super-spine", problems=problems
            )
            outcome = plan_cabling(
                plan,
                skip=len(problems) > known,
                bottom_ports=bottom_ports,
                top_ports=top_ports,
                strategy="pod",
                offset=model.super_spine_offset,
                bottom_sorting=spine_sorting,
                top_sorting=cast(
                    Literal["top_down", "bottom_up"], dc.get("fabric_interface_sorting_method", "bottom_up")
                ),
                deployment_id=model.dc_id,
                pool_id=technical,
                p2p_prefix_length=p2p_length,
                positions=reads.positions,
            )
            problems.extend(outcome.problems)
            route(spines, super_spine_devices, outcome.links, shared_underlay_as=shared_as)
        self._plan_border_leaf_cabling(plan, model, dc, reads, spines, spine_ports, technical, route, problems)
        if entries and reads.services is not None:
            plan_fabric_services(
                plan,
                entries=entries,
                fabric_name=self.fabric_name,
                deployment_id=self.data["id"],
                cable_deployment_id=model.dc_id,
                naming_convention=naming_convention_of(dc),
                indexes=[dc["index"], self.data["index"]],
                connectivity_mode=cast(Literal["pbr", "inline"], dc.get("connectivity_mode", "pbr")),
                border_names=spines,
                border_ports=spine_ports,
                devices=reads.devices,
                templates=reads.templates,
                reads=reads.services,
                controllers=self._controllers,
                logger=self.logger,
                problems=problems,
            )
        raise_problems(problems)
        return plan

    def _plan_spine_as(
        self, plan: TopologyPlan, dc_asn_pool_id: str | None, strategy: str, existing_spine_as_id: str | None
    ) -> Any:
        """The one underlay AS the pod's spines share (any eBGP underlay with a DC ASN pool), found or drawn from the pool;
        None for the other strategies.
        """
        if not dc_asn_pool_id or strategy not in (RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value):
            return None
        shared_as = plan_shared_as(
            plan,
            description=self._spine_as_description(),
            asn_pool=dc_asn_pool_id,
            existing_id=existing_spine_as_id,
            key=POD_SPINE_AS,
        )
        return shared_as

    def _plan_mesh(
        self,
        plan: TopologyPlan,
        model: PodFabricModel,
        reads: PodReads,
        spines: list[str],
        spine_ports: dict[str, list[Any]],
        shared_as: Any,
        slot_width: int,
        route: Any,
        problems: list[str],
    ) -> None:
        """Cable this pod's spines to every existing lower-index sibling pod (only the higher-index pod of a pair writes
        it), a deterministic uplink slot of ``slot_width`` ports each, and route the pair.
        """
        pod_name = self.data["name"]
        uplinks = list(model.spine_uplink_interfaces)
        if not reads.siblings:
            self.logger.info(f"Pod {pod_name}: no lower-index sibling pods yet — nothing to mesh-cable")
            return
        if not uplinks:
            raise_problems(
                [
                    f"Pod {pod_name}: no {model.spine_role} uplink interfaces to mesh-cable to "
                    f"{len(reads.siblings)} lower-index sibling pod(s) — add uplink-role ports to the spine template"
                ]
            )
        for sibling in reads.siblings:
            slot_start, offset = mesh_offsets(model.pod_index, sibling.index, slot_width)
            local_uplinks = uplinks[slot_start : slot_start + len(sibling.spines)]
            if len(local_uplinks) < len(sibling.spines):
                problems.append(
                    f"Pod {pod_name}: insufficient local {model.spine_role} uplinks for inter-pod mesh. "
                    f"Needed {len(sibling.spines)} free uplink name(s) for sibling pod idx={sibling.index}, "
                    f"but only {len(local_uplinks)} remain. "
                    "Increase uplink-role interfaces in the spine template or reduce mesh fan-out."
                )
                continue
            self.logger.info(
                f"Pod {pod_name} (idx={model.pod_index}): cabling to sibling pod idx={sibling.index} "
                f"[offset={offset}, slot_start={slot_start}, local_uplinks={local_uplinks}]"
            )
            known = len(problems)
            bottom_ports = [
                port
                for name in spines
                for port in template_port_facts(
                    [name], local_uplinks, spine_ports[name], label=model.spine_role, problems=problems
                )
            ]
            outcome = plan_cabling(
                plan,
                skip=len(problems) > known,
                bottom_ports=bottom_ports,
                top_ports=list(sibling.uplinks),
                strategy="pod",
                offset=offset,
                bottom_sorting="bottom_up",
                top_sorting="bottom_up",
                deployment_id=model.dc_id,
                pool_id=NodeRef(pool_key(self._pod_pool("technical"))),
                p2p_prefix_length=p2p_prefix_length(model.underlay_protocol),
                positions=reads.positions,
            )
            problems.extend(outcome.problems)
            route(spines, list(sibling.spines), outcome.links, shared_underlay_as=shared_as)

    async def _read_border_leafs(self, dc: Any) -> BorderLeafs:
        """The DC's border-leafs this pod hosts (created by dc.py, deployed to the DC), by deterministic name, with their
        uplink ports; none when the pod hosts none. Waits a short while: they may be read before add_dc's write is
        visible.
        """
        max_border_leafs_per_fabric = dc["design"]["max_border_leafs_per_fabric"]
        if max_border_leafs_per_fabric <= 0:
            return BorderLeafs()
        naming = DeviceNamingConfig(strategy=naming_convention_of(dc, lower=False))
        candidate_names = expected_device_names(
            naming_config=naming,
            fabric_name=self.fabric_name,
            device_indexes=[dc["index"], self.data["index"]],
            role="border-leaf",
            quantity=max_border_leafs_per_fabric,
        )
        border_leafs: list[Any] = []
        for attempt in range(BORDER_LEAF_WAIT.attempts):
            border_leafs = await self.client.filters(
                kind=DcimPhysicalDevice, name__values=sorted(candidate_names), role__value="border-leaf"
            )
            if border_leafs or attempt == BORDER_LEAF_WAIT.attempts - 1:
                break
            await asyncio.sleep(self._retry_delay(BORDER_LEAF_WAIT.delay, attempt))
        if not border_leafs:
            return BorderLeafs()
        names = sorted(bl.name.value for bl in border_leafs)
        uplinks = await self.client.filters(
            kind=DcimPhysicalInterface, device__name__values=names, role__value="uplink", include=["device"]
        )
        return BorderLeafs(
            names=names,
            uplinks=[
                PortFacts(
                    port.device.peer.name.value,
                    port.name.value,
                    getattr(getattr(port, "interface_type", None), "value", None),
                )
                for port in sorted(uplinks, key=lambda p: p.name.value)
            ],
        )

    def _plan_border_leaf_cabling(
        self,
        plan: TopologyPlan,
        model: PodFabricModel,
        dc: Any,
        reads: PodReads,
        spines: list[str],
        spine_ports: dict[str, list[Any]],
        technical: NodeRef,
        route: Any,
        problems: list[str],
    ) -> None:
        """Cable and route the pod's border-leafs (created by dc.py) to its spines, matched by deterministic name; nothing
        if the pod hosts none. The routing call is the gate (``plan_routing`` itself skips an unsupported strategy;
        gating here would skip ospf-ibgp).
        """
        border = reads.border_leafs
        if not border.names:
            return
        # The uplink names are the ones every border-leaf has; ports are per (device, name)
        bl_uplink_names = sorted({port.port_name for port in border.uplinks})
        downlinks = list(model.spine_downlink_interfaces)
        if not bl_uplink_names or not downlinks:
            problems.append(
                f"Pod {self.data['name']}: cannot cable border-leaf uplinks — "
                f"bl_uplinks={len(bl_uplink_names)}, spine_downlinks={len(downlinks)}."
            )
            return
        known = len(problems)
        top_ports = [
            port
            for name in spines
            for port in template_port_facts(
                [name], downlinks, spine_ports[name], label=model.spine_role, problems=problems
            )
        ]
        outcome = plan_cabling(
            plan,
            skip=len(problems) > known,
            bottom_ports=list(border.uplinks),
            top_ports=top_ports,
            strategy="rack",
            offset=spine_downlink_layout(self.data["design"])["border-leaf"],
            bottom_sorting="bottom_up",
            top_sorting="bottom_up",
            deployment_id=model.dc_id,
            pool_id=technical,
            p2p_prefix_length=p2p_prefix_length(model.underlay_protocol),
            positions=reads.positions,
        )
        problems.extend(outcome.problems)
        route(border.names, spines, outcome.links)

    async def _wait_for_super_spine_overlay(self, super_spine_devices: list[str], dc_id: str, pod_name: str) -> bool:
        """Whether the parent DC's super-spines have their overlay BGP: retried with backoff, fails if they never land
        (nothing re-runs an early return).
        """
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            if await bgp_processes_ready(self.client, super_spine_devices, "overlay"):
                return True
            if await self.wait_for_parent_generator_and_refetch(DC_GENERATORS, dc_id) is not None:
                continue
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"Pod {pod_name}: parent DC super-spine overlay BGP not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)
        self.logger.error(
            f"Pod {pod_name}: parent DC super-spine overlay BGP never appeared after {WAIT_FOR_PEER_GENERATOR.attempts} "
            "attempts — run add_dc for its DC first"
        )
        return False
