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

The pool table (ColoPoolSlot) is colocation_pools.py and the EVPN Multi-Site and edge-pair steps are
colocation_evpn.py's mixin; ColocationFabricGenerator holds the one implementation; the two subclasses
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
  the colocation operator's cross-connects, modelled as TopologyCarrierCircuits
  (pure carriers) with the dci or handoff TopologySessionCircuit
  that rides them (see data/demos/30_all/08_interconnects/), and they need both
  endpoints, which a colocation-side generator cannot see; their sessions belong
  to the virtual circuit generator (add_virtual_circuit).

``technical_pool`` is created here because the zone owns it, but its first
consumer is that interconnect cabling, not the edge/service legs: those carry no
P2P addressing, matching dc.py's border-to-service cabling.

Registered as add_colocation_zone / add_virtual_colocation in .infrahub.yml.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, cast

from infrahub_sdk.exceptions import NodeNotFoundError

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import BORDER_ROLE_FOR_SERVICES, CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import FIREWALL_CONTEXT
from ..helpers.routing import loopback_prefix_length
from ..pools import PoolMixin
from ..protocols import TopologyColocationZone, TopologyVirtualColocation
from ..routing import RoutingMixin
from ..types import DeviceOptions
from .colocation_evpn import ColocationEvpnMixin
from .colocation_pools import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_TECHNICAL_PREFIX_LENGTH,
    ATTACHED_SLOTS,
    COLO_POOL_SLOTS,
    LOOPBACK_IPV6_SLOT,
    ColocationFabricData,
    ColoPoolSlot,
)

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

# The design maximum each role is held to (TopologyTemplateColocationZone).
_COLO_DESIGN_MAX_BY_ROLE = {"edge": "max_edges", "firewall": "max_firewalls", "load-balancer": "max_load_balancers"}


class ColocationFabricGenerator(
    ColocationEvpnMixin, PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator
):
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

    def _parent_pool_name(self, slot: ColoPoolSlot) -> str:
        """The pool this node's ``slot`` is sliced from: the metro's supernet, else the bootstrap pool."""
        if self._slices_from_metro:
            return slot.name((self.data.get("parent") or {})["name"].lower())
        return slot.bootstrap_pool

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
        parent = self._parent_pool_name(slot)
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
        # The parents of all five slots in one read, not one get per slice (a name not there yet keeps its retry).
        await self.preload_parent_pools(self._parent_pool_name(slot) for slot in COLO_POOL_SLOTS)
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
        self._edge_devices = {}
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
        Handoff kind) riding a TopologyCarrierCircuit — needs both endpoints
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
           TopologyCarrierCircuit carrying a dci or
           handoff TopologySessionCircuit (data/demos/30_all/08_interconnects/), never as a cable. That
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
