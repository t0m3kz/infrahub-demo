"""Infrastructure generator for data center topology: a pure plan builder over what exists before the run, written by
the ``PlanReconciler`` (generators/plan.py)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from generators.base import BaseGenerator
from generators.capabilities.bgp import (
    RoutingShared,
    evpn_address_family,
    plan_routing,
    plan_shared_routing,
    routing_devices,
)
from generators.capabilities.ha import (
    FIREWALL_CONTEXT,
    LOADBALANCER_CONTEXT,
    ContextSpec,
    ServiceReads,
    context_vlan_pool_lock,
    plan_context_vlan_pool,
    plan_fabric_services,
    read_service_reads,
    service_entries,
    service_lock_keys,
    service_names,
)
from generators.dc.routing_inputs import RoutingInputs, read_routing_inputs
from generators.dcim.cabling_plan import PortFacts, plan_cabling, raise_problems
from generators.dcim.controllers import controllers_of, resolve_role_controller
from generators.dcim.device_plan import (
    DeviceOptions,
    DeviceReads,
    default_loopback_pool_name,
    plan_physical_devices,
    planned_device_names,
    read_device_state,
    role_group_name,
)
from generators.dcim.template_interfaces import TemplateReads, read_template_ports, template_interface_names_by_role
from generators.helpers.naming import naming_convention_of
from generators.helpers.routing import RoutingStrategy, loopback_prefix_length, p2p_prefix_length
from generators.ipam.pools import (
    ADDRESS_POOL_KIND,
    NUMBER_POOL_KIND,
    PREFIX_POOL_KIND,
    fabric_pool_nodes,
    plan_asn_pool,
    plan_number_pool,
    plan_pool_refs,
    pool_key,
    read_pool_ids,
    slot_pool_name,
)
from generators.model.dc_model import DC_FABRIC_LOOPBACK, DC_VALID_FABRIC_ROLES, DcFabricModel
from generators.model.pod_profile import templates_by_role
from generators.plan import NodeRef, TopologyPlan
from generators.protocols import TopologyDataCenter
from utils.data_cleaning import clean_data
from utils.limits import SITE_VNI_END, SITE_VNI_START

# Strategies under which dc.py pre-seeds its own DC-scoped tiers' BGP.
_SEEDED_STRATEGIES = (
    RoutingStrategy.EBGP_EBGP.value,
    RoutingStrategy.EBGP_IBGP.value,
    RoutingStrategy.OSPF_IBGP.value,
)


# The context VLAN pool of a fabric per kind of service context: the roles whose devices need it
_CONTEXT_ROLES = (
    (FIREWALL_CONTEXT, ("internal-firewall", "external-firewall")),
    (LOADBALANCER_CONTEXT, ("load-balancer",)),
)


def _context_pools(entries: list[tuple[str, dict[str, Any]]], names: list[list[str]]) -> list[ContextSpec]:
    """The service context kinds this DC's service devices need a VLAN pool for (those with devices)."""
    return [
        context
        for context, roles in _CONTEXT_ROLES
        if any(entry_names for (role, _), entry_names in zip(entries, names, strict=True) if role in roles)
    ]


class TopologyDcData(TypedDict, total=False):
    """One clean_data()-processed TopologyDataCenter entry (queries/topology/add/dc.gql); no runtime validation."""

    id: str
    name: str
    index: int
    design: dict[str, Any]
    naming_convention: str
    connectivity_mode: str
    underlay_protocol: str
    routing_strategy: str
    fabric_templates: list[dict[str, Any]]
    loopback_pool: dict[str, Any] | None
    technical_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    fabric_asn_pool: dict[str, Any] | None
    # A TopologyPod child is {id, name, index, design: {id}}; any other child is just {id}.
    children: list[dict[str, Any]]
    vni_pool: dict[str, Any] | None
    evpn_rt_as: dict[str, Any] | None
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


@dataclass(frozen=True)
class DcTier:
    """One group of devices the DC places: a role entry of its fabric_templates (or the share of one pod)."""

    role: str
    quantity: int
    template: dict[str, Any]
    indexes: tuple[int, ...]
    deployment_id: str
    role_index_start: int = 1

    def options(self, underlay_protocol: str) -> DeviceOptions:
        return DeviceOptions(
            indexes=list(self.indexes),
            allocate_loopback=True,
            loopback_prefix_length=loopback_prefix_length(underlay_protocol),
            role_index_start=self.role_index_start,
        )


@dataclass(frozen=True)
class DcReads:
    """What exists before the run and the plan needs: the devices of earlier runs and the role groups, the ports and
    owners of the templates, the pools by name, the routing state of the DC-scoped tiers and the shared routing objects,
    and the HA state, positions and ports of the service devices and the border-leafs they are cabled to.
    """

    devices: DeviceReads
    templates: TemplateReads
    pool_ids: Mapping[str, str]
    routing: RoutingInputs
    services: ServiceReads


class DCTopologyGenerator(BaseGenerator):
    """Generate data center topology with super-spine infrastructure."""

    data: TopologyDcData

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology."""

        try:
            deployment_list = clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error("No TopologyDeployment data found in GraphQL response")
                return

            self.data = cast(TopologyDcData, deployment_list[0])
            # No Pydantic validation: the model force-reads every required field so a partial response raises
            # KeyError. The DC's fabric design sizes every DC pool.
            model = DcFabricModel.compile(self.data)
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        # The controllers that govern the devices of a role (``resolve_role_controller``).
        self._controllers = controllers_of(self.data)

        self.logger.info(f"Processing Data Center: {model.dc_name}")

        # Pods come with the DC query and are read, never saved (DCPodCascadeGenerator fans out to them).
        self._existing_pods = self.pods_of(self.data)

        self.deployment_id = model.dc_id  # Store for cable linking
        self.fabric_name = model.fabric_name
        self._warn_invalid_fabric_template_roles(model)
        self.logger.info(f"Generating topology for data center {self.fabric_name.upper()}")

        # Both checks come before the first save: a DC over its design writes nothing.
        if error := model.super_spine_error():
            raise RuntimeError(error)
        if error := model.hyper_spine_error():
            raise RuntimeError(error)

        self.logger.info(f"Allocating DC pools: {list(model.pools_to_allocate)}")
        self._underlay_protocol = model.underlay_protocol
        tiers = [*self._dc_tiers(model), *self._border_leaf_tiers()]
        # Pools, shared routing objects, the super-spine to hyper-spine P2P addresses, the HA domains, their group ids and
        # the context VLAN pools are decisions taken from a read: the read, the plan and the write hold the same locks, so
        # two overlapping runs never both create them.
        entries = service_entries(self.data.get("fabric_templates", []), external=True)
        names = self._service_names(entries)
        async with self.plan_locks(
            f"pool-alloc-fabric-{model.dc_id}",
            f"shared-routing-{model.dc_id}",
            f"hyperspine-cabling-{model.dc_id}",
            *service_lock_keys(entries, names),
            *(
                context_vlan_pool_lock(name=self.fabric_name, spec=context)
                for context in _context_pools(entries, names)
            ),
        ):
            reads = await self.read_plan_inputs(model, tiers, entries, names)
            plan = self.build_plan(model, tiers, reads, entries, names)
            await self.apply_plan(plan)

    def _dc_tiers(self, model: DcFabricModel) -> list[DcTier]:
        """The super-spine and hyper-spine groups (the DC-scoped tiers), one per fabric_templates entry; the numbering of a
        role continues across its entries, so a second entry never re-creates a name. No super-spine tier when
        back-to-back.
        """
        tiers: list[DcTier] = []
        roles = ["hyper-spine"] if model.design_mode == "back-to-back" else ["super-spine", "hyper-spine"]
        if model.design_mode == "back-to-back":
            self.logger.info(
                f"DC {self.fabric_name}: design_mode=back-to-back — skipping super-spine tier, spines will connect directly across pods"
            )
        for role in roles:
            planned = 0
            for entry in templates_by_role(self.data.get("fabric_templates", []), role):
                tiers.append(
                    DcTier(
                        role=role,
                        quantity=entry["quantity"],
                        template=entry["template"],
                        indexes=(model.dc_index,),
                        deployment_id=model.dc_id,
                        role_index_start=planned + 1,
                    )
                )
                planned += entry["quantity"]
        return tiers

    def _tier_names(self, tier: DcTier, model: DcFabricModel) -> list[str]:
        return planned_device_names(
            fabric_name=self.fabric_name,
            pod_name=None,
            device_role=tier.role,
            quantity=tier.quantity,
            naming_convention=naming_convention_of(self.data),
            options=tier.options(model.underlay_protocol),
        )

    def _service_names(self, entries: list[tuple[str, dict[str, Any]]]) -> list[list[str]]:
        """The names of the service devices of each entry (the DC's own firewalls, load balancers and Internet-edge pair)."""
        return service_names(
            entries,
            fabric_name=self.fabric_name,
            naming_convention=naming_convention_of(self.data),
            indexes=[self.data["index"]],
        )

    def _border_names(self, tiers: list[DcTier], model: DcFabricModel) -> list[str]:
        """The border-leafs the service devices are cabled to, sorted."""
        return sorted(name for tier in tiers if tier.role == "border-leaf" for name in self._tier_names(tier, model))

    async def read_plan_inputs(
        self,
        model: DcFabricModel,
        tiers: list[DcTier],
        entries: list[tuple[str, dict[str, Any]]],
        service_device_names: list[list[str]],
    ) -> DcReads:
        """Everything the plan needs that exists before the run: no read of what this run creates."""
        service_devices = [name for entry_names in service_device_names for name in entry_names]
        names = sorted({*(name for tier in tiers for name in self._tier_names(tier, model)), *service_devices})
        dc_scoped = [name for tier in tiers if tier.role != "border-leaf" for name in self._tier_names(tier, model)]
        pool_names = {
            slot_pool_name(self.fabric_name, slot): kind
            for slot, kind in (
                *(
                    (slot, ADDRESS_POOL_KIND if slot == "management" else PREFIX_POOL_KIND)
                    for slot in model.pools_to_allocate
                ),
            )
        }
        pool_names[f"{self.fabric_name}-asn-pool"] = NUMBER_POOL_KIND
        pool_names[f"{self.fabric_name}-vni-pool"] = NUMBER_POOL_KIND
        template_ids = {tier.template["id"] for tier in tiers} | {entry["template"]["id"] for _, entry in entries}
        devices = await read_device_state(
            self.client,
            names,
            {role_group_name(tier.role) for tier in tiers} | {role_group_name(role) for role, _ in entries},
        )
        templates = await read_template_ports(self.client, template_ids)
        pool_ids = await read_pool_ids(self.client, pool_names)
        routing = await read_routing_inputs(
            self,
            fabric_name=self.fabric_name,
            strategy=model.routing_strategy,
            names=dc_scoped,
            skip_underlay=model.routing_strategy == RoutingStrategy.OSPF_IBGP.value,
        )
        services = await read_service_reads(
            self.client, entries=entries, names=service_device_names, border_names=self._border_names(tiers, model)
        )
        return DcReads(devices=devices, templates=templates, pool_ids=pool_ids, routing=routing, services=services)

    def build_plan(
        self,
        model: DcFabricModel,
        tiers: list[DcTier],
        reads: DcReads,
        entries: list[tuple[str, dict[str, Any]]],
        service_device_names: list[list[str]],
    ) -> TopologyPlan:
        """The DC's own fabric: its pools (every one is re-allocated each run: skipping one drops it from the run's
        group, which deletes it), the ASN pool (eBGP strategies: one per DC, border-leafs draw from it too) and the L2
        VNI pool, the shared routing objects, the references on the DC, the DC-scoped tiers (super-spine unless
        back-to-back, hyper-spine) and the border-leafs, the pre-seeded BGP of the DC-scoped tiers and their
        super-spine to hyper-spine cabling and routing. A tier that cannot be cabled raises before anything is written.
        """
        fabric = self.fabric_name
        plan = TopologyPlan(root_id=model.dc_id)
        pools = fabric_pool_nodes(
            plan,
            strategy="fabric",
            pools=model.pools_to_allocate,
            fabric_name=fabric,
            pod_name=None,
            underlay_protocol=model.underlay_protocol,
        )
        # Deterministic ASN range from the DC name; border-leaf devices draw from the same pool.
        asn_name = f"{fabric}-asn-pool"
        asn_pool = (
            plan_asn_pool(
                plan,
                name=asn_name,
                description=f"ASN pool for {fabric.upper()} fabric",
                start_range=model.asn_start,
                end_range=model.asn_end,
            )
            if model.requires_asn_pool
            else None
        )
        # The local VLAN id is chosen per device (segment_vlan.py), so there is no per-DC pool. The L2 VNI pool is capped
        # at 39999: the RD/RT leave 16 bits for the VNI, and it must stay clear of the fixed L3 VNIs (60000+, exchange
        # transit 61000+) and of GLOBAL-L2VNI (40000-49999).
        vni_name = f"{fabric}-vni-pool"
        vni_pool = plan_number_pool(
            plan,
            name=vni_name,
            description=f"L2 VNI pool for {fabric.upper()}",
            start_range=SITE_VNI_START,
            end_range=SITE_VNI_END,
            node="ManagedSegmentDeployment",
            node_attribute="vni",
        )
        # Shared routing objects at DC level so pods and racks never duplicate them; overlay_asn is asn_end + 1, outside
        # the device pool but inside the DC's grid slot.
        shared = plan_shared_routing(
            plan,
            fabric_name=fabric,
            strategy=model.routing_strategy,
            overlay_asn=model.asn_end + 1,
            asn_pool=asn_pool,
            facts=reads.routing.facts,
            logger=self.logger,
        )
        self._plan_dc_refs(plan, model, reads, {**pools, asn_name: asn_pool, vni_name: vni_pool}, shared.overlay_as)

        names_of_role: dict[str, list[str]] = {}
        templates = reads.templates
        for tier in tiers:
            # Every DC-scoped tier takes its loopbacks from the DC-fabric loopback pool when the design sizes one
            loopback_pool_name = (
                slot_pool_name(fabric, DC_FABRIC_LOOPBACK)
                if DC_FABRIC_LOOPBACK in model.pools_to_allocate
                else default_loopback_pool_name(fabric, None, tier.role)
            )
            options = tier.options(model.underlay_protocol)
            if loopback_pool := pools.get(loopback_pool_name):
                options["loopback_pool"] = loopback_pool
            if management_pool := pools.get(slot_pool_name(fabric, "management")):
                options["management_pool"] = management_pool
            names_of_role.setdefault(tier.role, []).extend(
                plan_physical_devices(
                    plan,
                    reads=reads.devices,
                    fabric_name=fabric,
                    pod_name="",
                    role=tier.role,
                    quantity=tier.quantity,
                    deployment_id=tier.deployment_id,
                    template=tier.template,
                    template_ports=templates.ports.get(tier.template["id"], []),
                    template_owner_id=templates.owners.get(tier.template["id"]),
                    naming_convention=naming_convention_of(self.data),
                    options=options,
                    controller=resolve_role_controller(
                        self._controllers, device_role=tier.role, template=tier.template, logger=self.logger
                    ),
                )
            )
        super_spines, hyper_spines = names_of_role.get("super-spine", []), names_of_role.get("hyper-spine", [])
        if model.routing_strategy in _SEEDED_STRATEGIES:
            self._plan_routing(plan, model, reads, shared, asn_pool, super_spines, hyper_spines)
        self._plan_services(plan, model, tiers, reads, entries, service_device_names)
        return plan

    def _plan_dc_refs(
        self,
        plan: TopologyPlan,
        model: DcFabricModel,
        reads: DcReads,
        nodes: Mapping[str, Any],
        overlay_as: NodeRef,
    ) -> None:
        """Point the DC at its pools and its overlay AS, for the ones it does not point at already (an existing pool or
        AS is compared by id, a new one is always sent).
        """
        fabric = self.fabric_name
        names = {
            "loopback_pool": slot_pool_name(fabric, "loopback"),
            "management_pool": slot_pool_name(fabric, "management"),
            "technical_pool": slot_pool_name(fabric, "technical"),
            "vni_pool": f"{fabric}-vni-pool",
        }
        if model.requires_asn_pool:
            names["fabric_asn_pool"] = f"{fabric}-asn-pool"
        refs: dict[str, Any] = {attribute: reads.pool_ids.get(name) or nodes[name] for attribute, name in names.items()}
        refs["evpn_rt_as"] = reads.routing.facts.overlay_as_id or overlay_as
        plan_pool_refs(
            plan,
            kind=TopologyDataCenter,
            node_id=model.dc_id,
            refs=refs,
            current={name: (self.data.get(name) or {}).get("id") for name in refs},
        )

    def _plan_routing(
        self,
        plan: TopologyPlan,
        model: DcFabricModel,
        reads: DcReads,
        shared_objects: Any,
        asn_pool: NodeRef | None,
        super_spines: list[str],
        hyper_spines: list[str],
    ) -> None:
        """Pre-seed each DC-scoped tier's BGP before any pod runs (super-spines get overlay under ospf-ibgp too: the
        underlay is skipped there), then the super-spine to hyper-spine cabling and its routing.
        """
        ospf = model.routing_strategy == RoutingStrategy.OSPF_IBGP.value
        facts = reads.routing.facts
        roles = {**dict.fromkeys(super_spines, "super-spine"), **dict.fromkeys(hyper_spines, "hyper-spine")}
        devices = routing_devices(plan, reads=reads.routing.reads, device_reads=reads.devices, roles=roles)

        def shared(*, shared_underlay: bool, skip_underlay: bool) -> RoutingShared:
            return RoutingShared(
                strategy=model.routing_strategy,
                asn_pool=asn_pool,
                overlay_as=shared_objects.overlay_as,
                ospf_area=shared_objects.ospf_area,
                underlay_password=shared_objects.underlay_password,
                overlay_password=shared_objects.overlay_password,
                evpn_address_family=evpn_address_family(facts.evpn_address_family_id),
                shared_underlay_as=shared_objects.super_spine_as if shared_underlay and not ospf else None,
                skip_underlay=skip_underlay,
            )

        mlag_pairs = reads.routing.reads.mlag_pairs
        for names, shared_underlay in ((super_spines, True), (hyper_spines, False)):
            if names:
                plan_routing(
                    plan,
                    shared=shared(shared_underlay=shared_underlay, skip_underlay=ospf),
                    devices=devices,
                    bottom_devices=names,
                    top_devices=[],
                    links=[],
                    reads=reads.routing.reads,
                    mlag_pairs=mlag_pairs,
                    logger=self.logger,
                )
        if not (hyper_spines and super_spines):
            return
        templates = reads.templates
        fabric_templates = self.data.get("fabric_templates", [])
        uplinks = [
            name
            for entry in templates_by_role(fabric_templates, "super-spine")
            for name in template_interface_names_by_role(
                interfaces=entry["template"].get("interfaces", []), role="uplink"
            )
        ]
        downlinks = [
            name
            for entry in templates_by_role(fabric_templates, "hyper-spine")
            for name in template_interface_names_by_role(
                interfaces=entry["template"].get("interfaces", []), role="downlink"
            )
        ]
        if not (uplinks and downlinks):
            raise_problems(
                [
                    f"DC {self.fabric_name}: cannot cable super-spine<->hyper-spine — "
                    f"super_spine_uplinks={len(uplinks)}, hyper_spine_downlinks={len(downlinks)}."
                ]
            )
        problems: list[str] = []
        bottom_ports = _ports(super_spines, uplinks, "super-spine", fabric_templates, templates, problems)
        top_ports = _ports(hyper_spines, downlinks, "hyper-spine", fabric_templates, templates, problems)
        outcome = plan_cabling(
            plan,
            skip=bool(problems),
            bottom_ports=bottom_ports,
            top_ports=top_ports,
            strategy="pod",
            offset=0,
            bottom_sorting="bottom_up",
            top_sorting="bottom_up",
            deployment_id=model.dc_id,
            pool_id=NodeRef(pool_key(slot_pool_name(self.fabric_name, "technical"))),
            p2p_prefix_length=p2p_prefix_length(model.underlay_protocol),
            positions={},
        )
        raise_problems([*problems, *outcome.problems])
        plan_routing(
            plan,
            shared=shared(shared_underlay=False, skip_underlay=False),
            devices=devices,
            bottom_devices=super_spines,
            top_devices=hyper_spines,
            links=outcome.links,
            reads=reads.routing.reads,
            mlag_pairs=mlag_pairs,
            logger=self.logger,
        )

    def _warn_invalid_fabric_template_roles(self, model: DcFabricModel) -> None:
        """Log a fabric_templates entry with a role this generator cannot place; it is never acted on."""
        for role in model.invalid_roles:
            self.logger.warning(
                f"DC {self.fabric_name}: fabric_templates entry with role={role!r} is not valid "
                f"at DC level (expected one of {sorted(DC_VALID_FABRIC_ROLES)}) — skipping this entry."
            )

    @staticmethod
    def pods_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The DC's pods from its cleaned payload: the children that carry a design."""
        return [child for child in data.get("children") or [] if child.get("design")]

    def _pod_border_leaf_capacities(self, pods: list[dict[str, Any]]) -> dict[str, int]:
        """Pod id -> how many border-leaf devices the pod can receive (its design's max_border_leafs_per_pod).
        A pod capped at 0 is skipped; one whose design is not in the DC design's pod_templates gets none."""
        cap_by_design = {
            template["id"]: template["max_border_leafs_per_pod"] for template in self.data["design"]["pod_templates"]
        }
        capacities: dict[str, int] = {}
        for pod in pods:
            design_id = pod["design"]["id"]
            if design_id not in cap_by_design:
                self.logger.warning(
                    f"DC {self.fabric_name}: pod {pod['name']}'s design is not one of the DC design's "
                    "pod_templates — no border-leafs are placed on it."
                )
            capacities[pod["id"]] = cap_by_design.get(design_id, 0)
        return capacities

    def _border_leaf_tiers(self) -> list[DcTier]:
        """The border-leaf groups of every fabric_templates(role="border-leaf") entry, spread over the DC's pods in index
        order and capped per pod by max_border_leafs_per_pod; cabling to the spines is pod.py's job.
        """
        entries = templates_by_role(self.data.get("fabric_templates", []), "border-leaf")
        if not entries:
            return []

        existing_pods = getattr(self, "_existing_pods", [])
        if not existing_pods:
            self.logger.info(f"DC {self.fabric_name}: no pods yet — deferring border-leaf placement")
            return []
        sorted_pods = sorted(existing_pods, key=lambda p: p["index"])

        max_border_leafs_per_fabric = self.data["design"]["max_border_leafs_per_fabric"]
        capacities = self._pod_border_leaf_capacities(sorted_pods)
        tiers: list[DcTier] = []
        for entry in entries:
            if entry["quantity"] > max_border_leafs_per_fabric:
                self.logger.error(
                    f"DC {self.fabric_name}: border-leaf entry requests {entry['quantity']} devices but "
                    f"design.max_border_leafs_per_fabric allows at most {max_border_leafs_per_fabric} — skipping."
                )
                continue

            remaining = entry["quantity"]
            for pod in sorted_pods:
                if remaining <= 0:
                    break
                pod_capacity = capacities[pod["id"]]
                if pod_capacity <= 0:
                    continue
                share = min(remaining, pod_capacity)
                remaining -= share
                tiers.append(
                    DcTier(
                        role="border-leaf",
                        quantity=share,
                        template=entry["template"],
                        indexes=(self.data["index"], pod["index"]),
                        deployment_id=self.data["id"],
                    )
                )

            if remaining > 0:
                # A warning, not a failure: pods may arrive later (dc_pod_cascade places the rest), but it is a data
                # error once all pods exist.
                capacity = sum(capacities.values())
                self.logger.warning(
                    f"DC {self.fabric_name}: border-leaf entry requested {entry['quantity']} device(s) but "
                    f"{remaining} are left unplaced — the DC's {len(sorted_pods)} pod(s) offer "
                    f"max_border_leafs_per_pod capacity for {capacity} in total. Add pods, or lower the "
                    "entry's quantity to match."
                )
        return tiers

    def _plan_services(
        self,
        plan: TopologyPlan,
        model: DcFabricModel,
        tiers: list[DcTier],
        reads: DcReads,
        entries: list[tuple[str, dict[str, Any]]],
        service_device_names: list[list[str]],
    ) -> None:
        """The DC's shared service chain, cabled to the border-leafs (a pod added later needs a dc_pod_cascade run): the
        devices and their HA domains, then the context VLAN pool per service role (generators/customers/firewall_context.py
        allocates from it; the external pair takes its VLANs from the firewall contexts' pool), then the cables. A cable
        that cannot be planned raises before anything is written.
        """
        data = self.data
        problems: list[str] = []
        border_ports = {
            name: reads.templates.ports.get(tier.template["id"], [])
            for tier in tiers
            if tier.role == "border-leaf"
            for name in self._tier_names(tier, model)
        }
        plan_fabric_services(
            plan,
            entries=entries,
            fabric_name=self.fabric_name,
            deployment_id=data["id"],
            cable_deployment_id=data["id"],
            naming_convention=naming_convention_of(data),
            indexes=[data["index"]],
            connectivity_mode=cast(Literal["pbr", "inline"], data.get("connectivity_mode", "pbr")),
            border_names=self._border_names(tiers, model),
            border_ports=border_ports,
            devices=reads.devices,
            templates=reads.templates,
            reads=reads.services,
            controllers=self._controllers,
            logger=self.logger,
            problems=problems,
        )
        for context in _context_pools(entries, service_device_names):
            plan_context_vlan_pool(plan, name=self.fabric_name, spec=context)
        raise_problems(problems)


def _ports(
    devices: list[str],
    names: list[str],
    role: str,
    fabric_templates: list[dict[str, Any]],
    templates: TemplateReads,
    problems: list[str],
) -> list[PortFacts]:
    """The ports called ``names`` of each device, typed as the first template of ``role`` types them; a name the template
    lacks is a problem.
    """
    entries = templates_by_role(fabric_templates, role)
    ports = {port.name: port for entry in entries for port in templates.ports.get(entry["template"]["id"], [])}
    if missing := [name for name in names if name not in ports]:
        problems.append(f"{role} template has no port {missing}: those links cannot be planned")
    return [
        PortFacts(device, name, ports[name].interface_type) for device in devices for name in names if name in ports
    ]
