"""Colocation zone and virtual colocation: the kit they declare (``fabric_templates``) and deploy; the metro owns no
devices. A zone hosts physical templates, a virtual colocation provider-hosted ones; the other kind is skipped with a
warning. Virtual devices are not cabled or edge-paired. Cables are internal to the zone; anything leaving it is a
TopologyCarrierCircuit. Pools: colocation_pools.py; EVPN: colocation_evpn.py.

One plan per run: the pools, devices, HA domains, cabling and EVPN border gateways of the node, written by the
``PlanReconciler`` under the locks of every decision it takes from a read (``plan_locks``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from generators.base import BaseGenerator
from generators.capabilities.bgp import (
    RoutingReads,
    address_family_lock_key,
    plan_shared_as,
    read_address_families,
    routing_devices,
)
from generators.capabilities.common import Ref
from generators.capabilities.ha import (
    FIREWALL_CONTEXT,
    HaReads,
    ServiceReads,
    context_vlan_pool_lock,
    plan_appliance_ha_pairs,
    plan_context_vlan_pool,
    plan_fabric_services,
    read_ha_reads,
    read_service_reads,
    role_index_starts,
    service_entries,
    service_lock_keys,
    service_names,
)
from generators.colocation.colocation_evpn import (
    COLO_FABRIC_AFS,
    EdgePairReads,
    EvpnEdge,
    plan_edge_borders,
    plan_edge_pair,
    site_as_description,
)
from generators.colocation.colocation_pools import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_TECHNICAL_PREFIX_LENGTH,
    ATTACHED_SLOTS,
    LOOPBACK_IPV6_SLOT,
    LOOPBACK_SLOT,
    MANAGEMENT_SLOT,
    TECHNICAL_IPV6_SLOT,
    ColocationFabricData,
    ColoPoolSlot,
)
from generators.dcim.cabling_plan import chain_ports_of, raise_problems, read_chain_ports
from generators.dcim.device_plan import (
    APPLIANCE_KIND,
    DEVICE_KIND,
    HA_KIND_BY_ROLE,
    DeviceOptions,
    DeviceReads,
    object_id,
    plan_physical_devices,
    read_device_state,
    role_group_name,
)
from generators.dcim.rack_layout import read_device_positions
from generators.dcim.template_interfaces import TemplateReads, read_template_ports
from generators.helpers.naming import naming_convention_of
from generators.helpers.pairing import pair_device_names
from generators.helpers.routing import loopback_prefix_length
from generators.ipam.pools import (
    NUMBER_POOL_KIND,
    PREFIX_POOL_KIND,
    plan_pool_refs,
    plan_sliced_pool,
    read_pool_ids,
)
from generators.model.colocation_model import (
    COLO_TEMPLATE_KIND_APPLIANCE,
    COLO_TEMPLATE_KIND_PHYSICAL,
    ColocationFabricModel,
)
from generators.plan import ExternalProducer, Hfid, NodeRef, TopologyPlan
from generators.protocols import (
    RoutingAutonomousSystem,
    TopologyColocationMetro,
    TopologyColocationZone,
    TopologyVirtualColocation,
)
from utils.data_cleaning import clean_data
from utils.limits import WAIT_FOR_PEER_GENERATOR


@dataclass(frozen=True)
class ColocationReads:
    """What exists before the run and the plan needs: the devices of earlier runs and the role groups, the template
    ports, the HA state with the live ports and rack positions of the devices the cables join, the ids of the BGP
    address families of the edge pair, the metro's site AS (None: not drawn yet) and its ASN pool (set when the node has
    edges).
    """

    devices: DeviceReads
    templates: TemplateReads
    services: ServiceReads
    families: Mapping[tuple[str, str], str]
    site_as_id: str | None
    asn_pool_id: str | None


class ColocationFabricGenerator(BaseGenerator):
    """Create the pools and the on-ramp devices a zone or virtual colocation declares; subclasses set the class
    attributes.
    """

    data: ColocationFabricData
    # Set by the subclass
    graphql_root_key: ClassVar[str]
    _node_kind: ClassVar[type[TopologyColocationZone] | type[TopologyVirtualColocation]]
    _template_kind: ClassVar[str]
    _label: ClassVar[str]

    async def generate(self, data: dict[str, Any]) -> None:
        # clean_data() flattens the GraphQL edges/node/value nesting
        nodes = clean_data(data).get(self.graphql_root_key, [])
        if not nodes:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        for node in nodes:
            self.data = cast(ColocationFabricData, node)
            await self._generate_fabric()

    async def _generate_fabric(self) -> None:
        node_id = self.data["id"]
        node_name = self.data["name"]
        metro = self.data.get("parent") or {}

        # pod_name stays None: a zone has no pod tier, so its pools are the zone-scoped ones
        self.fabric_name = node_name.lower()
        self.pod_name = None
        self.deployment_id = node_id

        self.logger.info(f"Generating colocation {self._label} {node_name} (metro {metro.get('name', '?')})")

        templates = self._valid_fabric_templates()
        if not templates:
            self.logger.info(
                f"Colocation {self._label} {node_name}: no usable fabric_templates entries, nothing to generate"
            )
            return

        if not self._design:
            self.logger.warning(
                f"Colocation {self._label} {node_name}: declares kit but has no design, so its pools are sized with "
                f"the fixed fallback (a /{_COLO_LOOPBACK_PREFIX_LENGTH} loopback, /{_COLO_MANAGEMENT_PREFIX_LENGTH} "
                f"management and /{_COLO_TECHNICAL_PREFIX_LENGTH} technical pool) and nothing bounds how much kit it "
                "holds (the check errors)."
            )
        edges = [entry for entry in templates if entry["role"] == "edge"]
        entries = service_entries(templates)
        names = self._service_names(entries)
        # The metro's ASN pool is add_colocation_metro's output: waited for before the plan, which draws the site AS from it
        asn_pool_id = await self._metro_asn_pool_id() if edges else None
        # Pools, the HA domains and their group ids, the context VLAN pool, the site AS, the FABRIC key, the address
        # families and the edge pair's free ports are decisions taken from a read: the read, the plan and the write hold
        # the same locks, so two overlapping runs never both take them
        async with self.plan_locks(*self._lock_keys(edges, entries, names)):
            reads = await self.read_plan_inputs(templates, entries, names, asn_pool_id)
            plan = self.build_plan(templates, entries, reads)
            await self.apply_plan(plan)
        self.logger.info(
            f"Colocation {self._label} {node_name}: attached loopback/management/technical pool references"
        )
        self._log_fabric_devices(templates)

    def _valid_fabric_templates(self) -> list[dict[str, Any]]:
        """The entries this generator can act on, warning per dropped entry (warning, not error, so other roles are
        still created).
        """
        model = ColocationFabricModel.compile(self.data, label=self._label, template_kind=self._template_kind)
        for warning in model.warnings:
            self.logger.warning(warning)
        return list(model.usable_templates)

    @property
    def _design(self) -> dict[str, Any] | None:
        """This node's own design (TopologyTemplateColocationZone), None when it has none."""
        return self.data.get("design")

    @property
    def _metro_design(self) -> dict[str, Any] | None:
        """The parent metro's design (TopologyTemplateColocationMetro), None when it has none."""
        return (self.data.get("parent") or {}).get("design")

    @property
    def _slices_from_metro(self) -> bool:
        """Whether this node's pools are cut out of the metro's supernets: only when both tiers have a design."""
        return bool(self._design and self._metro_design)

    @property
    def _virtual(self) -> bool:
        """Whether the kit is provider-hosted (a virtual colocation): appliances, not cabled, not edge-paired."""
        return self._template_kind == COLO_TEMPLATE_KIND_APPLIANCE

    @property
    def _device_kind(self) -> str:
        return APPLIANCE_KIND if self._virtual else DEVICE_KIND

    def _parent_pool_name(self, slot: ColoPoolSlot) -> str:
        """The pool this node's ``slot`` is sliced from: the metro's supernet, else the bootstrap pool."""
        if self._slices_from_metro:
            return slot.name((self.data.get("parent") or {})["name"].lower())
        return slot.bootstrap_pool

    def _naming_convention(self) -> Any:
        return naming_convention_of(self.data.get("parent") or {})

    def _edge_names(self, edges: list[dict[str, Any]]) -> list[str]:
        """The names of the devices of the ``edge`` entries (flat names ``{role code}-{zone}{NN}``)."""
        return [name for names in self._service_names([("edge", entry) for entry in edges]) for name in names]

    def _pair_of(self, edges: list[dict[str, Any]]) -> tuple[str, str] | None:
        """The two physical edges the node links and peers: None unless it hosts physical kit with exactly two edges."""
        names = self._edge_names(edges)
        if self._virtual or len(names) != 2:
            return None
        first, second = sorted(names)
        return first, second

    def _service_names(self, entries: list[tuple[str, dict[str, Any]]]) -> list[list[str]]:
        """The names of the devices of each firewall and load-balancer entry."""
        return service_names(
            entries, fabric_name=self.fabric_name, naming_convention=self._naming_convention(), indexes=[]
        )

    def _lock_keys(
        self, edges: list[dict[str, Any]], entries: list[tuple[str, dict[str, Any]]], names: list[list[str]]
    ) -> list[str]:
        """The locks of the run: the pool slices, the HA domains of the service devices (and the firewall group id
        pool), the context VLAN pool of a zone with firewalls and, with edges, the shared site AS; with an edge pair
        also its key, its address families and its free ports.
        """
        name = self.fabric_name
        keys = [f"colocation-pools-{name}", *service_lock_keys(entries, names)]
        if self._has_context_pool(entries):
            keys.append(context_vlan_pool_lock(name=name, spec=FIREWALL_CONTEXT))
        if edges:
            keys.append(f"shared-as-{site_as_description((self.data.get('parent') or {})['name'])}")
        if self._pair_of(edges) is not None:
            keys.extend(
                [
                    f"edge-pair-{name}",
                    f"routing-password-{name}-fabric-key",
                    *(address_family_lock_key(*family) for family in COLO_FABRIC_AFS),
                ]
            )
        return keys

    def _has_context_pool(self, entries: list[tuple[str, dict[str, Any]]]) -> bool:
        """Only a zone with physical firewalls gets the VLAN pool of its FirewallContexts."""
        return not self._virtual and any(role == "internal-firewall" for role, _ in entries)

    async def _metro_asn_pool_id(self) -> str:
        """The id of the parent metro's ASN pool; waits for add_colocation_metro, then looks it up by name with bounded
        retry.
        """
        metro = self.data.get("parent") or {}
        if pool_id := object_id(metro.get("fabric_asn_pool")):
            return pool_id

        pool_name = f"{metro.get('name', '').lower()}-asn-pool"
        await self.wait_for_parent_generators(("add_colocation_metro",), metro["id"])
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            if found := (await read_pool_ids(self.client, {pool_name: NUMBER_POOL_KIND})).get(pool_name):
                return found
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                await asyncio.sleep(self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt))
        self.fail(
            f"Colocation {self._label} {self.fabric_name}: metro {metro.get('name', '')} has no ASN pool "
            f"'{pool_name}' (add_colocation_metro creates it) — cannot draw the metro's EVPN site AS"
        )

    async def read_plan_inputs(
        self,
        templates: list[dict[str, Any]],
        entries: list[tuple[str, dict[str, Any]]],
        names: list[list[str]],
        asn_pool_id: str | None,
    ) -> ColocationReads:
        """Everything the plan needs that exists before the run: no read of what this run creates."""
        edges = [entry for entry in templates if entry["role"] == "edge"]
        edge_names = self._edge_names(edges)
        devices = await read_device_state(
            self.client,
            [*edge_names, *(name for entry_names in names for name in entry_names)],
            {role_group_name(entry["role"]) for entry in templates if entry["role"] != "edge"},
            kind=self._device_kind,
        )
        if self._virtual:
            # A provider-hosted appliance has no template ports and nothing to cable
            template_reads = TemplateReads(ports={}, owners={})
            services = ServiceReads(ha=await self._read_appliance_ha(entries, names), positions={}, chain_ports={})
        else:
            template_reads = await read_template_ports(self.client, {entry["template"]["id"] for entry in templates})
            if entries:
                services = await read_service_reads(self.client, entries=entries, names=names, border_names=edge_names)
            else:
                services = ServiceReads(
                    ha=HaReads(),
                    positions=await read_device_positions(self.client, edge_names),
                    chain_ports=await read_chain_ports(self.client, edge_names),
                )
        site_as_id = None
        if edges:
            found = await self.client.filters(
                kind=RoutingAutonomousSystem,
                description__value=site_as_description((self.data.get("parent") or {})["name"]),
            )
            site_as_id = found[0].id if found else None
        families = await read_address_families(self.client, COLO_FABRIC_AFS) if self._pair_of(edges) is not None else {}
        return ColocationReads(
            devices=devices,
            templates=template_reads,
            services=services,
            families=families,
            site_as_id=site_as_id,
            asn_pool_id=asn_pool_id,
        )

    async def _read_appliance_ha(self, entries: list[tuple[str, dict[str, Any]]], names: list[list[str]]) -> HaReads:
        """The HA domains of the appliance pairs (an appliance has no sync port or cable to read)."""
        kinds = {
            f"{first}-{second}": HA_KIND_BY_ROLE[role]
            for (role, _), entry_names in zip(entries, names, strict=True)
            for first, second in pair_device_names(list(entry_names))
        }
        if not kinds:
            return HaReads()
        return await read_ha_reads(
            self.client,
            domains=kinds,
            device_names=[],
            firewall=any(kind == "ManagedFirewallHA" for kind in kinds.values()),
        )

    def build_plan(
        self, templates: list[dict[str, Any]], entries: list[tuple[str, dict[str, Any]]], reads: ColocationReads
    ) -> TopologyPlan:
        """The node's pools (loopback, management, technical and the VTEP pool, each a slice of a metro supernet or a
        bootstrap pool; the first three attached to the node), its devices (an edge gets a /32 loopback, a
        firewall/load-balancer is HA-paired without one), the VLAN pool of a zone with physical firewalls, the cabling of
        its services to its edges and its EVPN border gateways, with the edge pair's interlink and FABRIC session. A cable
        that cannot be planned raises before anything is written.
        """
        name = self.fabric_name
        node_id = self.data["id"]
        metro = self.data.get("parent") or {}
        plan = TopologyPlan(
            root_id=node_id,
            producers=[ExternalProducer(("add_colocation_metro",), metro["id"])] if self._slices_from_metro else [],
        )
        edges = [entry for entry in templates if entry["role"] == "edge"]
        pair = self._pair_of(edges)
        slots = (*ATTACHED_SLOTS, LOOPBACK_IPV6_SLOT, *((TECHNICAL_IPV6_SLOT,) if pair else ()))
        pools = {
            slot: plan_sliced_pool(
                plan,
                name=slot.name(name),
                parent=Hfid(PREFIX_POOL_KIND, (self._parent_pool_name(slot),)),
                prefix_length=slot.zone_length(self._design),
                role=slot.role,
                kind=slot.zone_kind,
            )
            for slot in slots
        }
        plan_pool_refs(
            plan,
            kind=self._node_kind,
            node_id=node_id,
            refs={cast(str, slot.attribute): pools[slot] for slot in ATTACHED_SLOTS},
            current={},
        )
        problems: list[str] = []
        platforms, ports = self._plan_devices(plan, templates, entries, reads, pools, problems)
        if self._has_context_pool(entries):
            plan_context_vlan_pool(plan, name=name, spec=FIREWALL_CONTEXT)
        if platforms:
            self._plan_evpn(plan, platforms, reads, pools, pair, ports, problems)
        if edges and pair is None and not self._virtual:
            self.logger.info(
                f"Colocation {self._label} {name}: {len(platforms)} physical edge(s) — an edge pair needs exactly 2, "
                "no interlink or FABRIC session"
            )
        raise_problems(problems)
        return plan

    def _plan_devices(
        self,
        plan: TopologyPlan,
        templates: list[dict[str, Any]],
        entries: list[tuple[str, dict[str, Any]]],
        reads: ColocationReads,
        pools: Mapping[ColoPoolSlot, NodeRef],
        problems: list[str],
    ) -> tuple[dict[str, str], dict[str, list[Any]]]:
        """Every fabric_templates entry as devices of this node (flat names ``{role code}-{zone}{NN}``): the edges (a /32
        loopback, always allocated: a virtual edge, an MCR, still has one and peers over it), then the firewalls and load
        balancers with their HA domains and, for a physical zone, their cables to the edges. Returns edge name ->
        platform and the template ports of the edges.
        """
        account_id = object_id(self.data.get("account"))
        management = pools[MANAGEMENT_SLOT]
        platforms: dict[str, str] = {}
        ports: dict[str, list[Any]] = {}
        edges = [("edge", entry) for entry in templates if entry["role"] == "edge"]
        for (_, entry), start in zip(edges, role_index_starts(edges), strict=True):
            template = entry["template"]
            template_ports = reads.templates.ports.get(template["id"], [])
            names = plan_physical_devices(
                plan,
                reads=reads.devices,
                fabric_name=self.fabric_name,
                pod_name="",
                role="edge",
                quantity=entry["quantity"],
                deployment_id=self.data["id"],
                template=template,
                template_ports=template_ports,
                template_owner_id=reads.templates.owners.get(template["id"]),
                naming_convention=self._naming_convention(),
                options=DeviceOptions(
                    indexes=[],
                    allocate_loopback=True,
                    loopback_prefix_length=loopback_prefix_length("ipv4"),
                    loopback_pool=pools[LOOPBACK_SLOT],
                    management_pool=management,
                    role_index_start=start,
                ),
                controller=None,
                kind=self._device_kind,
                account_id=account_id,
            )
            platforms.update(dict.fromkeys(names, (template.get("platform") or {}).get("name") or ""))
            ports.update(dict.fromkeys(names, template_ports))
        if self._virtual:
            self._plan_appliances(plan, entries, reads, management, account_id)
        else:
            plan_fabric_services(
                plan,
                entries=entries,
                fabric_name=self.fabric_name,
                deployment_id=self.data["id"],
                cable_deployment_id=self.data["id"],
                naming_convention=self._naming_convention(),
                indexes=[],
                connectivity_mode="pbr",
                border_names=sorted(platforms),
                border_ports=ports,
                devices=reads.devices,
                templates=reads.templates,
                reads=reads.services,
                controllers=[],
                logger=self.logger,
                problems=problems,
                options=DeviceOptions(management_pool=management),
            )
        return platforms, ports

    def _plan_appliances(
        self,
        plan: TopologyPlan,
        entries: list[tuple[str, dict[str, Any]]],
        reads: ColocationReads,
        management: Ref,
        account_id: str | None,
    ) -> None:
        """The provider-hosted firewalls and load balancers: appliances ordered under the account, HA-paired (a pair has no
        sync interface or cable: a link between provider-hosted instances is a connection inside the provider's
        platform, not a DcimCable).
        """
        for (role, entry), start in zip(entries, role_index_starts(entries), strict=True):
            names = plan_physical_devices(
                plan,
                reads=reads.devices,
                fabric_name=self.fabric_name,
                pod_name="",
                role=role,
                quantity=entry["quantity"],
                deployment_id=self.data["id"],
                template=entry["template"],
                template_ports=[],
                template_owner_id=None,
                naming_convention=self._naming_convention(),
                options=DeviceOptions(indexes=[], management_pool=management, role_index_start=start),
                controller=None,
                kind=APPLIANCE_KIND,
                account_id=account_id,
            )
            plan_appliance_ha_pairs(
                plan, names=names, role=role, reads=reads.services.ha, appliance_kind=APPLIANCE_KIND
            )

    def _plan_evpn(
        self,
        plan: TopologyPlan,
        platforms: Mapping[str, str],
        reads: ColocationReads,
        pools: Mapping[ColoPoolSlot, NodeRef],
        pair: tuple[str, str] | None,
        ports: Mapping[str, list[Any]],
        problems: list[str],
    ) -> None:
        """Make the edges EVPN Multi-Site border gateways in the metro's site AS (shared by its zones, untracked: no run
        may own it), point the metro at it, and, with an edge pair, link and peer the two edges.
        """
        metro = self.data.get("parent") or {}
        site_as = plan_shared_as(
            plan,
            description=site_as_description(metro["name"]),
            asn_pool=reads.asn_pool_id,
            existing_id=reads.site_as_id,
            key="shared-as:evpn-site",
            track=False,
        )
        if site_as is None:
            self.fail(f"Colocation {self._label} {self.fabric_name}: no ASN pool to draw the EVPN site AS from")
        # The query read the metro's site AS: a re-run with it attached writes nothing
        plan_pool_refs(
            plan,
            kind=TopologyColocationMetro,
            node_id=metro["id"],
            refs={"evpn_rt_as": site_as},
            current={"evpn_rt_as": object_id(metro.get("evpn_rt_as"))},
        )
        devices = routing_devices(
            plan, reads=RoutingReads(), device_reads=reads.devices, roles=dict.fromkeys(platforms, "edge")
        )
        plan_edge_borders(
            plan,
            edges=[EvpnEdge(name, platform, self._device_kind) for name, platform in platforms.items()],
            site_as=site_as,
            routers={name: device.router_id for name, device in devices.items()},
            vtep_pool=pools[LOOPBACK_IPV6_SLOT],
        )
        if pair is None:
            return
        first, second = pair
        plan_edge_pair(
            plan,
            first=first,
            second=second,
            label=self._label,
            fabric_name=self.fabric_name,
            reads=EdgePairReads(
                ports=chain_ports_of(reads.services.chain_ports, {edge: ports[edge] for edge in pair}),
                positions=reads.services.positions,
            ),
            interlink_pool=pools[TECHNICAL_IPV6_SLOT],
            families=reads.families,
            deployment_id=self.data["id"],
            problems=problems,
        )

    def _log_fabric_devices(self, templates: list[dict[str, Any]]) -> None:
        """Log what each entry created and which kit is not cabled."""
        for entry in templates:
            role = entry["role"]
            self.logger.info(
                f"Colocation {self._label} {self.fabric_name}: created {entry['quantity']} "
                f"{'virtual' if self._virtual else 'physical'} {role} device(s)"
            )
            if self._virtual:
                self.logger.info(
                    f"Colocation {self._label} {self.fabric_name}: {role} kit is virtual, so it is not cabled — a "
                    "link between provider-hosted instances is a connection inside the provider's platform, not a "
                    "DcimCable"
                )


class ColocationZoneGenerator(ColocationFabricGenerator):
    """add_colocation_zone: the kit of a colocation cage, physical templates only."""

    graphql_root_key = "TopologyColocationZone"
    _node_kind = TopologyColocationZone
    _template_kind = COLO_TEMPLATE_KIND_PHYSICAL
    _label = "zone"


class VirtualColocationGenerator(ColocationFabricGenerator):
    """add_virtual_colocation: the kit of a provider-hosted presence, virtual templates only."""

    graphql_root_key = "TopologyVirtualColocation"
    _node_kind = TopologyVirtualColocation
    _template_kind = COLO_TEMPLATE_KIND_APPLIANCE
    _label = "virtual colocation"
