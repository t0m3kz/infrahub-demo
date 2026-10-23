"""Colocation zone and virtual colocation: the kit they declare and deploy.

A colocation zone (the cage, TopologyColocationZone) and a virtual colocation
(a provider-hosted presence such as an Equinix Network Edge or a Megaport MCR,
TopologyVirtualColocation) are the two tiers that own devices in a colocation
metro. The metro owns none (see colocation.py): it is the EVPN Multi-Site site
the zones and virtual colocations below it share.

Each declares ``fabric_templates: [{quantity: 2, role: edge, template: ...}]``
exactly like a DC declares its super-spines, and gets consistently named, correctly
rolled, template-instantiated devices with loopback + management addressing.
``deployment`` on every device created here is the zone's (or virtual
colocation's) id, and the names carry that name (``eg-fr201`` for edge 01 of
zone FR2, ``eg-am-ne01`` for the virtual colocation AM-NE). Metro, zone and
virtual colocation names share the device-name and pool-name space, so they are
unique across the three kinds. Kit is per zone because the zones of one metro
can be asymmetric (FR2 carries the edge pair and the firewalls, FR6 nothing); a
zone with no kit is legal and common, and its run is a no-op.

ColocationFabricGenerator holds the one implementation; the two subclasses
differ only in class attributes (the GraphQL root, the protocol class the pool
references are attached to, the template kind they may host, a label for logs).
A zone hosts physical templates (TemplateDcimPhysicalDevice), a virtual
colocation provider-hosted ones (TemplateCloudNetworkAppliance); an entry of the other kind
is skipped with a warning. That one rule is all that separates them: a virtual
colocation's devices are withheld from cabling and edge pairing by
_register_fabric_devices, so _cable_services and _ensure_edge_pair no-op for it by
construction and there is no separate "cabled" switch.

A virtual colocation's devices are CloudNetworkAppliances ordered under the presence's ``account``. An edge router is
an independent appliance; two firewalls form the usual ManagedFirewallHA (the Equinix cluster), which the provider
wires, so it gets no sync interface and no cable.

Scope note: every cable a run lays is internal to the zone, between two PHYSICAL
devices this generator itself just created: the HA sync link between a
firewall/load-balancer pair's members (create_devices()), and the leg from the
edge pair to that pair (_cable_services), plus the interlink between the two
edges (_ensure_edge_pair). Those conditions (our own hardware, one facility) are
what a DcimCable means, and they are what bounds this file. Two kinds of link
consequently do NOT appear:

* anything involving virtual kit, because a link between provider-hosted
  instances is a connection inside the provider's platform rather than fibre;
* anything leaving the zone, including the link to another zone of the same
  metro, the on-ramp's links to the DCs and cloud fabrics it fronts. Those are
  the colocation operator's cross-connects, modelled as TopologyPhysicalCircuits
  (pure carriers) with the TopologyInterconnectCircuit or TopologyHandoffCircuit
  that rides them (see data/demos/30_all/08_interconnects/), and they need both
  endpoints, which a colocation-side generator cannot see; their sessions belong
  to the virtual circuit generator (add_virtual_circuit).

``technical_pool`` is created here because the zone owns it, but its first
consumer is that interconnect cabling, not the edge/service legs: those carry no
P2P addressing, matching dc.py's border-to-service cabling.

Registered as add_colocation_zone / add_virtual_colocation in .infrahub.yml.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreNumberPool
from typing_extensions import TypedDict

from utils.data_cleaning import clean_data

from ..common import CommonGenerator, DeviceOptions
from ..connections import BORDER_ROLE_FOR_SERVICES, CablingMixin
from ..devices import DeviceMixin
from ..fabric_sizing import (
    COLO_ZONE_LOOPBACK_IPV6_BITS,
    COLO_ZONE_TECHNICAL_IPV6_BITS,
    colo_metro_loopback_bits,
    colo_metro_loopback_ipv6_bits,
    colo_metro_management_bits,
    colo_metro_technical_bits,
    colo_metro_technical_ipv6_bits,
    colo_zone_loopback_bits,
    colo_zone_management_bits,
    colo_zone_technical_bits,
    host_bits_to_prefix_length,
)
from ..firewall_context import FIREWALL_CONTEXT
from ..helpers import get_loopback_name
from ..helpers.routing import loopback_prefix_length, p2p_prefix_length
from ..pools import PoolMixin
from ..protocols import DcimVirtualInterface, ManagedBGP, TopologyColocationZone, TopologyVirtualColocation
from ..routing import EVPN_AF, RoutingMixin
from ..types import CablingOptions

# Roles a zone or virtual colocation can host. Deliberately NOT dc.py's
# _DC_VALID_FABRIC_ROLES: `edge` is valid here and invalid at DC level (a DC
# reaches the outside through its border-leafs), while the DC-only fabric tiers
# (super-spine, hyper-spine, border-leaf) are meaningless where there is no
# fabric.
_COLO_VALID_FABRIC_ROLES = frozenset({"edge", "firewall", "load-balancer"})

# Roles that are a service appliance hanging off the on-ramp rather than a
# router in it: HA-paired two devices at a time via the shared
# DeviceMixin.create_ha_role_devices() (also used by dc.py and pod.py for
# their own border-leaf/border-spine firewall/load-balancer pairs), and given
# no loopback, since they take no part in the underlay/overlay.
#
# The template a firewall/load-balancer entry points at MUST provide an
# interface with role=ha (CP-26000_FIREWALL's `sync`, PA-5260_FIREWALL's
# `HA1`): _ensure_ha_interfaces needs one per member to lay the sync cable, and
# logs an error (i.e. fails the generator task) when a physical member has
# none. SRX-1500_EDGE_FIREWALL, for instance, has only fxp0 + four downlinks and
# cannot be paired.
_COLO_SERVICE_ROLES = frozenset({"firewall", "load-balancer"})

# What kind of template each class may instantiate. The template itself says
# whether it is physical or a provider-hosted appliance (the queries select it as
# `template_kind`); the class says whether the node can hold that. A zone holds our
# own hardware in our own cage racks; a virtual colocation holds provider-hosted
# appliances (CloudNetworkAppliance) and has no racks of ours at all.
_COLO_TEMPLATE_KIND_PHYSICAL = "TemplateDcimPhysicalDevice"
_COLO_TEMPLATE_KIND_APPLIANCE = "TemplateCloudNetworkAppliance"

# The prefix lengths a zone or virtual colocation WITHOUT a design slices from the
# bootstrap pools (Loopback-IPv4 etc.): fixed, because there is no envelope to
# derive them from. With a design every size comes from generators/fabric_sizing.py
# instead (ColoPoolSlot.zone_length), exactly like a DC's.
_COLO_LOOPBACK_PREFIX_LENGTH = 28
_COLO_TECHNICAL_PREFIX_LENGTH = 26
_COLO_MANAGEMENT_PREFIX_LENGTH = 28

# EVPN Multi-Site border gateway (see _ensure_evpn). Every DC fabric this
# demo builds runs an IPv6 underlay, and a Multi-Site tunnel only forms between
# VTEPs of one address family, so the edge's VTEP is a dedicated IPv6
# loopback rather than its IPv4 Loopback0 (which stays the BGP router-id: a
# router-id is 32-bit and cannot be an IPv6 address). The slice that holds the
# VTEPs is the loopback-ipv6 slot below.
_COLO_VTEP_LOOPBACK_INDEX = 1
# The two edges of a zone share no fabric: one direct link between them (port
# role `interlink`, N9K-C9316D-GX_EDGE's Ethernet1/8) carries a /127 and the
# iBGP FABRIC session (see _ensure_edge_pair). The technical-ipv6 slot holds eight
# /127s, far more than the one link needs.
_COLO_INTERLINK_ROLE = "interlink"
_COLO_P2P_IPV6_PREFIX_LENGTH = p2p_prefix_length("ipv6")
_COLO_FABRIC_UNICAST_AF = ("ipv6", "unicast")

# The design maximum each role is held to (TopologyTemplateColocationZone).
_COLO_DESIGN_MAX_BY_ROLE = {"edge": "max_edges", "firewall": "max_firewalls", "load-balancer": "max_load_balancers"}

# Host-bit sizing functions of generators/fabric_sizing.py, by tier.
ZoneBits = Callable[[Mapping[str, Any]], int]
MetroBits = Callable[[Mapping[str, Any]], int]


@dataclass(frozen=True)
class ColoPoolSlot:
    """One address block of the colocation hierarchy, in both of its tiers.

    The metro holds a SUPERNET per slot (a prefix pool named ``{metro}-{suffix}``,
    sized by ``metro_bits`` for max_zones zones of the largest zone design) and each
    zone or virtual colocation holds a SLICE (a pool named ``{zone}-{suffix}``,
    sized by ``zone_bits`` from its own design) cut out of that supernet, the way
    a pod slices its DC's pools. A zone or virtual colocation that is not under a
    metro with a design slices from the bootstrap pool ``bootstrap_pool``
    instead, with the design-derived size or, with no design either, ``fallback_length``.

    ``attribute`` is the relationship the pool is attached to on the owner (the
    metro and the zone use the same three names); the IPv6 slots have none, they are
    found by name.
    """

    suffix: str
    bootstrap_pool: str
    role: str
    zone_kind: Literal["address", "prefix"]
    ipv6: bool
    fallback_length: int
    zone_bits: ZoneBits
    metro_bits: MetroBits
    attribute: str | None = None

    def name(self, owner: str) -> str:
        """The pool this slot is on ``owner`` (a lower-case metro, zone or virtual colocation name)."""
        return f"{owner}-{self.suffix}"

    def zone_length(self, design: Mapping[str, Any] | None) -> int:
        """Prefix length of the zone's slice: from its design, else the fixed fallback."""
        if not design:
            return self.fallback_length
        return host_bits_to_prefix_length(self.zone_bits(design), ipv6=self.ipv6)

    def metro_length(self, design: Mapping[str, Any]) -> int:
        """Prefix length of the metro's supernet, from the metro design."""
        return host_bits_to_prefix_length(self.metro_bits(design), ipv6=self.ipv6)


def _zone_vtep_bits(_design: Mapping[str, Any]) -> int:
    return COLO_ZONE_LOOPBACK_IPV6_BITS


def _zone_interlink_bits(_design: Mapping[str, Any]) -> int:
    return COLO_ZONE_TECHNICAL_IPV6_BITS


LOOPBACK_SLOT = ColoPoolSlot(
    suffix="loopback-pool",
    bootstrap_pool="Loopback-IPv4",
    role="loopback",
    zone_kind="address",
    ipv6=False,
    fallback_length=_COLO_LOOPBACK_PREFIX_LENGTH,
    zone_bits=colo_zone_loopback_bits,
    metro_bits=colo_metro_loopback_bits,
    attribute="loopback_pool",
)
MANAGEMENT_SLOT = ColoPoolSlot(
    suffix="management-pool",
    bootstrap_pool="Management-IPv4",
    role="management",
    zone_kind="address",
    ipv6=False,
    fallback_length=_COLO_MANAGEMENT_PREFIX_LENGTH,
    zone_bits=colo_zone_management_bits,
    metro_bits=colo_metro_management_bits,
    attribute="management_pool",
)
TECHNICAL_SLOT = ColoPoolSlot(
    suffix="technical-pool",
    bootstrap_pool="Technical-IPv4",
    role="technical",
    zone_kind="prefix",
    ipv6=False,
    fallback_length=_COLO_TECHNICAL_PREFIX_LENGTH,
    zone_bits=colo_zone_technical_bits,
    metro_bits=colo_metro_technical_bits,
    attribute="technical_pool",
)
# The IPv6 slots are sized by what they hold (one VTEP /128 per edge, one /127 for the
# interlink), not by the design, so the zone's size is the same with or without one.
LOOPBACK_IPV6_SLOT = ColoPoolSlot(
    suffix="loopback-ipv6-pool",
    bootstrap_pool="Loopback-IPv6",
    role="loopback",
    zone_kind="address",
    ipv6=True,
    fallback_length=host_bits_to_prefix_length(COLO_ZONE_LOOPBACK_IPV6_BITS, ipv6=True),
    zone_bits=_zone_vtep_bits,
    metro_bits=colo_metro_loopback_ipv6_bits,
)
TECHNICAL_IPV6_SLOT = ColoPoolSlot(
    suffix="technical-ipv6-pool",
    bootstrap_pool="Technical-IPv6",
    role="technical",
    zone_kind="prefix",
    ipv6=True,
    fallback_length=host_bits_to_prefix_length(COLO_ZONE_TECHNICAL_IPV6_BITS, ipv6=True),
    zone_bits=_zone_interlink_bits,
    metro_bits=colo_metro_technical_ipv6_bits,
)
COLO_POOL_SLOTS = (LOOPBACK_SLOT, MANAGEMENT_SLOT, TECHNICAL_SLOT, LOOPBACK_IPV6_SLOT, TECHNICAL_IPV6_SLOT)
# The slots attached to the owner (zone, virtual colocation or metro) as a pool reference.
ATTACHED_SLOTS = (LOOPBACK_SLOT, MANAGEMENT_SLOT, TECHNICAL_SLOT)


class ColocationFabricData(TypedDict, total=False):
    """The subset of queries/topology/add/colocation_zone.gql and colocation_virtual.gql this generator reads."""

    id: str
    name: str
    parent: dict[str, Any]
    design: dict[str, Any] | None
    account: dict[str, Any] | None
    fabric_templates: list[dict[str, Any]]


class ColocationFabricGenerator(PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator):
    """Create the pools and the on-ramp devices a zone or virtual colocation declares.

    Not registered itself: ColocationZoneGenerator and VirtualColocationGenerator
    set the class attributes below.
    """

    data: ColocationFabricData
    # Set by the subclass.
    graphql_root_key: ClassVar[str]
    _node_kind: ClassVar[type[TopologyColocationZone] | type[TopologyVirtualColocation]]
    _template_kind: ClassVar[str]
    _label: ClassVar[str]

    async def generate(self, data: dict[str, Any]) -> None:
        # clean_data() unwraps the raw GraphQL edges/node/value nesting into the
        # flat dicts this generator reads, same first step as every other
        # generator here (see dc.py's generate()).
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

        # pod_name stays None: a zone has no pod tier, which is what makes
        # DeviceMixin.create_devices() fall back to the zone-scoped pool names
        # ("{fabric_name}-loopback-pool" / "-management-pool") that
        # _ensure_fabric_pools creates below.
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
        if self._slices_from_metro:
            # The metro's supernets are written by add_colocation_metro, whose
            # created-trigger can still be running (same race as the metro's ASN
            # pool, see _metro_asn_pool_id); _slice_pool retries the lookup by name.
            await self.wait_for_parent_generators(("add_colocation_metro",), metro["id"])
        await self._ensure_fabric_pools()
        physical_names_by_role = await self._create_fabric_devices(templates=templates)
        if physical_names_by_role.get("firewall"):
            # The zone's FirewallContext VLAN pool: customer_colocation.py gives
            # every footprint served by this zone a context on its firewall
            # pair and takes the context's transit VLAN from this pool by name
            # (the legs' addresses come from the per-VRF FW-Transit-* bootstrap
            # pools). Only a zone with physical firewalls gets it: without one
            # no context is ever created. See PoolMixin.ensure_context_vlan_pool.
            await self.ensure_context_vlan_pool(name=self.fabric_name, spec=FIREWALL_CONTEXT)
        await self._cable_services(physical_names_by_role=physical_names_by_role)
        overlay_process_ids = await self._ensure_evpn()
        await self._ensure_edge_pair(
            physical_edge_names=physical_names_by_role.get("edge", []), overlay_process_ids=overlay_process_ids
        )

    def _valid_fabric_templates(self) -> list[dict[str, Any]]:
        """Drop entries this generator cannot act on, warning per entry.

        warning(), not error(): one bad entry must not stop the other roles from
        being created (FailOnErrorLogger turns error() into a task failure),
        matching dc.py's _validate_fabric_template_roles.
        """
        usable: list[dict[str, Any]] = []
        for entry in self.data.get("fabric_templates", []):
            role = entry.get("role")
            if role not in _COLO_VALID_FABRIC_ROLES:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry with role={role!r} is not "
                    f"valid here (expected one of {sorted(_COLO_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            if not entry.get("quantity", 0) > 0:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry for role={role!r} has "
                    f"quantity {entry.get('quantity')!r} — skipping this entry."
                )
                continue
            template = entry.get("template")
            if not template:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry for role={role!r} has "
                    "no template — skipping this entry."
                )
                continue
            template_kind = template.get("template_kind")
            if template_kind != self._template_kind:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry for role={role!r} uses a "
                    f"{template_kind or 'unknown'} template, which a {self._label} cannot host (expected "
                    f"{self._template_kind}) — skipping this entry."
                )
                continue
            usable.append(entry)
        return self._within_design(self._drop_uncablable_service_entries(usable))

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
        """Whether this node's pools are cut out of the metro's supernets.

        Only when BOTH tiers state an envelope: the metro's supernets exist only
        when the metro has a design, and are sized for the zone designs its design
        lists. Every other combination slices the bootstrap pools directly, with the
        sizes it has (ColoPoolSlot.zone_length)."""
        return bool(self._design and self._metro_design)

    def _within_design(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hold the entries to the design envelope, warning per finding (never an error).

        * no design: nothing bounds the kit, the entries pass (the fallback pool
          sizes are warned about once per run, see _generate_fabric);
        * a design not among the metro design's zone_templates: the metro's supernets
          were not sized for it (the check errors);
        * a role whose total quantity passes the design's maximum: the pools are
          sized for the envelope, so allocation beyond it would fail midway. The
          entries that would pass it are skipped, whole, before any device exists.
          They are judged in template-id order so a re-run keeps the same entries
          whatever order the query returns them in.
        """
        design = self._design
        if not entries or not design:
            return entries

        metro_design = self._metro_design
        if metro_design:
            allowed = sorted(template["name"] for template in metro_design["zone_templates"])
            if design["name"] not in allowed:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: design '{design['name']}' is not one of metro "
                    f"design '{metro_design['name']}' zone_templates {allowed}, so the metro's pools were not sized "
                    "for it."
                )

        total_by_role: dict[str, int] = {}
        skipped: set[int] = set()
        for index in sorted(
            range(len(entries)),
            key=lambda i: (str(entries[i]["template"].get("id")), entries[i]["quantity"], entries[i]["role"]),
        ):
            entry = entries[index]
            role = entry["role"]
            maximum = design[_COLO_DESIGN_MAX_BY_ROLE[role]]
            total = total_by_role.get(role, 0) + entry["quantity"]
            if total > maximum:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry for role={role!r} "
                    f"(quantity {entry['quantity']}) would bring the {role} total to {total}, past the "
                    f"{_COLO_DESIGN_MAX_BY_ROLE[role]}={maximum} of design '{design['name']}' its pools are sized "
                    "for — skipping this entry."
                )
                skipped.add(index)
                continue
            total_by_role[role] = total
        return [entry for index, entry in enumerate(entries) if index not in skipped]

    def _drop_uncablable_service_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop a physical firewall/load-balancer entry no physical edge template
        in this zone can cable to, warning why.

        _cable_services hangs each service role off a same-named port role
        (`firewall`, `load-balancer`) on the edge devices — see
        N9K-C9316D-GX_EDGE's Ethernet1/[15-16]=firewall. Without this check,
        an entry for a role no edge template exposes still passes every other
        check above, so create_devices() builds its HA pair — only for
        create_chain_cabling to fail the whole generator task afterwards with
        "cannot cable ... load-balancer_ports=0", leaving a real, uncabled
        appliance behind. Catching it here instead means the entry is
        dropped, with a clear reason, before any device for it exists — same
        as every other drop above.

        Virtual entries are exempt: a virtual edge/service pair is never
        cabled in the first place (see _register_fabric_devices), so its
        template carries no `interfaces` to check against.

        No physical edge entry at all is a DIFFERENT, already-handled case —
        _cable_services's own no-op guard covers "services declared with no
        edge pair to hang them off" — so this check does nothing when there is
        none, rather than dropping every service entry.
        """
        physical_edge_entries = [
            entry
            for entry in entries
            if entry.get("role") == "edge"
            and (entry.get("template") or {}).get("template_kind") == _COLO_TEMPLATE_KIND_PHYSICAL
        ]
        if not physical_edge_entries:
            return entries

        physical_edge_port_roles: set[str] = set()
        for entry in physical_edge_entries:
            for iface in (entry.get("template") or {}).get("interfaces") or []:
                role = iface.get("role")
                if role:
                    physical_edge_port_roles.add(role)

        usable: list[dict[str, Any]] = []
        for entry in entries:
            role = entry.get("role")
            template = entry.get("template") or {}
            is_physical_service = role in _COLO_SERVICE_ROLES and template.get("template_kind") == (
                _COLO_TEMPLATE_KIND_PHYSICAL
            )
            if is_physical_service and role not in physical_edge_port_roles:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry for role={role!r} has no "
                    f"matching {role!r}-role port on any physical edge template in this {self._label} (available "
                    f"edge port roles: {sorted(physical_edge_port_roles) or 'none'}) — skipping this entry rather "
                    "than creating an appliance with nothing to cable it to. Point the edge template at a device "
                    f"with a {role!r}-role port block, or drop this entry."
                )
                continue
            usable.append(entry)
        return usable

    async def _slice_pool(self, slot: ColoPoolSlot) -> Any:
        """Upsert this node's pool for ``slot``: a slice of the metro's supernet or of a bootstrap pool.

        Sliced from the metro's supernet (``{metro}-{suffix}``) when both the metro
        and this node have a design (_slices_from_metro), otherwise from the
        bootstrap pool, and sized by the node's design or the fallback
        (ColoPoolSlot.zone_length).

        The slice is keyed by the pool's name, so a re-run gets the prefix it first
        got back. A design changed since then therefore does NOT resize an existing
        pool: the allocator returns the old prefix and only the pool's default
        length follows the new design. Resizing means deleting the pool and its
        prefix first; nothing here migrates.
        """
        if self._slices_from_metro:
            parent = slot.name((self.data.get("parent") or {})["name"].lower())
        else:
            parent = slot.bootstrap_pool
        try:
            return await self.ensure_sliced_pool(
                pool_name=slot.name(self.fabric_name),
                parent_pool_name=parent,
                prefix_length=slot.zone_length(self._design),
                role=slot.role,
                kind=slot.zone_kind,
            )
        except NodeNotFoundError:
            self.fail(
                f"Colocation {self._label} {self.fabric_name}: pool '{parent}' to slice "
                f"'{slot.name(self.fabric_name)}' from does not exist (add_colocation_metro creates the metro's "
                "supernets when the metro has a design)"
            )

    async def _ensure_fabric_pools(self) -> None:
        """Create this node's loopback/management/technical/VTEP pools and attach the first three.

        Written out rather than delegating to PoolMixin.allocate_resource_pools()
        for one concrete reason: that helper derives pool KIND from pool name
        (``is_prefix_pool = strategy == "fabric" and pool_name in ["technical",
        "loopback"]``), so a pool it names "{zone}-loopback-pool" is always a
        CoreIPPrefixPool. That is right for a DC, whose pods slice their own
        address pools out of it, but a zone has no pod tier — create_devices()
        resolves "{fabric_name}-loopback-pool" and hands it straight to
        allocate_next_ip_address(), which needs a CoreIPAddressPool. Asking
        allocate_resource_pools() for both a prefix pool and an address pool
        under the same name is not expressible, so the pools are built here
        with explicit kinds.

        Each pool is a SLICE (_slice_pool) of the metro's supernet or of a global
        bootstrap pool (Loopback-IPv4 etc., data/bootstrap/17_ip_prefix_pools.yml),
        never a fresh top-level supernet invented at runtime — the bug that rule
        exists to avoid (a runtime-created IpamPrefix only exists on its creating
        branch, so re-running on another branch found the pool by name but could
        not resolve its resource, and every allocation failed with "No more
        resources available"). The metro's supernets are themselves slices of the
        bootstrap pools (colocation.py).

        No "does it already exist" short-circuit: allocate_next_ip_prefix()'s
        identifier makes each slice idempotent and the pools' unique names plus
        save(allow_upsert=True) make the pools themselves idempotent, so
        re-running always converges instead of being able to permanently skip
        healing a pool left broken by an earlier code version.

        The management pool is needed whenever there is any kit, since
        create_devices() always resolves "{fabric_name}-management-pool".
        """
        name = self.fabric_name

        # Serialised like PoolMixin.allocate_resource_pools: two overlapping runs of one zone (the created
        # trigger and an updated(fabric_templates) one) would otherwise both find no pool and both create a
        # twin that hands out the same addresses. Zone, VC and metro names are unique across the three kinds,
        # so one key per name is enough.
        refs: dict[str, str | None] = {}
        async with self.resource_lock(f"colocation-pools-{name}"):
            for slot in ATTACHED_SLOTS:
                pool = await self._slice_pool(slot)
                refs[cast(str, slot.attribute)] = pool.id
            self._vtep_pool = await self._slice_pool(LOOPBACK_IPV6_SLOT)

        await self.attach_refs(kind=self._node_kind, node_id=self.data["id"], refs=refs)
        self.logger.info(f"Colocation {self._label} {name}: attached loopback/management/technical pool references")

    def _appliance_options(self) -> DeviceOptions:
        """create_devices() options of a provider-hosted appliance: the account it is ordered under.

        An edge router is an independent appliance (no HA domain). A firewall pair keeps its usual ManagedFirewallHA,
        whose default mode (active-passive) is the Equinix cluster; the provider wires it, so it gets no sync
        interface and no cable (DeviceMixin._ensure_ha_pairs).
        """
        options = DeviceOptions(appliance=True)
        account_id = (self.data.get("account") or {}).get("id")
        if account_id:
            options["account_id"] = account_id
        return options

    async def _create_fabric_devices(self, *, templates: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Instantiate every fabric_templates entry as this node's devices.

        Returns the created PHYSICAL device names keyed by role, so
        _cable_services can wire the service appliances to the on-ramp
        routers. Physical only, because that is all a DcimCable can join — virtual
        kit is created and then withheld, see _register_fabric_devices. Names
        accumulate per role rather than being overwritten, because nothing stops
        a zone declaring two entries for the same role (two firewall models, say).

        indexes=[] — an on-ramp is flat, with no fabric/pod/suite/row/rack path
        to encode, so the "standard" naming strategy yields
        "{role code}-{zone}{NN}" (e.g. ``eg-fr201``). Contrast dc.py, which
        passes the DC and pod indexes because a border-leaf's name records which
        pod's spines it cables to. The naming convention is the parent metro's.

        Two shapes of device come out of here, split on role exactly as dc.py
        splits its fabric tiers from its firewall/load-balancer pair:

          edge                    — a router in the on-ramp. Gets a /32 loopback,
                                    because it runs the routing that peers with
                                    the DCs it fronts.
          firewall, load-balancer — a service appliance hanging off that on-ramp.
                                    No loopback (it is not part of the underlay
                                    or overlay), HA-paired two-at-a-time via the
                                    shared DeviceMixin.create_ha_role_devices()
                                    — the same shape dc.py and pod.py use for
                                    their own border-leaf/border-spine pairs.
        """
        # Edge name -> platform, for _ensure_evpn's BGW setup.
        self._edge_devices: dict[str, str] = {}
        naming_convention = cast(
            Literal["standard", "hierarchical", "flat", "computed"],
            ((self.data.get("parent") or {}).get("naming_convention") or "standard").lower(),
        )

        physical_names_by_role: dict[str, list[str]] = {}

        for entry in (e for e in templates if e["role"] == "edge"):
            virtual = entry["template"].get("template_kind") == _COLO_TEMPLATE_KIND_APPLIANCE
            # Always allocated, virtual or physical: an edge router runs the
            # routing that peers with the DCs it fronts either way — an MCR
            # still has a loopback and still peers over it.
            options = DeviceOptions(
                indexes=[], allocate_loopback=True, loopback_prefix_length=loopback_prefix_length("ipv4")
            )
            if virtual:
                # Provider-hosted kit, so a CloudNetworkAppliance. No
                # hosting_device: the colocation operator's own platform runs
                # it (Network Edge, MCR), and that platform is not a device we
                # model — contrast dc.py's virtual firewall/load-balancer
                # instances (DcimVirtualDevice), which sit on physical hardware
                # we do own.
                options.update(self._appliance_options())
            names = await self.create_devices(
                deployment_id=self.data["id"],
                device_role="edge",
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_convention,
                options=options,
            )
            self._register_fabric_devices(physical_names_by_role, role="edge", virtual=virtual, names=names)
            platform = (entry["template"].get("platform") or {}).get("name") or ""
            self._edge_devices.update(dict.fromkeys(names, platform))

        for role in sorted(_COLO_SERVICE_ROLES):
            service_entries = [e for e in templates if e["role"] == role]
            if not service_entries:
                continue
            results = await self.create_ha_role_devices(
                role=cast(Literal["firewall", "load-balancer"], role),
                entries=service_entries,
                deployment_id=self.data["id"],
                naming_convention=naming_convention,
                indexes=[],
                appliance_template_kind=_COLO_TEMPLATE_KIND_APPLIANCE,
                appliance_options=self._appliance_options(),
            )
            for entry, names in results:
                virtual = entry["template"].get("template_kind") == _COLO_TEMPLATE_KIND_APPLIANCE
                self._register_fabric_devices(physical_names_by_role, role=role, virtual=virtual, names=names)

        return physical_names_by_role

    async def _metro_asn_pool_id(self) -> str:
        """The id of the parent metro's ASN pool, which the metro's one site AS is drawn from.

        The metro's own generator (add_colocation_metro) creates and attaches
        the pool, and a zone's created-trigger can fire before that task has
        finished. So the query's `parent.fabric_asn_pool` may still be empty:
        wait for the metro's generator, then look the pool up by name with the
        same bounded retry the DC-pool lookups use.
        """
        metro = self.data.get("parent") or {}
        pool = metro.get("fabric_asn_pool") or {}
        if pool.get("id"):
            return pool["id"]

        metro_name = metro.get("name", "")
        await self.wait_for_parent_generators(("add_colocation_metro",), metro["id"])
        try:
            found = await self._get_parent_pool_with_retry(f"{metro_name.lower()}-asn-pool", kind=CoreNumberPool)
        except NodeNotFoundError:
            self.fail(
                f"Colocation {self._label} {self.fabric_name}: metro {metro_name} has no ASN pool "
                f"'{metro_name.lower()}-asn-pool' (add_colocation_metro creates it) — cannot draw the metro's "
                "EVPN site AS"
            )
        return found.id

    async def _ensure_evpn(self) -> dict[str, str]:
        """Make this node's edges EVPN Multi-Site border gateways for the metro's site.

        A stretched segment reaches a colocation through the edge pair, which
        re-originates the DC's EVPN routes into the metro (and back) — the
        border-gateway role, with the METRO as the Multi-Site site: one site AS
        however many zones and virtual colocations carry edges. That needs three
        things the generic device pass does not create:

        * the metro's site ASN, attached to the metro as ``evpn_rt_as`` — the
          site-id and the admin field of its local route-targets, same role as a
          DC's fabric AS (PoolMixin.attach_refs, the same evpn_rt_as link a DC
          gets). Whichever zone or virtual colocation runs first draws it; the
          others find it by its description;
        * an overlay ManagedBGP per edge in that ASN, so the DCI peerings
          (add_virtual_circuit, for a TopologyInterconnectCircuit) have a
          process to attach to;
        * an IPv6 VTEP loopback per edge (LOOPBACK_IPV6_SLOT).

        The DCI sessions (add_virtual_circuit, generators/topology/virtual_circuit.py) and the
        shared Multi-Site VIP (data/demos/30_all/08_interconnects/
        01_colo_onramp/) are not built here: both need the DC end of the dark
        fibre, which this generator cannot see.

        Idempotent: the AS is found by its deterministic description before a
        new one is drawn from the metro's ASN pool (a from_pool on a new node
        would allocate a fresh ASN every run). ensure_shared_as does the
        find-or-create under a lock keyed by that description, which is the
        metro's name, so zones of one metro running concurrently never draw two.
        Every other object upserts by HFID.

        Returns the overlay process id of each edge it saved (edge name -> id),
        so _ensure_edge_pair references them directly instead of re-querying
        by name (search-index lag on objects created earlier in this run).
        """
        name = self.fabric_name
        process_ids: dict[str, str] = {}
        edge_devices: dict[str, str] = getattr(self, "_edge_devices", {})
        if not edge_devices:
            return process_ids
        metro = self.data["parent"]
        # Untracked (track=False): every zone and virtual colocation of the metro finds this one AS, so none of
        # their tracking groups may own it; one that stopped declaring edges would otherwise delete an AS its
        # sibling's overlay processes still use as local_as.
        site_as_id = await self.ensure_shared_as(
            description=f"{metro['name'].lower()} EVPN site ASN",
            asn_pool_id=await self._metro_asn_pool_id(),
            track=False,
        )
        await self.attach_refs(kind="TopologySegmentHosting", node_id=metro["id"], refs={"evpn_rt_as": site_as_id})

        loopbacks = await self.client.filters(
            kind=DcimVirtualInterface,
            device__name__values=sorted(edge_devices),
            role__values=["loopback", "loopback-vtep"],
            include=["device", "ip_address"],
        )
        by_device: dict[tuple[str, str], Any] = {
            (loopback.device.peer.name.value, loopback.role.value): loopback for loopback in loopbacks
        }
        for edge_name, platform in sorted(edge_devices.items()):
            router_loopback = by_device.get((edge_name, "loopback"))
            if router_loopback is None or not router_loopback.ip_address.initialized:
                self.logger.error(
                    f"Colocation {self._label} {name}: {edge_name} has no addressed loopback — skipping its EVPN process"
                )
                continue
            device = router_loopback.device.peer
            vtep = by_device.get((edge_name, "loopback-vtep"))
            vtep_ip: Any
            if vtep is not None and vtep.ip_address.initialized:
                vtep_ip = {"id": vtep.ip_address.id}
            else:
                vtep_ip = await self.client.allocate_next_ip_address(
                    resource_pool=self._vtep_pool,
                    identifier=f"{edge_name}-vtep",
                    prefix_length=128,
                    data={"description": f"EVPN VTEP loopback for {edge_name}"},
                )
            vtep_obj = await self.client.create(
                kind=DcimVirtualInterface,
                data={
                    **({"id": vtep.id} if vtep is not None else {}),
                    "name": get_loopback_name(platform, _COLO_VTEP_LOOPBACK_INDEX),
                    "description": "EVPN VTEP source (IPv6)",
                    "device": {"id": device.id},
                    "status": "active",
                    "role": "loopback-vtep",
                    "ip_address": vtep_ip,
                },
            )
            await vtep_obj.save(allow_upsert=True)
            bgp = await self.client.create(
                kind=ManagedBGP,
                data={
                    "name": f"{edge_name}-bgp-overlay",
                    "description": f"EVPN Multi-Site border-gateway process for {edge_name}",
                    "status": "active",
                    "local_as": {"id": site_as_id},
                    "router_id": {"id": router_loopback.ip_address.id},
                    "capabilities": [{"id": device.id}],
                    "process_role": "overlay",
                },
            )
            await bgp.save(allow_upsert=True)
            process_ids[edge_name] = bgp.id
            self.logger.info(f"Colocation {self._label} {name}: ensured EVPN VTEP + overlay BGP for {edge_name}")
        return process_ids

    async def _ensure_edge_pair(self, *, physical_edge_names: list[str], overlay_process_ids: dict[str, str]) -> None:
        """Link a zone's two physical edges and peer them over that link (iBGP FABRIC).

        The edges are single-box sites with no fabric between them, yet they
        share one Multi-Site site (same AS, same VIP): they need a direct
        link plus a directly connected iBGP session carrying IPv6 unicast
        (VTEP /128 reachability) and l2vpn evpn. A loopback EVPN session is
        impossible — the edges run no IGP.

        Only when EXACTLY two PHYSICAL edges exist in this zone: virtual edges
        (an MCR, a Network Edge) have no port to cable, and a zone with 0 or 1
        physical edge has nothing to pair (info log, no pair). More than two is
        not a pair either. The pair stays inside one zone: edges of two zones
        are joined by circuits, not by this link.

        Everything it writes is owned by this zone and keyed
        deterministically: the technical IPv6 slice, the P2P prefix (keyed by
        the link), the cable, the FABRIC-<a>-<b> peering and the zone's own
        routing key. One lock for the whole step, so overlapping runs of one
        zone never both create any of them.
        """
        name = self.fabric_name
        if len(physical_edge_names) != 2:
            self.logger.info(
                f"Colocation {self._label} {name}: {len(physical_edge_names)} physical edge(s) — an edge pair "
                "needs exactly 2, no interlink or FABRIC session"
            )
            return
        first, second = sorted(physical_edge_names)
        processes = [overlay_process_ids.get(first), overlay_process_ids.get(second)]
        if not all(processes):
            self.logger.warning(
                f"Colocation {self._label} {name}: {first}/{second} have no overlay BGP process from this run — "
                "skipping the edge pair (see the EVPN step above)"
            )
            return

        async with self.resource_lock(f"edge-pair-{name}"):
            pool = await self._slice_pool(TECHNICAL_IPV6_SLOT)
            legs = await self.create_chain_cabling(
                hops=[
                    {"devices": [first], "down_role": _COLO_INTERLINK_ROLE},
                    {"devices": [second], "up_role": _COLO_INTERLINK_ROLE},
                ],
                options=CablingOptions(pool=pool, p2p_prefix_length=_COLO_P2P_IPV6_PREFIX_LENGTH),
            )
            pairs = legs[0] if legs else []
            if not pairs:
                self.logger.warning(
                    f"Colocation {self._label} {name}: no {_COLO_INTERLINK_ROLE}-role port pair on "
                    f"{first}/{second} — no FABRIC session"
                )
                return
            interface_ids = [iface.id for pair in pairs for iface in pair]

            key_id = await self._ensure_routing_password(
                name=f"{name}-fabric-key", description=f"iBGP FABRIC session auth key for {self._label} {name}"
            )
            if key_id is None:
                return
            address_families = [
                await self.ensure_address_family(*_COLO_FABRIC_UNICAST_AF),
                await self.ensure_address_family(*EVPN_AF),
            ]
            await self.upsert_bgp_peering(
                name=f"FABRIC-{first}-{second}",
                description=(
                    f"Colocation {self._label} {name} edge pair {first} <-> {second}: "
                    "IPv6 unicast + EVPN over the interlink"
                ),
                status="active",
                session_type="IBGP",
                peering_role="fabric",
                process_ids=[str(process_id) for process_id in processes],
                address_family_ids=address_families,
                password_id=key_id,
                interface_ids=interface_ids,
            )
            self.logger.info(
                f"Colocation {self._label} {name}: ensured edge pair {first} <-> {second} (interlink + FABRIC session)"
            )

    def _register_fabric_devices(
        self,
        physical_names_by_role: dict[str, list[str]],
        *,
        role: str,
        virtual: bool,
        names: list[str],
    ) -> None:
        """Log one fabric_templates entry's just-created devices and, if
        physical, accumulate their names for _cable_services.

        Virtual devices are logged and then deliberately withheld from that
        accumulation. A DcimCable is fibre someone pulls between two chassis
        in one room; these are rented instances on the colocation operator's
        own platform, and the link between them is a connection inside that
        platform, not a cable. It is a real link and it is not modelled here:
        there is no virtual-link node kind in the schema, and the thing that
        does express a link we do not own — a virtual circuit (Interconnect or
        Handoff kind) riding a TopologyPhysicalCircuit — needs both endpoints
        and a provider, which is the virtual circuit generator's input, not
        this one's.

        This withholding is also what makes a virtual colocation need no flag:
        its templates are all virtual, so nothing reaches cabling or edge
        pairing. Not merely cosmetic: PA-VM_EDGE_CUSTOMER_* exposes `uplink` on
        eth[1-6], the exact role _cable_border_services claims on the service
        side, so without this a virtual colocation declaring an edge+firewall
        pair would reach create_chain_cabling and fail the task on
        C8000V_EDGE's missing `firewall` ports.
        """
        kind_label = "virtual" if virtual else "physical"
        self.logger.info(
            f"Colocation {self._label} {self.fabric_name}: created {len(names)} {kind_label} {role} device(s): {names}"
        )
        if virtual:
            self.logger.info(
                f"Colocation {self._label} {self.fabric_name}: {role} kit is virtual, so it is not cabled — a link "
                "between provider-hosted instances is a connection inside the provider's platform, not a DcimCable"
            )
            return
        physical_names_by_role.setdefault(role, []).extend(names)

    async def _cable_services(self, *, physical_names_by_role: dict[str, list[str]]) -> None:
        """Cable this zone's firewall/load-balancer pair to its edge pair.

        The same leg dc.py builds between its border-leafs and its shared
        services, and built with the same helper, because it is the same shape:
        the appliance is not in the underlay, it hangs off the tier that is.
        Index-paired (eg-fr201<->fw-fr201, eg-fr202<->fw-fr202), so each edge/service
        couple is one independent redundant path rather than an any-to-any mesh.

        When a DcimCable is the right model, and when it is not
        ------------------------------------------------------
        A DcimCable means fibre someone pulled between two chassis, so it is only
        correct when both ends are kit we own, in one room. Two conditions have to
        hold, and this method satisfies both by construction rather than by
        checking:

        1. Both ends are physical. Virtual kit never reaches here — the device
           pass drops it (see _register_fabric_devices), because a link between
           two provider-hosted instances lives inside the provider's platform.
        2. Both ends are in one facility: the edge pair and the appliances are
           all deployed to this one zone. A link that leaves the zone is the
           colocation operator's cross-connect, modelled as a
           TopologyPhysicalCircuit carrying a TopologyInterconnectCircuit or
           TopologyHandoffCircuit (data/demos/30_all/08_interconnects/), never as a cable. That
           is why the on-ramp's links to the DCs, the cloud fabrics it fronts and
           the other zones are absent here and not merely deferred.

        Always "pbr": a zone has no connectivity_mode attribute to read, and the
        DC's "inline" alternative needs a tier that both hands traffic to the
        appliance and takes it back (border-leaf), which an on-ramp router pair
        is not — it forwards on to a DC or a cloud fabric instead of returning
        it. So each service role gets its own independent leg off the edges.

        No P2P addressing, for the same reason dc.py's call passes no pool:
        _cable_border_services hands create_chain_cabling no CablingOptions, so
        _resolve_pool(None, fallback_name=None) returns None. The zone's
        technical_pool therefore still has no consumer here.
        """
        edge_names = physical_names_by_role.get("edge", [])
        firewall_names = physical_names_by_role.get("firewall", [])
        load_balancer_names = physical_names_by_role.get("load-balancer", [])
        if not edge_names or not (firewall_names or load_balancer_names):
            # Nothing to cable: a zone with no services, a virtual colocation
            # (all its kit is virtual), or — a real possibility worth not
            # crashing on — services declared with no edge pair to hang them
            # off. create_chain_cabling would no-op on the empty side anyway;
            # returning early keeps the log quiet.
            return

        # Role names on the EDGE side of each leg — BORDER_ROLE_FOR_SERVICES
        # (generators/connections.py), the same mapping dc.py and pod.py use
        # for their own border-leaf/border-spine: a `firewall`-role port
        # faces a firewall. N9K-C9316D-GX_EDGE provides Ethernet1/[15-16] for
        # that (see data/bootstrap/10_physical_devices_templates_cisco_nxos.
        # yaml) and provides no `load-balancer` ports at all — a zone
        # declaring a load-balancer against an edge template with no such
        # port never gets this far: _drop_uncablable_service_entries drops
        # that entry at validation time, before any device for it exists.
        await self._cable_border_services(
            border_role_for=BORDER_ROLE_FOR_SERVICES,
            connectivity_mode="pbr",
            border_names=edge_names,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )


class ColocationZoneGenerator(ColocationFabricGenerator):
    """add_colocation_zone: the kit of a colocation cage, physical templates only."""

    graphql_root_key = "TopologyColocationZone"
    _node_kind = TopologyColocationZone
    _template_kind = _COLO_TEMPLATE_KIND_PHYSICAL
    _label = "zone"


class VirtualColocationGenerator(ColocationFabricGenerator):
    """add_virtual_colocation: the kit of a provider-hosted presence, virtual templates only."""

    graphql_root_key = "TopologyVirtualColocation"
    _node_kind = TopologyVirtualColocation
    _template_kind = _COLO_TEMPLATE_KIND_APPLIANCE
    _label = "virtual colocation"
