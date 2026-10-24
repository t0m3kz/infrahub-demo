"""Office fabric: the pools, switches, cabling and OSPF underlay of an office's LISP site.

Triggered on TopologyCustomerOffice creation (trigger rule
trigger-customer-office-fabric-on-created in data/events/99_actions.yml) and
on a later fabric_templates update. Registered as add_office_fabric in
.infrahub.yml, targeting the customer_deployments_office group and querying
office_fabric.gql.

No-ops unless office.fabric_templates is set — that relationship is the opt-in
(schemas/extensions/topology/topology_customer.yml), the way sdwan_gateway is
add_sdwan_edge's (generators/topology/sdwan_edge.py). It declares the fabric the
way a colocation zone declares its on-ramp kit: ``2 x distribution-switch on
template X``, never hand-written DcimPhysicalDevices.

One office footprint is one fabric (an SD-Access style site). Two roles, and no
others (utils/fabric_roles.py):

* distribution-switch — the co-located border and control-plane (map-server /
  map-resolver) node;
* access-switch — the fabric edge the office's hosts attach to.

What a run produces, all idempotent and all scoped to the office:

* ``{office}-loopback|management|technical-pool`` — one slice of each bootstrap
  IPv4 pool (data/bootstrap/17_ip_prefix_pools.yml), keyed by the pool name — and
  ``{office}-vni-pool``, the L2 VNI pool (the LISP instance-id) segments hosted by
  the office draw from; this one is attached to the office as ``vni_pool``, the
  field generators/topology/segment.py reads;
* the switches, with Loopback0 (the RLOC) and a management address;
* every access-switch dual-homed to every distribution-switch, and the pair of
  distribution-switches linked to each other, all on /31 point-to-point links
  from the technical pool;
* an OSPF area 0 underlay over those links, in the default namespace, and no
  overlay BGP at all (RoutingOptions.no_overlay);
* the LISP site key, attached to the office as ``lisp_site_key`` — the one secret
  the map-servers authenticate the edge ETRs with;
* the WAN exit, when the office has an SD-WAN edge (``<office>-EDGE1``, created by
  add_sdwan_edge — the office's sdwan_gateway is the signal): each distribution-switch
  cabled to the edge, a dot1q sub-interface on that link in the office's tenant VRF
  with a /31 from the VRF's transit pool, and an eBGP session (peering_role regular,
  ttl 1, ipv4 unicast, a key of its own) between a BGP process on the switch and one
  on the edge. The same shape as the hand-authored gateway handoff
  (data/demos/30_all/08_interconnects/04_sdwan/06_vrf_handoff.yml), generated. No new
  operator input: the AS numbers come from PRIVATE-ASN32, the VLAN from the VRF
  (utils/exchange_transit.wan_exit_vlan), everything else from the two devices. The
  edge's VCO payload describes its side from the same peering
  (transforms/config/controller.py _vrf_handoffs).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

from infrahub_sdk.protocols import CoreIPPrefixPool, CoreNumberPool, IpamNamespace
from typing_extensions import TypedDict

from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, transit_pool_name, wan_exit_vlan
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..helpers.naming import sdwan_edge_name
from ..helpers.pools import SITE_VNI_END, SITE_VNI_START
from ..helpers.routing import loopback_prefix_length, p2p_prefix_length
from ..helpers.template_interfaces import template_interface_names_by_role
from ..pools import PoolMixin
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedBGP,
    TopologyCustomerOffice,
)
from ..routing import IPV4_UNICAST_AF, RoutingMixin
from ..types import CablingOptions, DeviceOptions, RoutingOptions, naming_convention_of

ACCESS_ROLE = "access-switch"
DISTRIBUTION_ROLE = "distribution-switch"
_OFFICE_VALID_FABRIC_ROLES = CAMPUS_FABRIC_ROLES | LISP_BORDER_ROLES

_PHYSICAL_TEMPLATE_KIND = "TemplateDcimPhysicalDevice"

# The access-to-distribution link: the access-switch's `uplink` ports to the
# distribution-switch's `downlink` ports, and the link between the two
# distribution-switches on their `interlink` ports.
_UPLINK_ROLE = "uplink"
_DOWNLINK_ROLE = "downlink"
_INTERLINK_ROLE = "interlink"

# The office underlay is IPv4 and not design-driven (contrast dc.py, which
# derives every prefix length from its design in fabric_sizing.py). Sized for
# the campus (a handful of switches), well inside the bootstrap supernets: a /28 holds the
# loopbacks and the management addresses, a /27 the sixteen /31 links of two
# distribution-switches and a few access-switches.
_UNDERLAY_PROTOCOL = "ipv4"
_OFFICE_LOOPBACK_PREFIX_LENGTH = 28
_OFFICE_MANAGEMENT_PREFIX_LENGTH = 28
_OFFICE_TECHNICAL_PREFIX_LENGTH = 27

# The routing planner derives the underlay from the strategy's first half
# ("ospf") and, with RoutingOptions.no_overlay, ignores the second.
_ROUTING_STRATEGY = "ospf-ibgp"

# The WAN exit (see the module docstring). The edge's LAN ports are the `downlink`
# ports of its template (VCE-610_EDGE: GE3, GE4), the switch side its `uplink` port
# (_UPLINK_ROLE: toward the WAN), one switch per edge port.
_EDGE_LAN_PORT_ROLE = "downlink"
_WAN_EXIT_LINK_PREFIX_LENGTH = 31
_WAN_EXIT_PROCESS_ROLE = "regular"
# The BGP AS numbers of the exit: one for the pair of distribution-switches, one for the edge, each found by its
# description (ensure_shared_as) before one is drawn from the bootstrap private 32-bit pool.
_WAN_EXIT_ASN_POOL = "PRIVATE-ASN32"
# add_sdwan_edge runs beside this generator, not before it: wait a little for the edge it creates rather than
# skip the exit on a race. An office without an sdwan_gateway never waits.
_EDGE_WAIT_ATTEMPTS = 8
_EDGE_WAIT_BASE_DELAY = 3.0


class TopologyCustomerOfficeData(TypedDict, total=False):
    """The subset of queries/topology/add/office_fabric.gql this generator reads."""

    id: str
    name: str
    naming_convention: str
    sdwan_gateway: dict[str, Any] | None
    fabric_templates: list[dict[str, Any]]


@dataclass(frozen=True)
class _ExitLink:
    """One cabled edge-to-distribution-switch link of the WAN exit, addressed in the tenant VRF."""

    switch_name: str
    switch_id: str
    switch_sub: Any
    switch_address: Any
    edge_sub: Any
    edge_address: Any


class OfficeFabricGenerator(PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator):
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

        # pod_name stays None: an office has no pod tier, which is what makes
        # DeviceMixin.create_devices() and RoutingMixin name their per-fabric
        # objects after the office ("{fabric_name}-loopback-pool", "-ospf-area-0",
        # "-lisp-site-key").
        self.fabric_name = office_name.lower()
        self.pod_name = None
        self.deployment_id = office_id

        templates = self._valid_fabric_templates()
        if not templates:
            self.logger.info(f"Office {office_name}: no usable fabric_templates entries — no fabric requested")
            return
        self.logger.info(f"Generating the fabric of office {office_name}")

        pools = await self._ensure_office_pools(office_id=office_id)
        # The office's two references in one fetch and one save: its VNI pool and its LISP site key.
        site_key_id = await self.ensure_lisp_site_key()
        await self.attach_refs(
            kind=TopologyCustomerOffice,
            node_id=office_id,
            refs={"vni_pool": pools["vni"].id, "lisp_site_key": site_key_id},
        )
        names_by_role = await self._create_office_devices(templates=templates, office_id=office_id, pools=pools)

        access_names = names_by_role.get(ACCESS_ROLE, [])
        distribution_names = names_by_role.get(DISTRIBUTION_ROLE, [])
        pairs = await self._cable_office_fabric(
            templates=templates,
            access_names=access_names,
            distribution_names=distribution_names,
            technical_pool=pools["technical"],
        )

        area_id = await self.ensure_ospf_area()
        if area_id is None:
            return
        await self.create_routing(
            bottom_devices=access_names,
            top_devices=distribution_names,
            options=RoutingOptions(
                design={"routing_strategy": _ROUTING_STRATEGY},
                ospf_area_id=area_id,
                no_overlay=True,
            ),
            p2p_interfaces=pairs,
            bottom_role=ACCESS_ROLE,
            top_role=DISTRIBUTION_ROLE,
        )
        if distribution_names:
            template = next(entry["template"] for entry in templates if entry["role"] == DISTRIBUTION_ROLE)
            await self._ensure_wan_exit(distribution_names=distribution_names, distribution_template=template)

    def _valid_fabric_templates(self) -> list[dict[str, Any]]:
        """Drop entries this generator cannot act on, warning per entry.

        warning(), not error(): one bad entry must not stop the office's other
        role from being created (FailOnErrorLogger turns error() into a task
        failure), matching colocation.py. At most one entry per role: device
        names are derived from the role alone, so a second entry would rename
        the first one's devices onto itself.
        """
        usable: list[dict[str, Any]] = []
        seen_roles: set[str] = set()
        for entry in self.data.get("fabric_templates") or []:
            role = entry.get("role")
            if role not in _OFFICE_VALID_FABRIC_ROLES:
                self.logger.warning(
                    f"Office {self.fabric_name}: fabric_templates entry with role={role!r} is not valid in an "
                    f"office fabric (expected one of {sorted(_OFFICE_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            if not entry.get("quantity", 0) > 0:
                self.logger.warning(
                    f"Office {self.fabric_name}: fabric_templates entry for role={role!r} has quantity "
                    f"{entry.get('quantity')!r} — skipping this entry."
                )
                continue
            template = entry.get("template")
            if not template:
                self.logger.warning(
                    f"Office {self.fabric_name}: fabric_templates entry for role={role!r} has no template — "
                    "skipping this entry."
                )
                continue
            if template.get("template_kind") != _PHYSICAL_TEMPLATE_KIND:
                self.logger.warning(
                    f"Office {self.fabric_name}: fabric_templates entry for role={role!r} uses a "
                    f"{template.get('template_kind') or 'unknown'} template — an office fabric is physical "
                    "switches only, skipping this entry."
                )
                continue
            if role in seen_roles:
                self.logger.warning(
                    f"Office {self.fabric_name}: a second fabric_templates entry for role={role!r} — only one "
                    "entry per role is supported (device names derive from the role), skipping it."
                )
                continue
            seen_roles.add(role)
            usable.append(entry)
        return usable

    async def _ensure_office_pools(self, *, office_id: str) -> dict[str, Any]:
        """Create this office's loopback/management/technical/VNI pools (attaching the VNI pool is the caller's job).

        Each IP pool is a SLICE of a global bootstrap pool, never a fresh
        top-level supernet invented at runtime (see colocation_fabric.py's
        _ensure_fabric_pools for why), keyed by the pool name so a re-run
        gets the same slice back. The loopback and management pools are address
        pools because create_devices() hands them straight to
        allocate_next_ip_address(); the technical pool is a prefix pool the /31
        links are allocated from. Management addresses live in the MANAGEMENT
        namespace, as a data center's do (PoolMixin.allocate_resource_pools).

        Under a per-office lock: two overlapping runs would otherwise both
        create the same-named pool as two separate nodes.
        """
        office = self.fabric_name
        async with self.resource_lock(f"office-pools-{office_id}"):
            loopback_pool = await self.ensure_sliced_pool(
                pool_name=f"{office}-loopback-pool",
                parent_pool_name="Loopback-IPv4",
                prefix_length=_OFFICE_LOOPBACK_PREFIX_LENGTH,
                role="loopback",
                kind="address",
            )
            management_pool = await self.ensure_sliced_pool(
                pool_name=f"{office}-management-pool",
                parent_pool_name="Management-IPv4",
                prefix_length=_OFFICE_MANAGEMENT_PREFIX_LENGTH,
                role="management",
                kind="address",
                namespace="MANAGEMENT",
            )
            technical_pool = await self.ensure_sliced_pool(
                pool_name=f"{office}-technical-pool",
                parent_pool_name="Technical-IPv4",
                prefix_length=_OFFICE_TECHNICAL_PREFIX_LENGTH,
                role="technical",
                kind="prefix",
            )
            # The L2 VNI (the LISP instance-id) of a segment hosted by the office.
            # An office never stretches a segment (checks/segment_placement.py), so
            # only the local band is needed.
            vni_pool = await self.upsert_number_pool(
                pool_name=f"{office}-vni-pool",
                description=f"L2 VNI pool for office {office.upper()}",
                start_range=SITE_VNI_START,
                end_range=SITE_VNI_END,
                node="ManagedSegmentDeployment",
                node_attribute="vni",
            )
        return {"loopback": loopback_pool, "management": management_pool, "technical": technical_pool, "vni": vni_pool}

    async def _create_office_devices(
        self, *, templates: list[dict[str, Any]], office_id: str, pools: dict[str, Any]
    ) -> dict[str, list[str]]:
        """Instantiate every fabric_templates entry as the office's switches; names by role.

        indexes=[] — an office is flat, so the "standard" naming strategy yields
        "{role code}-{office}{NN}" (``ds-c005-office01``). Both roles get a Loopback0
        (the RLOC) and a management address, from the pools passed explicitly
        rather than re-resolved by name.
        """
        naming_convention = naming_convention_of(self.data)
        names_by_role: dict[str, list[str]] = {}
        for entry in templates:
            role = entry["role"]
            names_by_role[role] = await self.create_devices(
                deployment_id=office_id,
                device_role=role,
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_convention,
                options=DeviceOptions(
                    indexes=[],
                    allocate_loopback=True,
                    loopback_prefix_length=loopback_prefix_length(_UNDERLAY_PROTOCOL),
                    loopback_pool=pools["loopback"],
                    management_pool=pools["management"],
                ),
            )
        return names_by_role

    async def _cable_office_fabric(
        self,
        *,
        templates: list[dict[str, Any]],
        access_names: list[str],
        distribution_names: list[str],
        technical_pool: Any,
    ) -> list[tuple[Any, Any]]:
        """Cable every access-switch to every distribution-switch, and the distribution pair to each other.

        Returns the cabled interface pairs, the P2P links the OSPF underlay runs on.
        The ports come from the templates' own port roles: ``rack`` strategy gives
        each access-switch one uplink per distribution-switch (a plan that cannot
        fit raises, naming the shortfall). The interlink only exists for exactly
        two distribution-switches; without an `interlink` port pair it is skipped
        with a warning, and the pair still reaches each other through the access
        switches.
        """
        templates_by_role = {entry["role"]: entry["template"] for entry in templates}
        options = CablingOptions(pool=technical_pool, p2p_prefix_length=p2p_prefix_length(_UNDERLAY_PROTOCOL))
        pairs: list[tuple[Any, Any]] = []

        if access_names and distribution_names:
            uplinks = template_interface_names_by_role(
                interfaces=templates_by_role[ACCESS_ROLE].get("interfaces") or [], role=_UPLINK_ROLE
            )
            downlinks = template_interface_names_by_role(
                interfaces=templates_by_role[DISTRIBUTION_ROLE].get("interfaces") or [], role=_DOWNLINK_ROLE
            )
            if not uplinks or not downlinks:
                self.logger.error(
                    f"Office {self.fabric_name}: cannot cable {sorted(access_names)} to {sorted(distribution_names)} "
                    f"— {_UPLINK_ROLE}_ports={len(uplinks)} on the {ACCESS_ROLE} template, "
                    f"{_DOWNLINK_ROLE}_ports={len(downlinks)} on the {DISTRIBUTION_ROLE} template."
                )
                return pairs
            pairs.extend(
                await self.create_cabling(
                    bottom_devices=access_names,
                    bottom_interfaces=uplinks,
                    top_devices=distribution_names,
                    top_interfaces=downlinks,
                    strategy="rack",
                    options=options,
                )
            )

        if len(distribution_names) == 2:
            first, second = sorted(distribution_names)
            legs = await self.create_chain_cabling(
                hops=[
                    {"devices": [first], "down_role": _INTERLINK_ROLE},
                    {"devices": [second], "up_role": _INTERLINK_ROLE},
                ],
                options=options,
            )
            interlinks = legs[0] if legs else []
            if not interlinks:
                self.logger.warning(
                    f"Office {self.fabric_name}: no {_INTERLINK_ROLE}-role port pair on {first}/{second} — "
                    "the distribution pair is not linked directly"
                )
            pairs.extend(interlinks)
        return pairs

    async def _find_sdwan_edge(self, edge_name: str) -> tuple[Any, list[str]] | None:
        """The office's SD-WAN edge and the names of its LAN ports, None when it is not there (yet).

        add_sdwan_edge creates the edge from its own trigger, so it can land after this run reaches
        the exit. An office with an sdwan_gateway has an edge coming and polls for it (and for its
        template's ports, which are instantiated asynchronously); one without never waits. A run
        that still finds nothing skips the exit and the next run of this generator builds it.
        """
        attempts = _EDGE_WAIT_ATTEMPTS if self.data.get("sdwan_gateway") else 1
        for attempt in range(attempts):
            edges = await self.client.filters(kind=DcimPhysicalDevice, name__values=[edge_name])
            if edges:
                ports = await self.client.filters(
                    kind=DcimPhysicalInterface, device__ids=[edges[0].id], role__value=_EDGE_LAN_PORT_ROLE
                )
                if ports:
                    return edges[0], sorted(port.name.value for port in ports)
            if attempt < attempts - 1:
                delay = self._retry_delay(_EDGE_WAIT_BASE_DELAY, attempt)
                self.logger.info(
                    f"Office {self.fabric_name}: SD-WAN edge {edge_name} or its ports not there yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{attempts})"
                )
                await asyncio.sleep(delay)
        return None

    async def _ensure_wan_exit(self, *, distribution_names: list[str], distribution_template: dict[str, Any]) -> None:
        """Build the office's exit to its SD-WAN edge: cabling, tenant-VRF handoff sub-interfaces, eBGP.

        Skipped (logged, never raised) while the office has no SD-WAN edge. Everything it writes is
        keyed by the two devices, so a re-run converges on the same objects: the links by their
        interface ids (the /31 identifier), the AS numbers by description, the processes and the
        peerings by name.
        """
        office_name = self.data["name"]
        edge_name = sdwan_edge_name(office_name)
        found = await self._find_sdwan_edge(edge_name)
        if found is None:
            self.logger.info(
                f"Office {office_name}: no SD-WAN edge {edge_name} (yet) — no WAN exit; the next run builds it"
            )
            return
        edge, edge_ports = found

        # The switch side of the link is the template's `uplink` port: toward the WAN, as an access-switch's
        # uplink is toward the distribution layer.
        switch_ports = template_interface_names_by_role(
            interfaces=distribution_template.get("interfaces") or [], role=_UPLINK_ROLE
        )
        if not switch_ports:
            self.logger.warning(
                f"Office {office_name}: the {DISTRIBUTION_ROLE} template has no {_UPLINK_ROLE}-role port — "
                f"nothing to cable {edge_name} to, no WAN exit"
            )
            return

        # An office is always PROD: its exit lives in the PROD tenant VRF.
        namespaces = await self.client.filters(kind=IpamNamespace, namespace_type__value=PROD_TYPE)
        if len(namespaces) != 1:
            self.fail(
                f"Office {office_name}: expected one IpamNamespace of type {PROD_TYPE}, found {len(namespaces)} "
                "— load data/bootstrap/22_namespaces.yml"
            )
        pool_name = transit_pool_name(namespaces[0].name.value)
        transit_pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
        if not transit_pools:
            self.fail(
                f"Office {office_name}: transit pool {pool_name} not found — load data/bootstrap/23_transit_pools.yml"
            )

        # One run of an office at a time writes its exit: two overlapping runs would both create the same
        # processes, sub-interfaces and peerings.
        async with self.resource_lock(f"office-wan-exit-{self.data['id']}"):
            await self._build_wan_exit(
                edge=edge,
                edge_name=edge_name,
                edge_ports=edge_ports,
                distribution_names=distribution_names,
                switch_ports=switch_ports,
                transit_pool=transit_pools[0],
                vlan=wan_exit_vlan(PROD_TYPE),
            )

    async def _build_wan_exit(
        self,
        *,
        edge: Any,
        edge_name: str,
        edge_ports: list[str],
        distribution_names: list[str],
        switch_ports: list[str],
        transit_pool: Any,
        vlan: int,
    ) -> None:
        """Cable, address and peer every link of the exit; every precondition has held by now."""
        # Edge -> switch ("rack": every edge port to its own switch). No technical pool: the addresses of the
        # link are the tenant-VRF ones below, on the sub-interfaces.
        pairs = await self.create_cabling(
            bottom_devices=[edge_name],
            bottom_interfaces=edge_ports,
            top_devices=distribution_names,
            top_interfaces=switch_ports,
            strategy="rack",
        )
        if not pairs:
            return

        links: list[_ExitLink] = []
        for edge_port, switch_port in sorted(pairs, key=lambda pair: pair[1].device.display_label):
            link = await self._address_exit_link(
                edge_port=edge_port,
                edge_name=edge_name,
                switch_port=switch_port,
                transit_pool=transit_pool,
                vlan=vlan,
            )
            if link is not None:
                links.append(link)
        if not links:
            return

        await self._peer_exit_links(links=links, edge=edge, edge_name=edge_name)

    async def _address_exit_link(
        self, *, edge_port: Any, edge_name: str, switch_port: Any, transit_pool: Any, vlan: int
    ) -> _ExitLink | None:
        """Address one cabled link in the tenant VRF: a /31 from its transit pool, a dot1q sub-interface per end.

        The /31 is keyed by the link (its two interface ids), so a re-run gets the same one. Both
        sub-interfaces are ``<port>.<vlan>`` with the port's own role, the convention of a handoff circuit's
        ends (``eth0.1900``, add_virtual_circuit); the switch takes the lower address.
        """
        switch_name = switch_port.device.display_label
        prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=transit_pool,
            identifier=f"wan-exit__{'__'.join(sorted([edge_port.id, switch_port.id]))}",
            prefix_length=_WAN_EXIT_LINK_PREFIX_LENGTH,
            member_type="address",
            data={"role": "technical", "is_pool": True, "description": f"WAN exit {switch_name} <-> {edge_name}"},
        )
        # allocate_next_ip_prefix() is a pool mutation, not a save(): the prefix joins the run's group here.
        self.client.group_context.related_node_ids.append(prefix.id)
        switch_address, edge_address = await self.upsert_p2p_addresses(
            prefix, description=f"WAN exit {switch_name} <-> {edge_name}"
        )
        switch_sub = await self.ensure_vlan_subinterface(
            device_id=switch_port.device.id,
            device_name=switch_name,
            trunk_iface=switch_port,
            vlan_id_value=vlan,
            ip_address_id=switch_address.id,
            role=switch_port.role.value,
        )
        edge_sub = await self.ensure_vlan_subinterface(
            device_id=edge_port.device.id,
            device_name=edge_name,
            trunk_iface=edge_port,
            vlan_id_value=vlan,
            ip_address_id=edge_address.id,
            role=edge_port.role.value,
        )
        if switch_sub is None or edge_sub is None:
            self.logger.warning(
                f"Office {self.fabric_name}: no sub-interface for the WAN exit {switch_name} <-> {edge_name}"
            )
            return None
        return _ExitLink(
            switch_name=switch_name,
            switch_id=switch_port.device.id,
            switch_sub=switch_sub,
            switch_address=switch_address,
            edge_sub=edge_sub,
            edge_address=edge_address,
        )

    async def _peer_exit_links(self, *, links: list[_ExitLink], edge: Any, edge_name: str) -> None:
        """One BGP process per distribution-switch, one on the edge, one eBGP peering per link.

        The two AS numbers are found by description before one is drawn (a Number pool is not idempotent
        per run), the key is the shared find-or-create password helper's. Processes and peerings are
        passed by id, never looked up by name (search-index lag on same-run objects).
        """
        office_name = self.data["name"]
        key_id = await self._ensure_routing_password(
            name=f"{self.fabric_name}-wan-exit-key",
            description=f"eBGP auth key of the WAN exit of office {office_name}",
        )
        if key_id is None:
            return
        asn_pool = await self.client.get(kind=CoreNumberPool, name__value=_WAN_EXIT_ASN_POOL)
        switch_as_id = await self.ensure_shared_as(
            description=f"{self.fabric_name} WAN exit ASN", asn_pool_id=asn_pool.id
        )
        edge_as_id = await self.ensure_shared_as(
            description=f"{self.fabric_name} SD-WAN edge ASN", asn_pool_id=asn_pool.id
        )
        if switch_as_id is None or edge_as_id is None:
            self.fail(f"Office {office_name}: no AS number for the WAN exit")
        unicast_id = await self.ensure_address_family(*IPV4_UNICAST_AF)
        loopbacks = await self.client.filters(
            kind=DcimVirtualInterface,
            device__ids=[link.switch_id for link in links],
            role__value="loopback",
            include=["device", "ip_address"],
        )
        router_ids = {
            loopback.device.id: loopback.ip_address.id for loopback in loopbacks if loopback.ip_address.initialized
        }

        # The edge has no loopback: its router-id is its first handoff address, as the gateway's is.
        edge_process_id = await self._upsert_exit_process(
            device_name=edge_name,
            device_id=edge.id,
            local_as_id=edge_as_id,
            router_id=links[0].edge_address.id,
        )
        for link in links:
            router_id = router_ids.get(link.switch_id)
            if router_id is None:
                self.logger.warning(
                    f"Office {office_name}: {link.switch_name} has no addressed loopback — no WAN exit BGP"
                )
                continue
            switch_process_id = await self._upsert_exit_process(
                device_name=link.switch_name, device_id=link.switch_id, local_as_id=switch_as_id, router_id=router_id
            )
            await self.upsert_bgp_peering(
                # Peerings share a name space with circuits: neither side's bare name.
                name=f"WAN-EXIT-{link.switch_name}-{edge_name}",
                description=f"WAN exit {link.switch_name} <-> {edge_name} (eBGP in the tenant VRF)",
                status="active",
                session_type="EBGP",
                peering_role="regular",
                process_ids=[switch_process_id, edge_process_id],
                address_family_ids=[unicast_id],
                interface_ids=[link.switch_sub.id, link.edge_sub.id],
                password_id=key_id,
            )
            self.logger.info(f"Office {office_name}: ensured WAN exit {link.switch_name} <-> {edge_name}")

    async def _upsert_exit_process(self, *, device_name: str, device_id: str, local_as_id: str, router_id: str) -> str:
        """Upsert the WAN exit's ManagedBGP process on one device; returns its id."""
        process = await self.client.create(
            kind=ManagedBGP,
            data={
                "name": f"{device_name}-bgp-wan-exit",
                "description": f"WAN exit eBGP process of {device_name}",
                "status": "active",
                "process_role": _WAN_EXIT_PROCESS_ROLE,
                "local_as": {"id": local_as_id},
                "router_id": {"id": router_id},
                "capabilities": [{"id": device_id}],
            },
        )
        await process.save(allow_upsert=True)
        return process.id
