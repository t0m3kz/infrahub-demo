"""Colocation zone and virtual colocation: the kit they declare (``fabric_templates``) and deploy; the metro owns no
devices. A zone hosts physical templates, a virtual colocation provider-hosted ones; the other kind is skipped with a
warning. Virtual devices are not cabled or edge-paired. Cables are internal to the zone; anything leaving it is a
TopologyCarrierCircuit. Pools: colocation_pools.py; EVPN: colocation_evpn.py.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, cast

from infrahub_sdk.exceptions import NodeNotFoundError

from generators.colocation.colocation_evpn import ColocationEvpnMixin
from generators.colocation.colocation_pools import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_TECHNICAL_PREFIX_LENGTH,
    ATTACHED_SLOTS,
    COLO_POOL_SLOTS,
    LOOPBACK_IPV6_SLOT,
    ColocationFabricData,
    ColoPoolSlot,
)
from generators.helpers.routing import loopback_prefix_length
from generators.mixins.common import CommonGenerator
from generators.mixins.connections import CablingMixin
from generators.mixins.devices import DeviceMixin
from generators.mixins.firewall_context import FIREWALL_CONTEXT
from generators.mixins.pools import PoolMixin
from generators.mixins.routing import RoutingMixin
from generators.mixins.types import DeviceOptions
from generators.protocols import TopologyColocationZone, TopologyVirtualColocation
from utils.data_cleaning import clean_data
from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES

# Roles a zone or virtual colocation can host; unlike dc.py, `edge` is valid and DC-only fabric tiers are not
_COLO_VALID_FABRIC_ROLES = frozenset({"edge", "internal-firewall", "load-balancer"})

# Service appliances hanging off the on-ramp, HA-paired by DeviceMixin.create_ha_role_devices() and given no loopback.
# Their template must provide a role=ha interface (_ensure_ha_interfaces needs one per member to lay the sync cable), so
# a model with no such port cannot be paired.
_COLO_SERVICE_ROLES = frozenset({"internal-firewall", "load-balancer"})

# The template kind each class may instantiate: a zone holds our hardware, a virtual colocation provider-hosted
# appliances
_COLO_TEMPLATE_KIND_PHYSICAL = "TemplateDcimPhysicalDevice"
_COLO_TEMPLATE_KIND_APPLIANCE = "TemplateCloudNetworkAppliance"

# The design maximum each role is held to (TopologyTemplateColocationZone).
_COLO_DESIGN_MAX_BY_ROLE = {
    "edge": "max_edges",
    "internal-firewall": "max_firewalls",
    "load-balancer": "max_load_balancers",
}


class ColocationFabricGenerator(
    ColocationEvpnMixin, PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator
):
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

        # pod_name stays None: a zone has no pod tier, so create_devices() uses the zone-scoped pool names from
        # _ensure_fabric_pools
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
            # The metro's supernets come from add_colocation_metro, which may still be running; _slice_pool retries by
            # name
            await self.wait_for_parent_generators(("add_colocation_metro",), metro["id"])
        await self._ensure_fabric_pools()
        physical_names_by_role = await self._create_fabric_devices(templates=templates)
        if physical_names_by_role.get("internal-firewall"):
            # The zone's FirewallContext VLAN pool (see PoolMixin.ensure_context_vlan_pool); only a zone with physical
            # firewalls gets one
            await self.ensure_context_vlan_pool(name=self.fabric_name, spec=FIREWALL_CONTEXT)
        await self._cable_services(physical_names_by_role=physical_names_by_role)
        overlay_process_ids = await self._ensure_evpn()
        await self._ensure_edge_pair(
            physical_edge_names=physical_names_by_role.get("edge", []), overlay_process_ids=overlay_process_ids
        )

    def _valid_fabric_templates(self) -> list[dict[str, Any]]:
        """Drop entries this generator cannot act on, warning per entry (warning, not error, so other roles are still
        created).
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
        """Whether this node's pools are cut out of the metro's supernets: only when both tiers have a design."""
        return bool(self._design and self._metro_design)

    def _within_design(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hold the entries to the design envelope, warning per finding (never an error); no design means entries pass.
        A role over the design maximum has its entries skipped whole, in template-id order, before any device exists."""
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
        """Drop a physical firewall/load-balancer entry that no physical edge template can cable to, warning why
        (virtual entries are exempt).
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
            port_role = BORDER_ROLE_FOR_SERVICES.get(str(role), str(role))
            if is_physical_service and port_role not in physical_edge_port_roles:
                self.logger.warning(
                    f"Colocation {self._label} {self.fabric_name}: fabric_templates entry for role={role!r} has no "
                    f"matching {port_role!r}-role port on any physical edge template in this {self._label} (available "
                    f"edge port roles: {sorted(physical_edge_port_roles) or 'none'}) — skipping this entry rather "
                    "than creating an appliance with nothing to cable it to. Point the edge template at a device "
                    f"with a {port_role!r}-role port block, or drop this entry."
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
        """Upsert this node's pool for ``slot``: a slice of the metro's supernet or of a bootstrap pool, keyed by pool
        name. A changed design does NOT resize an existing pool.
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
        """Create this node's loopback/management/technical/VTEP pools and attach the first three. Not
        PoolMixin.allocate_resource_pools(): a zone has no pod tier and needs an address pool under that name. Each pool
        is a slice of a metro supernet or bootstrap pool; re-runs converge.
        """
        name = self.fabric_name

        # Serialised like PoolMixin.allocate_resource_pools, so overlapping runs of a zone never create a twin pool
        # (names are unique across the kinds)
        refs: dict[str, str | None] = {}
        # The parents of all five slots in one read (a name not there yet keeps its retry)
        await self.preload_parent_pools(self._parent_pool_name(slot) for slot in COLO_POOL_SLOTS)
        async with self.resource_lock(f"colocation-pools-{name}"):
            for slot in ATTACHED_SLOTS:
                pool = await self._slice_pool(slot)
                refs[cast(str, slot.attribute)] = pool.id
            self._vtep_pool = await self._slice_pool(LOOPBACK_IPV6_SLOT)

        await self.attach_refs(kind=self._node_kind, node_id=self.data["id"], refs=refs)
        self.logger.info(f"Colocation {self._label} {name}: attached loopback/management/technical pool references")

    def _appliance_options(self) -> DeviceOptions:
        """create_devices() options of a provider-hosted appliance: the account it is ordered under (an HA pair gets no
        sync interface or cable).
        """
        options = DeviceOptions(appliance=True)
        account_id = (self.data.get("account") or {}).get("id")
        if account_id:
            options["account_id"] = account_id
        return options

    async def _create_fabric_devices(self, *, templates: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Instantiate every fabric_templates entry as this node's devices; returns created PHYSICAL names by role for
        _cable_services. indexes=[] (flat names ``{role code}-{zone}{NN}``); an edge gets a /32 loopback, a
        firewall/load-balancer is HA-paired without one.
        """
        # Edge name -> platform, for _ensure_evpn
        self._edge_devices = {}
        naming_convention = cast(
            Literal["standard", "hierarchical", "flat", "computed"],
            ((self.data.get("parent") or {}).get("naming_convention") or "standard").lower(),
        )

        physical_names_by_role: dict[str, list[str]] = {}

        for entry in (e for e in templates if e["role"] == "edge"):
            virtual = entry["template"].get("template_kind") == _COLO_TEMPLATE_KIND_APPLIANCE
            # Always allocated: a virtual edge (an MCR) still has a loopback and peers over it
            options = DeviceOptions(
                indexes=[], allocate_loopback=True, loopback_prefix_length=loopback_prefix_length("ipv4")
            )
            if virtual:
                # Provider-hosted kit: a CloudNetworkAppliance with no hosting_device (the operator's platform is not
                # modelled)
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
                role=cast(Literal["internal-firewall", "load-balancer"], role),
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
        """Log one fabric_templates entry's devices and, if physical, accumulate their names for _cable_services.
        Virtual devices are withheld: a DcimCable is fibre in one room, and their `uplink` ports would fail
        create_chain_cabling.
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
        """Cable this zone's firewall/load-balancer pair to its edge pair, index-paired like dc.py's border to services
        legs. Always "pbr" and no P2P addressing (no CablingOptions): a zone has no connectivity_mode.
        """
        edge_names = physical_names_by_role.get("edge", [])
        firewall_names = physical_names_by_role.get("internal-firewall", [])
        load_balancer_names = physical_names_by_role.get("load-balancer", [])
        if not edge_names or not (firewall_names or load_balancer_names):
            # Nothing to cable: no services, a virtual colocation, or services with no edge pair
            return

        # Edge-side role names of each leg (BORDER_ROLE_FOR_SERVICES, as dc.py and pod.py); a service role with no
        # matching edge port is dropped earlier by _drop_uncablable_service_entries
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
