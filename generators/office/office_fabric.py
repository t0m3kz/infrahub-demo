"""The office (add_office_fabric): its fabric, its SD-WAN edge and its tunnels; no-op unless office.fabric_templates is
set. Roles distribution-switch and access-switch: pools, switches, cabling and OSPF underlay of the LISP site, dual-homed
on /31 links, OSPF area 0, no overlay BGP; attaches ``vni_pool`` and ``lisp_site_key``. Role sdwan-edge (one): the edge,
one tunnel per entry of ``sdwan_gateways``, its attachment to the gateways' controller and, with no fabric, the office
LAN (generators/office/edge.py). With an edge and distribution switches it also builds the WAN exit (cabling,
tenant-VRF sub-interfaces, eBGP; AS from PRIVATE-ASN32, VLAN from wan_exit_vlan): generators/office/wan_exit.py. The
edge's internet access (``INET-<office>``) is authored data.

Plans: the fabric, the edge, then the WAN exit (cables, then the rest). All are written by the ``PlanReconciler``, the
decisions they take from a read under the locks (``plan_locks``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from infrahub_sdk.protocols import CoreNumberPool
from typing_extensions import TypedDict

from generators.base import BaseGenerator
from generators.capabilities.bgp import (
    IPV4_UNICAST_AF,
    RoutingReads,
    RoutingShared,
    address_family_lock_key,
    lisp_site_key_name,
    plan_password,
    plan_routing,
    read_address_families,
    routing_devices,
)
from generators.capabilities.ospf import plan_ospf_area
from generators.capabilities.vrf import local_vrf_ids, vrfs_held
from generators.dcim.cabling_plan import (
    ChainHop,
    ChainPorts,
    Link,
    chain_ports_of,
    plan_cabling,
    plan_chain_legs,
    raise_problems,
    read_chain_ports,
    template_port_facts,
)
from generators.dcim.device_plan import (
    DeviceOptions,
    DeviceReads,
    plan_physical_devices,
    read_device_state,
    role_group_name,
)
from generators.dcim.template_interfaces import TemplateReads, read_template_ports, template_interface_names_by_role
from generators.helpers.naming import naming_convention_of, sdwan_edge_name
from generators.helpers.routing import loopback_prefix_length, p2p_prefix_length
from generators.ipam.pools import (
    PREFIX_POOL_KIND,
    plan_number_pool,
    plan_pool_refs,
    plan_sliced_pool,
)
from generators.model.office_model import (
    ACCESS_ROLE,
    DISTRIBUTION_ROLE,
    OFFICE_LOOPBACK_PREFIX_LENGTH,
    OFFICE_MANAGEMENT_PREFIX_LENGTH,
    OFFICE_TECHNICAL_PREFIX_LENGTH,
    SDWAN_EDGE_ROLE,
    OfficeFabricModel,
)
from generators.office import edge as sdwan
from generators.office.wan_exit import (
    SWITCH_PORT_ROLE,
    ExitLink,
    edge_as_description,
    plan_exit_cabling,
    plan_exit_links,
    switch_as_description,
    wan_exit_key_name,
)
from generators.plan import Hfid, NodeRef, TopologyPlan
from generators.protocols import (
    DcimPhysicalDevice,
    DcimVirtualInterface,
    RoutingAutonomousSystem,
    SecurityZone,
    TopologyCustomerOffice,
)
from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, transit_pool_name, wan_exit_vlan
from utils.limits import (
    LOOPBACK_IPV4_POOL,
    MANAGEMENT_IPV4_POOL,
    PRIVATE_ASN32_POOL,
    SITE_VNI_END,
    SITE_VNI_START,
    TECHNICAL_IPV4_POOL,
)

# Access-to-distribution link ports: access `uplink` to distribution `downlink`; the distribution pair on `interlink`.
_UPLINK_ROLE = "uplink"
_DOWNLINK_ROLE = "downlink"
_INTERLINK_ROLE = "interlink"

# IPv4 (the pool sizes are in generators/model/office_model.py).
_UNDERLAY_PROTOCOL = "ipv4"

# The planner derives the underlay from the first half ("ospf"); RoutingShared.no_overlay ignores the second.
_ROUTING_STRATEGY = "ospf-ibgp"


class TopologyCustomerOfficeData(TypedDict, total=False):
    """The subset of queries/topology/add/office_fabric.gql this generator reads."""

    id: str
    name: str
    naming_convention: str
    owner: dict[str, Any]
    lan_prefixes: list[dict[str, Any]]
    sdwan_gateways: list[dict[str, Any]]
    fabric_templates: list[dict[str, Any]]


@dataclass(frozen=True)
class OfficeReads:
    """What exists before the run and the fabric plan needs: the devices of earlier runs and the role groups, the ports
    of the templates and the live ports of the distribution pair (its interlink).
    """

    devices: DeviceReads
    templates: TemplateReads
    chain_ports: Mapping[str, Any]


class OfficeFabricGenerator(BaseGenerator):
    """add_office_fabric — an office's pools, switches, cabling, OSPF underlay and LISP site key."""

    data: TopologyCustomerOfficeData
    graphql_root_key = "TopologyCustomerOffice"

    async def generate(self, data: dict[str, Any]) -> None:
        offices = clean_data(data).get(self.graphql_root_key, [])
        if not offices:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        for office in offices:
            self.data = cast(TopologyCustomerOfficeData, office)
            await self._generate_office()

    async def _generate_office(self) -> None:
        office_id = self.data["id"]
        office_name = self.data["name"]

        # pod_name stays None: an office has no pod tier, so per-fabric objects are named after the office.
        self.fabric_name = office_name.lower()
        self.pod_name = None
        self.deployment_id = office_id

        model = OfficeFabricModel.compile(self.data)
        for warning in model.warnings:
            self.logger.warning(warning)
        if model.errors:
            self.fail("\n".join(model.errors))
        templates, edge_template = list(model.usable_templates), model.edge_template
        gateways = self.data.get("sdwan_gateways") or []
        if gateways and edge_template is None:
            self.logger.warning(
                f"Office {office_name}: sdwan_gateways without a {SDWAN_EDGE_ROLE} fabric_templates entry — no edge, "
                "no tunnels"
            )
        if not templates and edge_template is None:
            self.logger.info(f"Office {office_name}: no usable fabric_templates entries — no fabric requested")
            return
        self.logger.info(f"Generating office {office_name}")

        # Everything the edge needs from the data is read, and every gap raised, before the first write
        attachments: list[sdwan.GatewayAttachment] = []
        controller = None
        if edge_template is not None:
            if not (self.data.get("owner") or {}).get("id"):
                self.fail(f"Office {office_name}: the office has no owner — the edge cannot be created")
            found = sdwan.gateway_attachments(office_name, gateways)
            gaps = [item for item in found if isinstance(item, str)]
            if gaps:
                self.fail("\n".join(gaps))
            attachments = [item for item in found if not isinstance(item, str)]
            if attachments:
                controller = await sdwan.find_orchestrator(self, office_name=office_name, gateways=attachments)

        if templates:
            # The pools slice shared bootstrap pools and the LISP site key is found or created by name: the read, the
            # plan and the write hold the locks, so two overlapping runs never both create them
            async with self.plan_locks(
                f"office-pools-{office_id}", f"routing-password-{lisp_site_key_name(self.fabric_name)}"
            ):
                reads = await self.read_plan_inputs(templates)
                plan = self.build_plan(templates=templates, reads=reads)
                await self.apply_plan(plan)
        if edge_template is not None:
            await self._build_edge(edge_template, attachments, controller, has_fabric=bool(templates))

        names = {entry["role"]: self._names(entry) for entry in templates}
        distribution_names = names.get(DISTRIBUTION_ROLE, [])
        if distribution_names and edge_template is not None:
            template = next(entry["template"] for entry in templates if entry["role"] == DISTRIBUTION_ROLE)
            await self._ensure_wan_exit(
                distribution_names=distribution_names, distribution_template=template, edge_template=edge_template
            )

    async def _build_edge(
        self,
        edge_template: dict[str, Any],
        attachments: list[sdwan.GatewayAttachment],
        controller: Any,
        *,
        has_fabric: bool,
    ) -> None:
        """The edge, then what hangs on it: a tunnel per gateway, the controller attachment and, with no switch fabric,
        the office LAN."""
        office_id, office_name = self.data["id"], self.data["name"]
        edge_name = sdwan_edge_name(office_name)
        template_name = edge_template.get("template_name")
        if not template_name:
            self.fail(f"Office {office_name}: the {SDWAN_EDGE_ROLE} entry's template has no template_name")
        reconciled = await self.apply_plan(
            sdwan.plan_edge(
                office_name=office_name,
                office_id=office_id,
                owner_id=self.data["owner"]["id"],
                template_name=template_name,
            )
        )
        edge_id = reconciled.device_ids[edge_name]
        if attachments:
            wan_ports = sorted(port for device, port in reconciled.interface_ids if device == edge_name)
            if not wan_ports:
                self.fail(
                    f"Office {office_name}: edge {edge_name} has no role={sdwan.EDGE_WAN_PORT_ROLE} port — "
                    f"its template {template_name} cannot carry the tunnels"
                )
            await sdwan.ensure_tunnels(
                self,
                office_name=office_name,
                office_id=office_id,
                edge_port_id=reconciled.interface_ids[(edge_name, wan_ports[0])],
                gateways=attachments,
            )
            if controller is not None:
                await sdwan.attach_to_orchestrator(self, controller, edge_id)
        if not has_fabric:
            await sdwan.ensure_office_lan(self, self.data, office_name=office_name, office_id=office_id)

    def _options(self, pools: Mapping[str, NodeRef] | None = None) -> DeviceOptions:
        options = DeviceOptions(
            indexes=[], allocate_loopback=True, loopback_prefix_length=loopback_prefix_length(_UNDERLAY_PROTOCOL)
        )
        if pools:
            options["loopback_pool"] = pools["loopback"]
            options["management_pool"] = pools["management"]
        return options

    def _names(self, entry: dict[str, Any]) -> list[str]:
        """The flat names ``{role code}-{office}{NN}`` of the devices of an entry."""
        from generators.dcim.device_plan import planned_device_names

        return planned_device_names(
            fabric_name=self.fabric_name,
            pod_name=None,
            device_role=entry["role"],
            quantity=entry["quantity"],
            naming_convention=naming_convention_of(self.data),
            options=self._options(),
        )

    async def read_plan_inputs(self, templates: list[dict[str, Any]]) -> OfficeReads:
        """What exists before the run: the devices and role groups, the template ports and, for a distribution pair, the
        live ports its interlink may use. No read of what this run creates.
        """
        names = {entry["role"]: self._names(entry) for entry in templates}
        devices = await read_device_state(
            self.client,
            [name for role_names in names.values() for name in role_names],
            {role_group_name(entry["role"]) for entry in templates},
        )
        template_reads = await read_template_ports(self.client, {entry["template"]["id"] for entry in templates})
        pair = names.get(DISTRIBUTION_ROLE, [])
        chain_ports = await read_chain_ports(self.client, pair) if len(pair) == 2 else {}
        return OfficeReads(devices=devices, templates=template_reads, chain_ports=chain_ports)

    def build_plan(self, *, templates: list[dict[str, Any]], reads: OfficeReads) -> TopologyPlan:
        """The office's fabric: its pools (slices of bootstrap pools keyed by pool name, and the L2 VNI pool of its
        segments, only the local band, never stretched), the VNI pool and LISP site key attached to the office, its
        switches (flat names ``{role code}-{office}{NN}``; both roles get Loopback0 and a management address), their
        cabling (every access-switch to every distribution-switch, then the distribution pair to each other) and the OSPF
        underlay on those links. A cable that cannot be planned raises before anything is written.
        """
        office = self.fabric_name
        plan = TopologyPlan(root_id=self.data["id"])
        pools = {
            kind: plan_sliced_pool(
                plan,
                name=f"{office}-{kind}-pool",
                parent=Hfid(PREFIX_POOL_KIND, (parent,)),
                prefix_length=length,
                role=kind,
                kind=pool_kind,
            )
            for kind, parent, length, pool_kind in (
                ("loopback", LOOPBACK_IPV4_POOL, OFFICE_LOOPBACK_PREFIX_LENGTH, "address"),
                ("management", MANAGEMENT_IPV4_POOL, OFFICE_MANAGEMENT_PREFIX_LENGTH, "address"),
                ("technical", TECHNICAL_IPV4_POOL, OFFICE_TECHNICAL_PREFIX_LENGTH, "prefix"),
            )
        }
        vni_pool = plan_number_pool(
            plan,
            name=f"{office}-vni-pool",
            description=f"L2 VNI pool for office {office.upper()}",
            start_range=SITE_VNI_START,
            end_range=SITE_VNI_END,
            node="ManagedSegmentDeployment",
            node_attribute="vni",
        )
        # The office's shared LISP site key: found or created, tracked
        site_key = plan_password(
            plan,
            name=lisp_site_key_name(office),
            description=f"Shared LISP map-server site key for {office}",
        )
        plan_pool_refs(
            plan,
            kind=TopologyCustomerOffice,
            node_id=self.data["id"],
            refs={"vni_pool": vni_pool, "lisp_site_key": site_key},
            current={},
        )

        names: dict[str, list[str]] = {}
        for entry in templates:
            template = entry["template"]
            names[entry["role"]] = plan_physical_devices(
                plan,
                reads=reads.devices,
                fabric_name=office,
                pod_name="",
                role=entry["role"],
                quantity=entry["quantity"],
                deployment_id=self.data["id"],
                template=template,
                template_ports=reads.templates.ports.get(template["id"], []),
                template_owner_id=reads.templates.owners.get(template["id"]),
                naming_convention=naming_convention_of(self.data),
                options=self._options(pools),
                controller=None,
            )
        problems: list[str] = []
        links = self._plan_cabling(plan, templates, names, reads, pools["technical"], problems)
        raise_problems(problems)
        self._plan_routing(plan, names, links, reads.devices)
        return plan

    def _plan_cabling(
        self,
        plan: TopologyPlan,
        templates: list[dict[str, Any]],
        names: Mapping[str, list[str]],
        reads: OfficeReads,
        technical_pool: NodeRef,
        problems: list[str],
    ) -> list[Link]:
        """Every access-switch to every distribution-switch and the distribution pair to each other. A template with no
        `uplink` (access) or `downlink` (distribution) port is a problem that stops the plan, interlink included.
        """
        by_role = {entry["role"]: entry["template"] for entry in templates}
        access, distribution = names.get(ACCESS_ROLE, []), names.get(DISTRIBUTION_ROLE, [])
        length = p2p_prefix_length(_UNDERLAY_PROTOCOL)
        links: list[Link] = []

        if access and distribution:
            ports = {
                role: reads.templates.ports.get(by_role[role]["id"], []) for role in (ACCESS_ROLE, DISTRIBUTION_ROLE)
            }
            uplinks = template_interface_names_by_role(
                interfaces=by_role[ACCESS_ROLE].get("interfaces") or [], role=_UPLINK_ROLE
            )
            downlinks = template_interface_names_by_role(
                interfaces=by_role[DISTRIBUTION_ROLE].get("interfaces") or [], role=_DOWNLINK_ROLE
            )
            if not uplinks or not downlinks:
                problems.append(
                    f"Office {self.fabric_name}: cannot cable {sorted(access)} to {sorted(distribution)} "
                    f"— {_UPLINK_ROLE}_ports={len(uplinks)} on the {ACCESS_ROLE} template, "
                    f"{_DOWNLINK_ROLE}_ports={len(downlinks)} on the {DISTRIBUTION_ROLE} template."
                )
            else:
                known = len(problems)
                bottom = template_port_facts(access, uplinks, ports[ACCESS_ROLE], label=ACCESS_ROLE, problems=problems)
                top = template_port_facts(
                    distribution, downlinks, ports[DISTRIBUTION_ROLE], label=DISTRIBUTION_ROLE, problems=problems
                )
                outcome = plan_cabling(
                    plan,
                    skip=len(problems) > known,
                    bottom_ports=bottom,
                    top_ports=top,
                    strategy="rack",
                    offset=0,
                    bottom_sorting="bottom_up",
                    top_sorting="bottom_up",
                    deployment_id=self.data["id"],
                    pool_id=technical_pool,
                    p2p_prefix_length=length,
                    positions={},
                )
                problems.extend(outcome.problems)
                links.extend(outcome.links)

        if len(distribution) == 2:
            first, second = sorted(distribution)
            template_ports = reads.templates.ports.get(by_role[DISTRIBUTION_ROLE]["id"], [])
            links.extend(
                plan_chain_legs(
                    plan,
                    hops=(
                        ChainHop(devices=[first], down_role=_INTERLINK_ROLE),
                        ChainHop(devices=[second], up_role=_INTERLINK_ROLE),
                    ),
                    ports=chain_ports_of(reads.chain_ports, {first: template_ports, second: template_ports}),
                    deployment_id=self.data["id"],
                    positions={},
                    problems=problems,
                    pool_id=technical_pool,
                    p2p_prefix_length=length,
                )
            )
        return links

    def _plan_routing(
        self, plan: TopologyPlan, names: Mapping[str, list[str]], links: list[Link], device_reads: DeviceReads
    ) -> None:
        """The OSPF underlay of the office: area 0, a process per switch, an interface config per cabled port and a peering
        per cabled pair; no overlay BGP.
        """
        access, distribution = names.get(ACCESS_ROLE, []), names.get(DISTRIBUTION_ROLE, [])
        devices = routing_devices(
            plan,
            reads=RoutingReads(),
            device_reads=device_reads,
            roles={**dict.fromkeys(access, ACCESS_ROLE), **dict.fromkeys(distribution, DISTRIBUTION_ROLE)},
        )
        plan_routing(
            plan,
            shared=RoutingShared(
                strategy=_ROUTING_STRATEGY, ospf_area=plan_ospf_area(plan, self.fabric_name), no_overlay=True
            ),
            devices=devices,
            bottom_devices=access,
            top_devices=distribution,
            links=links,
            reads=RoutingReads(),
            mlag_pairs={},
            logger=self.logger,
        )

    async def _ensure_wan_exit(
        self, *, distribution_names: list[str], distribution_template: dict[str, Any], edge_template: dict[str, Any]
    ) -> None:
        """Build the office's exit to its SD-WAN edge, which this run has just built: cabling, tenant-VRF handoff
        sub-interfaces, eBGP."""
        office_id, office_name = self.data["id"], self.data["name"]
        edge_name = sdwan_edge_name(office_name)
        switch_ports = template_interface_names_by_role(
            interfaces=distribution_template.get("interfaces") or [], role=SWITCH_PORT_ROLE
        )
        if not switch_ports:
            self.logger.warning(
                f"Office {office_name}: the {DISTRIBUTION_ROLE} template has no {SWITCH_PORT_ROLE}-role port — "
                f"nothing to cable {edge_name} to, no WAN exit"
            )
            return

        # An office is always PROD: its exit lives in the PROD tenant VRF.
        zones = await self.client.filters(kind=SecurityZone, vrf_type__value=PROD_TYPE)
        if len(zones) != 1:
            self.fail(
                f"Office {office_name}: expected one SecurityZone of type {PROD_TYPE}, found {len(zones)} "
                "— load data/bootstrap/24_security_zones.yml"
            )
        zone = zones[0].name.value
        pool_name = transit_pool_name(zone)
        if not await self.client.filters(kind=PREFIX_POOL_KIND, name__value=pool_name):
            self.fail(
                f"Office {office_name}: transit pool {pool_name} not found — load data/bootstrap/23_transit_pools.yml"
            )

        # One run per office: overlapping runs would create the same processes, sub-interfaces and peerings; the shared
        # ASes, the key and the address family are found or created under their own locks.
        async with self.plan_locks(
            f"office-wan-exit-{office_id}",
            f"shared-as-{switch_as_description(self.fabric_name)}",
            f"shared-as-{edge_as_description(self.fabric_name)}",
            f"routing-password-{wan_exit_key_name(self.fabric_name)}",
            address_family_lock_key(*IPV4_UNICAST_AF),
        ):
            await self._build_wan_exit(
                edge_name=edge_name,
                distribution_names=distribution_names,
                distribution_template=distribution_template,
                edge_template=edge_template,
                transit_pool=pool_name,
                zone=zone,
            )

    async def _build_wan_exit(
        self,
        *,
        edge_name: str,
        distribution_names: list[str],
        distribution_template: dict[str, Any],
        edge_template: dict[str, Any],
        transit_pool: str,
        zone: str,
    ) -> None:
        """Cable, address and peer every link of the exit; every precondition has held by now."""
        office_id = self.data["id"]
        ports = await self._exit_ports(edge_name, distribution_names, distribution_template, edge_template)
        cabling = TopologyPlan(root_id=None)
        outcome = plan_exit_cabling(
            cabling, edge_name=edge_name, switch_names=distribution_names, ports=ports, deployment_id=office_id
        )
        raise_problems(outcome.problems)
        if not outcome.links:
            return
        cabled = await self.apply_plan(cabling)
        device_ids = {
            device.name.value: device.id
            for device in await self.client.filters(
                kind=DcimPhysicalDevice, name__values=[edge_name, *distribution_names]
            )
        }
        links = [
            ExitLink(
                switch=link.top,
                switch_port_id=cabled.ids[link.top.hfid],
                edge=link.bottom,
                edge_port_id=cabled.ids[link.bottom.hfid],
            )
            for link in sorted(outcome.links, key=lambda link: link.top.device_name)
        ]
        # Both ends get an instance of the namespace first and keep their device locks until the addresses are saved.
        needs = {device_ids[name]: {zone} for link in links for name in (link.switch.device_name, edge_name)}
        async with vrfs_held(self, needs):
            await self._write_exit_links(
                links=links, edge_name=edge_name, device_ids=device_ids, transit_pool=transit_pool, zone=zone
            )

    async def _exit_ports(
        self,
        edge_name: str,
        distribution_names: list[str],
        distribution_template: dict[str, Any],
        edge_template: dict[str, Any],
    ) -> ChainPorts:
        """The live ports of the edge and the distribution switches, their templates' where a device has none yet."""
        template_reads = await read_template_ports(self.client, [edge_template["id"], distribution_template["id"]])
        by_template = template_reads.ports
        return chain_ports_of(
            await read_chain_ports(self.client, [edge_name, *distribution_names]),
            {
                edge_name: by_template[edge_template["id"]],
                **dict.fromkeys(distribution_names, by_template[distribution_template["id"]]),
            },
        )

    async def _write_exit_links(
        self, *, links: list[ExitLink], edge_name: str, device_ids: Mapping[str, str], transit_pool: str, zone: str
    ) -> None:
        """The addresses, sub-interfaces and eBGP of the cabled links; the caller holds the VRF locks of both ends."""
        office_name = self.data["name"]
        vrf_ids = await local_vrf_ids(self, device_ids.values(), [zone])
        asn_pool = await self.client.get(kind=CoreNumberPool, name__value=PRIVATE_ASN32_POOL)
        descriptions = [switch_as_description(self.fabric_name), edge_as_description(self.fabric_name)]
        found_as = await self.client.filters(kind=RoutingAutonomousSystem, description__values=descriptions)
        loopbacks = await self.client.filters(
            kind=DcimVirtualInterface,
            device__ids=[device_ids[link.switch.device_name] for link in links],
            role__value="loopback",
            include=["device", "ip_address"],
        )
        plan = TopologyPlan(root_id=None)
        plan_exit_links(
            plan,
            office_name=office_name,
            fabric_name=self.fabric_name,
            edge_name=edge_name,
            links=links,
            transit_pool=transit_pool,
            vlan=wan_exit_vlan(PROD_TYPE),
            vrf_ids=vrf_ids,
            device_ids=device_ids,
            zone=zone,
            asn_pool_id=asn_pool.id,
            as_ids={str(node.description.value): node.id for node in found_as},
            families=await read_address_families(self.client, [IPV4_UNICAST_AF]),
            router_ids={
                loopback.device.peer.name.value: cast(str, loopback.ip_address.id)
                for loopback in loopbacks
                if loopback.ip_address.initialized
            },
            unicast=IPV4_UNICAST_AF,
            logger=self.logger,
        )
        await self.apply_plan(plan)
        for link in links:
            self.logger.info(f"Office {office_name}: ensured WAN exit {link.switch.device_name} <-> {edge_name}")
