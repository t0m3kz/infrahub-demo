"""Colocation metro bootstrap: pools + the metro's own on-ramp devices.

A colocation metro is the third deployment kind that owns physical devices
(after TopologyDataCenter and TopologyPod), and until now the only one whose
devices were hand-written YAML: data/demos/30_all/08_interconnects/ declared
raw DcimPhysicalDevice entries with hand-picked names, and
data/demos/16_hub_and_spoke/ went further and built a device called
"C004-EDGE1" out of SRX-1500_EDGE_FIREWALL (role=firewall, no dci ports),
i.e. a firewall template doing edge-router duty.

That is what this generator replaces: a metro declares
``fabric_templates: [{quantity: 2, role: edge, template: ...}]`` exactly like
a DC declares its super-spines, and gets consistently named, correctly rolled,
template-instantiated devices with loopback + management addressing.

The metro, not the cage, is the tier that declares and owns this kit, mirroring
dc.py's border-leafs: those are declared on the TopologyDataCenter rather than
on any one of its pods because they are a DC-wide tier, and on-ramp routers are
a metro-wide tier for the same reason. One pair fronts every cage in Frankfurt
and the other cages reach it over a cross-connect — nobody racks a router pair
in each cage they rent. So ``deployment`` on every device created here is the
metro's id, and the names carry the metro (``eg-fr01``), not a cage. Which cage
physically houses them matters only to the cross-connect, and a circuit already
records that on its own endpoints (TopologyPhysicalCircuit.locations, which
peer at the cage — see TopologyConnectableLocation).

Not every metro is one we own hardware in, which is what ``deployment_type`` is
for: a ``physical`` metro holds our own kit in our own cage racks, a ``virtual``
one holds provider-hosted instances instead (Equinix Network Edge, a Megaport
MCR) and has no racks of ours at all, and ``hybrid`` holds both. The template's
own kind decides whether a slot becomes a DcimPhysicalDevice or a
DcimVirtualDevice; deployment_type decides whether the metro may host that kind
at all. See _COLO_ALLOWED_TEMPLATE_KINDS.

Scope note — every cable a run lays is internal to the metro, between two
PHYSICAL devices this generator itself just created: the HA sync link between a
firewall/load-balancer pair's members (create_devices()), and the leg from the
edge pair to that pair (_cable_metro_services). Those two conditions — our own
hardware, one facility — are what a DcimCable means, and they are what bounds
this file. Two kinds of link consequently do NOT appear:

* anything involving virtual kit, because a link between provider-hosted
  instances is a connection inside the provider's platform rather than fibre;
* anything leaving the cage — the on-ramp's links to the DCs and cloud fabrics
  it fronts. Those are the colocation operator's cross-connects, modelled as
  TopologyPhysicalCircuits with per-customer TopologyVirtualCircuits riding them
  (see data/demos/30_all/08_interconnects/), and they need both endpoints, which
  a colocation-side generator cannot see — they belong to the interconnect
  generator.

``technical_pool`` is created here because the metro
owns it, but its first consumer is that interconnect cabling, not this file:
the intra-metro legs carry no P2P addressing, matching dc.py's border-to-service
cabling.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool
from typing_extensions import TypedDict

from utils.data_cleaning import clean_data

from ..common import CommonGenerator, DeviceOptions
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..helpers.pools import name_to_asn_range
from ..pools import PoolMixin
from ..protocols import TopologyColocationMetro

# Roles a colocation metro can host. Deliberately NOT dc.py's
# _DC_VALID_FABRIC_ROLES: `edge` is valid here and invalid at DC level (a DC
# reaches the outside through its border-leafs), while the DC-only fabric tiers
# (super-spine, hyper-spine, border-leaf) are meaningless in a metro that has no
# fabric.
_COLO_VALID_FABRIC_ROLES = frozenset({"edge", "firewall", "load-balancer"})

# device_role -> HA node kind, same mapping and same reason as dc.py's
# _HA_KIND_BY_ROLE: the pairing itself happens inside
# DeviceMixin.create_devices() (DeviceOptions.ha_kind), two devices at a time in
# name order, so only the right kind per role needs picking here. A role in this
# mapping is a service appliance hanging off the on-ramp rather than a router in
# it, which is also why it gets no loopback — see _create_metro_devices.
#
# The template a firewall/load-balancer entry points at MUST provide an
# interface with role=ha (CP-26000_FIREWALL's `sync`, PA-5260_FIREWALL's
# `HA1`): _ensure_ha_interfaces needs one per member to build the
# ManagedHAInterface pair and the sync cable, and logs an error — i.e. fails the
# generator task — when a physical member has none. SRX-1500_EDGE_FIREWALL, for
# instance, has only fxp0 + four downlinks and cannot be paired.
_COLO_HA_KIND_BY_ROLE: dict[str, str] = {
    "firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}

# Which device kind each metro strategy (TopologyColocationMetro.deployment_type)
# is allowed to instantiate. The template itself says whether it is physical or
# virtual — queries/topology/add/colocation_metro.gql selects it as
# `template_kind` — and deployment_type says whether the metro can hold that:
#
#   virtual  — provider-hosted kit only (Equinix Network Edge, a Megaport MCR).
#              We rent no cage space of our own in this metro, so nothing
#              physical can be mounted in it.
#   physical — our own hardware, in our own cage racks.
#   hybrid   — a metro with our own hardware that also fronts provider-hosted
#              virtual kit.
#
# deployment_type is the ONLY signal available at this point, and that is not a
# shortcut: a cage's LocationRacks are attached by a later file in the same load
# (data/demos/**/03_racks.yml sets `pod: <cage>`), so when the created-trigger
# fires this generator even a metro we own racks in still shows zero racks
# anywhere beneath it. Rack presence cannot be inspected here — the declared
# strategy stands in for it. Checking that the two actually agree belongs in
# checks/, against settled data.
_COLO_TEMPLATE_KIND_PHYSICAL = "TemplateDcimPhysicalDevice"
_COLO_TEMPLATE_KIND_VIRTUAL = "TemplateDcimVirtualDevice"
_COLO_ALLOWED_TEMPLATE_KINDS: dict[str, frozenset[str]] = {
    "virtual": frozenset({_COLO_TEMPLATE_KIND_VIRTUAL}),
    "physical": frozenset({_COLO_TEMPLATE_KIND_PHYSICAL}),
    "hybrid": frozenset({_COLO_TEMPLATE_KIND_PHYSICAL, _COLO_TEMPLATE_KIND_VIRTUAL}),
}
# Matches the schema's own default_value (schemas/extensions/topology/
# topology_colocation.yml), so a metro that never declares a strategy is treated
# as the hardware metro it almost always is.
_COLO_DEFAULT_DEPLOYMENT_TYPE = "physical"

# A metro's on-ramp is small and its addressing is not sliced further by any
# child tier, so these are fixed rather than design-driven (contrast dc.py,
# which reads every prefix length off its TopologyDataCenterDesign).
_COLO_LOOPBACK_PREFIX_LENGTH = 28
_COLO_TECHNICAL_PREFIX_LENGTH = 26
_COLO_MANAGEMENT_PREFIX_LENGTH = 28

# Metro ASN blocks are drawn from the same deterministic name-hash grid as DC
# fabric ASNs (helpers/pools.py's name_to_asn_range), so a metro can never
# collide with a DC or with another metro. max_pods=1 asks for the smallest
# block the grid offers — a metro hosts a handful of routers, not a fabric.
_COLO_ASN_MAX_PODS = 1


class TopologyColocationMetroData(TypedDict, total=False):
    """The subset of queries/topology/add/colocation_metro.gql this generator reads."""

    id: str
    name: str
    deployment_type: str
    naming_convention: str
    parent: dict[str, Any]
    loopback_pool: dict[str, Any] | None
    technical_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    asn_pool: dict[str, Any] | None
    fabric_templates: list[dict[str, Any]]


class ColocationMetroGenerator(PoolMixin, DeviceMixin, CablingMixin, CommonGenerator):
    """Create a colocation metro's resource pools and its own on-ramp devices."""

    data: TopologyColocationMetroData
    graphql_root_key = "TopologyColocationMetro"

    async def generate(self, data: dict[str, Any]) -> None:
        # clean_data() unwraps the raw GraphQL edges/node/value nesting into the
        # flat dicts this generator reads — same first step as every other
        # generator here (see dc.py's generate()).
        metros = clean_data(data).get(self.graphql_root_key, [])
        if not metros:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        for metro in metros:
            self.data = cast(TopologyColocationMetroData, metro)
            await self._generate_metro()

    async def _generate_metro(self) -> None:
        metro_id = self.data["id"]
        metro_name = self.data["name"]

        # pod_name stays None: a metro has no pod tier, which is what makes
        # DeviceMixin.create_devices() fall back to the metro-scoped pool names
        # ("{fabric_name}-loopback-pool" / "-management-pool") that
        # _ensure_colocation_pools creates below — see its docstring.
        self.fabric_name = metro_name.lower()
        self.pod_name = None
        self.deployment_id = metro_id

        deployment_type = self.data.get("deployment_type") or _COLO_DEFAULT_DEPLOYMENT_TYPE
        self.logger.info(f"Generating colocation metro {metro_name} (deployment_type={deployment_type})")

        templates = self._valid_fabric_templates()
        if not templates:
            self.logger.info(f"Metro {metro_name}: no usable fabric_templates entries — nothing to generate")
            return

        await self._ensure_colocation_pools(metro_id=metro_id)
        physical_names_by_role = await self._create_metro_devices(templates=templates, metro_id=metro_id)
        await self._cable_metro_services(physical_names_by_role=physical_names_by_role)

    def _valid_fabric_templates(self) -> list[dict[str, Any]]:
        """Drop entries this generator cannot act on, warning per entry.

        warning(), not error(): one bad entry must not stop the metro's other
        roles from being created (FailOnErrorLogger turns error() into a task
        failure), matching dc.py's _validate_fabric_template_roles.
        """
        deployment_type = self.data.get("deployment_type") or _COLO_DEFAULT_DEPLOYMENT_TYPE
        allowed_template_kinds = _COLO_ALLOWED_TEMPLATE_KINDS.get(deployment_type)
        if allowed_template_kinds is None:
            # Schema drift: a new deployment_type choice nobody taught this
            # generator about. Guessing a device kind is worse than building
            # nothing, so the metro becomes a no-op and says why.
            self.logger.warning(
                f"Metro {self.fabric_name}: unknown deployment_type {deployment_type!r} (expected one of "
                f"{sorted(_COLO_ALLOWED_TEMPLATE_KINDS)}) — skipping every fabric_templates entry rather "
                "than guessing whether its devices are physical or virtual."
            )
            return []

        usable: list[dict[str, Any]] = []
        for entry in self.data.get("fabric_templates", []):
            role = entry.get("role")
            if role not in _COLO_VALID_FABRIC_ROLES:
                self.logger.warning(
                    f"Metro {self.fabric_name}: fabric_templates entry with role={role!r} is not valid in a "
                    f"colocation metro (expected one of {sorted(_COLO_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            if not entry.get("quantity", 0) > 0:
                self.logger.warning(
                    f"Metro {self.fabric_name}: fabric_templates entry for role={role!r} has quantity "
                    f"{entry.get('quantity')!r} — skipping this entry."
                )
                continue
            template = entry.get("template")
            if not template:
                self.logger.warning(
                    f"Metro {self.fabric_name}: fabric_templates entry for role={role!r} has no template — "
                    "skipping this entry."
                )
                continue
            template_kind = template.get("template_kind")
            if template_kind not in allowed_template_kinds:
                self.logger.warning(
                    f"Metro {self.fabric_name}: fabric_templates entry for role={role!r} uses a "
                    f"{template_kind or 'unknown'} template, which a deployment_type={deployment_type!r} metro "
                    f"cannot host (allowed: {sorted(allowed_template_kinds)}) — skipping this entry. Either "
                    "point it at the other template kind or set the metro's deployment_type to 'hybrid'."
                )
                continue
            usable.append(entry)
        return usable

    async def _ensure_colocation_pools(self, *, metro_id: str) -> None:
        """Create this metro's loopback/management/technical/ASN pools and attach them.

        Written out rather than delegating to PoolMixin.allocate_resource_pools()
        for one concrete reason: that helper derives pool KIND from pool name
        (``is_prefix_pool = strategy == "fabric" and pool_name in ["technical",
        "loopback"]``), so a pool it names "{metro}-loopback-pool" is always a
        CoreIPPrefixPool. That is right for a DC, whose pods slice their own
        address pools out of it, but a metro has no pod tier — create_devices()
        resolves "{fabric_name}-loopback-pool" and hands it straight to
        allocate_next_ip_address(), which needs a CoreIPAddressPool. Asking
        allocate_resource_pools() for both a prefix pool and an address pool
        under the same name is not expressible, so the three pools are built
        here with explicit kinds.

        Each pool is a SLICE of a global bootstrap pool (Loopback-IPv4 etc.,
        data/bootstrap/17_ip_prefix_pools.yml), never a fresh top-level
        supernet invented at runtime — see dc.py's
        _ensure_firewall_context_pools for the bug that rule exists to avoid
        (a runtime-created IpamPrefix only exists on its creating branch, so
        re-running on another branch found the pool by name but could not
        resolve its resource, and every allocation failed with "No more
        resources available").

        No "does it already exist" short-circuit: allocate_next_ip_prefix()'s
        identifier makes each slice idempotent and the pools' unique names plus
        save(allow_upsert=True) make the pools themselves idempotent, so
        re-running always converges instead of being able to permanently skip
        healing a pool left broken by an earlier code version.
        """
        metro = self.fabric_name

        loopback_pool = await self._ensure_sliced_pool(
            pool_name=f"{metro}-loopback-pool",
            parent_pool_name="Loopback-IPv4",
            prefix_length=_COLO_LOOPBACK_PREFIX_LENGTH,
            role="loopback",
            kind="address",
        )
        management_pool = await self._ensure_sliced_pool(
            pool_name=f"{metro}-management-pool",
            parent_pool_name="Management-IPv4",
            prefix_length=_COLO_MANAGEMENT_PREFIX_LENGTH,
            role="management",
            kind="address",
        )
        technical_pool = await self._ensure_sliced_pool(
            pool_name=f"{metro}-technical-pool",
            parent_pool_name="Technical-IPv4",
            prefix_length=_COLO_TECHNICAL_PREFIX_LENGTH,
            role="technical",
            kind="prefix",
        )

        asn_start, asn_end = name_to_asn_range(dc_name=metro, max_pods=_COLO_ASN_MAX_PODS)
        await self.upsert_asn_pool(
            pool_name=f"{metro}-asn-pool",
            description=f"ASN pool for colocation metro {metro.upper()}",
            start_range=asn_start,
            end_range=asn_end,
            parent_kind="TopologyColocationMetro",
            parent_id=metro_id,
            parent_attr="asn_pool",
        )

        # One fetch + one save for the three IP pools (upsert_asn_pool already
        # attached its own). Plain save(), not allow_upsert=True: `node` is a
        # known-existing node, so update() sends only the modified fields —
        # allow_upsert=True routes through the Upsert mutation, which resends
        # every attribute and relationship and so re-fires any `updated`
        # trigger watching fabric_templates on every pool attach.
        node = await self.client.get(kind=TopologyColocationMetro, id=metro_id)
        if not node:
            self.logger.error(f"Metro {metro}: could not re-fetch metro {metro_id} to attach pool references")
            return
        node.loopback_pool = {"id": loopback_pool.id}
        node.management_pool = {"id": management_pool.id}
        node.technical_pool = {"id": technical_pool.id}
        await node.save()
        self.logger.info(f"Metro {metro}: attached loopback/management/technical pool references")

    async def _ensure_sliced_pool(
        self,
        *,
        pool_name: str,
        parent_pool_name: str,
        prefix_length: int,
        role: str,
        kind: Literal["address", "prefix"],
    ) -> Any:
        """Allocate one prefix out of a global bootstrap pool and wrap it in a pool."""
        parent_pool = await self._get_parent_pool_with_retry(parent_pool_name)
        allocated_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=parent_pool,
            identifier=pool_name,
            prefix_length=prefix_length,
            data={"role": role},
        )
        if kind == "address":
            pool = await self.client.create(
                kind=CoreIPAddressPool,
                data={
                    "name": pool_name,
                    "default_address_type": "IpamIPAddress",
                    "default_prefix_length": prefix_length,
                    "ip_namespace": {"hfid": ["default"]},
                    "identifier": pool_name,
                    "resources": [allocated_prefix],
                },
            )
        else:
            pool = await self.client.create(
                kind=CoreIPPrefixPool,
                data={
                    "name": pool_name,
                    "default_prefix_type": "IpamPrefix",
                    "default_prefix_length": prefix_length,
                    "ip_namespace": {"hfid": ["default"]},
                    "identifier": pool_name,
                    "resources": [allocated_prefix],
                },
            )
        await pool.save(allow_upsert=True)
        self.logger.info(f"- Created [{'CoreIPAddressPool' if kind == 'address' else 'CoreIPPrefixPool'}] {pool_name}")
        return pool

    async def _create_metro_devices(self, *, templates: list[dict[str, Any]], metro_id: str) -> dict[str, list[str]]:
        """Instantiate every fabric_templates entry as this metro's devices.

        Returns the created PHYSICAL device names keyed by role, so
        _cable_metro_services can wire the service appliances to the on-ramp
        routers. Physical only, because that is all a DcimCable can join — virtual
        kit is created and then withheld, see the branch below. Names accumulate
        per role rather than being overwritten, because nothing stops a metro
        declaring two entries for the same role (two firewall models, say).

        indexes=[] — a metro's on-ramp is flat, with no fabric/pod/suite/row/rack
        path to encode, so the "standard" naming strategy yields
        "{role code}-{metro}{NN}" (e.g. ``eg-fr01``). Contrast dc.py, which
        passes the DC and pod indexes because a border-leaf's name records which
        pod's spines it cables to.

        Two shapes of device come out of here, split on role exactly as dc.py
        splits its fabric tiers from its firewall/load-balancer pair:

          edge                    — a router in the on-ramp. Gets a /32 loopback,
                                    because it runs the routing that peers with
                                    the DCs it fronts.
          firewall, load-balancer — a service appliance hanging off that on-ramp.
                                    No loopback (it is not part of the underlay
                                    or overlay), and its devices are paired into
                                    an HA domain by create_devices() itself.
        """
        naming_convention = cast(
            Literal["standard", "hierarchical", "flat", "computed"],
            (self.data.get("naming_convention") or "standard").lower(),
        )

        physical_names_by_role: dict[str, list[str]] = {}
        for entry in templates:
            role = entry["role"]
            ha_kind = _COLO_HA_KIND_BY_ROLE.get(role)
            options = DeviceOptions(indexes=[])
            if ha_kind:
                # Pairing is two-at-a-time in name order, for any quantity — an
                # odd device is simply left unpaired, so a metro declaring
                # quantity 3 firewalls gets fw-fr01/fw-fr02 in a domain and
                # fw-fr03 standalone.
                options["ha_kind"] = ha_kind
            else:
                options["allocate_loopback"] = True
                options["loopback_prefix_length"] = 32
            virtual = entry["template"].get("template_kind") == _COLO_TEMPLATE_KIND_VIRTUAL
            if virtual:
                # Provider-hosted virtual kit, so a DcimVirtualDevice. No
                # hosting_device: the colocation operator's own platform runs
                # it (Network Edge, MCR), and that platform is not a device we
                # model — contrast dc.py's virtual firewall/load-balancer
                # instances, which sit on physical hardware we do own.
                options["virtual"] = True
            if role == "load-balancer":
                # The pre-existing group is "loadbalancers", not the
                # "load-balancers" create_devices() would derive from the role.
                options["group_name"] = "loadbalancers"
            names = await self.create_devices(
                deployment_id=metro_id,
                device_role=role,
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_convention,
                options=options,
            )
            kind_label = "virtual" if virtual else "physical"
            self.logger.info(f"Metro {self.fabric_name}: created {len(names)} {kind_label} {role} device(s): {names}")
            if virtual:
                # Deliberately withheld from the cabling pass. A DcimCable is
                # fibre someone pulls between two chassis in one room; these are
                # rented instances on the colocation operator's own platform, and
                # the link between them is a connection inside that platform, not
                # a cable. It is a real link and it is not modelled here: there is
                # no virtual-link node kind in the schema, and the thing that does
                # express a link we do not own — a TopologyVirtualCircuit over a
                # TopologyPhysicalCircuit — needs both endpoints and a provider,
                # which is the interconnect generator's input, not this one's.
                #
                # Not merely cosmetic: PA-VM_EDGE_CUSTOMER_* exposes `uplink` on
                # eth[1-6], the exact role _cable_border_services claims on the
                # service side, so without this a deployment_type=virtual metro
                # declaring an edge+firewall pair would reach create_chain_cabling
                # and fail the task on C8000V_EDGE's missing `firewall` ports.
                self.logger.info(
                    f"Metro {self.fabric_name}: {role} kit is virtual, so it is not cabled — a link between "
                    "provider-hosted instances is a connection inside the provider's platform, not a DcimCable"
                )
                continue
            physical_names_by_role.setdefault(role, []).extend(names)
        return physical_names_by_role

    async def _cable_metro_services(self, *, physical_names_by_role: dict[str, list[str]]) -> None:
        """Cable this metro's firewall/load-balancer pair to its edge pair.

        The same leg dc.py builds between its border-leafs and its shared
        services, and built with the same helper, because it is the same shape:
        the appliance is not in the underlay, it hangs off the tier that is.
        Index-paired (eg-fr01<->fw-fr01, eg-fr02<->fw-fr02), so each edge/service
        couple is one independent redundant path rather than an any-to-any mesh.

        When a DcimCable is the right model, and when it is not
        ------------------------------------------------------
        A DcimCable means fibre someone pulled between two chassis, so it is only
        correct when both ends are kit we own, in one room. Two conditions have to
        hold, and this method satisfies both by construction rather than by
        checking:

        1. Both ends are physical. Virtual kit never reaches here — the device
           pass drops it (see _create_metro_devices), because a link between two
           provider-hosted instances lives inside the provider's platform.
        2. Both ends are in one facility. A link that leaves the cage is the
           colocation operator's cross-connect, modelled as a
           TopologyPhysicalCircuit with per-customer TopologyVirtualCircuits
           riding it (data/demos/30_all/08_interconnects/), never as a cable. That
           is why the on-ramp's links to the DCs and cloud fabrics it fronts are
           absent here and not merely deferred.

        Condition 2 is an assumption, not an assertion, and worth naming as such:
        the on-ramp pair is declared on the metro and neither
        TopologyColocationMetro.fabric_templates nor the devices it creates carry
        a TopologyColocationZone, so nothing in the model says which cage holds
        them. The convention the demo data follows is that one cage holds the
        whole on-ramp (FR2 in Frankfurt, with FR6 customer cages only) and the
        other cages reach it over a cross-connect. Split an on-ramp across two
        cages and these cables would be wrong — but so would the single
        management/loopback pool they share, so the fix belongs in the schema
        (a zone on the kit) rather than in a guard here.

        Always "pbr": TopologyColocationMetro has no connectivity_mode attribute
        to read, and the DC's "inline" alternative needs a tier that both hands
        traffic to the appliance and takes it back (border-leaf), which an
        on-ramp router pair is not — it forwards on to a DC or a cloud fabric
        instead of returning it. So each service role gets its own independent
        leg off the edges.

        No P2P addressing, for the same reason dc.py's call passes no pool:
        _cable_border_services hands create_chain_cabling no CablingOptions, so
        _resolve_pool(None, fallback_name=None) returns None. The metro's
        technical_pool therefore still has no consumer here.
        """
        edge_names = physical_names_by_role.get("edge", [])
        firewall_names = physical_names_by_role.get("firewall", [])
        load_balancer_names = physical_names_by_role.get("load-balancer", [])
        if not edge_names or not (firewall_names or load_balancer_names):
            # Nothing to cable: a metro with no services (Paris), one whose kit is
            # all virtual (Amsterdam), or — a real possibility worth not crashing
            # on — services declared with no edge pair to hang them off.
            # create_chain_cabling would no-op on the empty side anyway; returning
            # early keeps the log quiet. Note this also covers the mixed case a
            # deployment_type=hybrid metro allows: physical services declared
            # against virtual edges leave edge_names empty, and the cage-crossing
            # question never arises because no cable is laid.
            return

        # Role names on the EDGE side of each leg. Deliberately the same values
        # dc.py uses for its border-leafs: a `firewall`-role port faces a
        # firewall. N9K-C9316D-GX_EDGE provides Ethernet1/[15-16] for that (see
        # data/bootstrap/10_physical_devices_templates_cisco_nxos.yaml) and
        # provides no `load-balancer` ports at all, so a metro declaring a
        # load-balancer against that template gets create_chain_cabling's
        # explicit "cannot cable ... load-balancer_ports=0" error rather than a
        # silently uncabled appliance. Point such a metro at an edge template
        # that has the ports.
        await self._cable_border_services(
            border_role_for={"firewall": "firewall", "load-balancer": "load-balancer"},
            connectivity_mode="pbr",
            border_names=edge_names,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )
