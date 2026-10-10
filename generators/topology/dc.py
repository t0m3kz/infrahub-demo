"""Infrastructure generator for data center topology."""

from typing import Any, Literal, cast

from typing_extensions import TypedDict

from utils.data_cleaning import clean_data

from ..common import CommonGenerator, DeviceOptions
from ..connections import BORDER_ROLE_FOR_SERVICES, CablingMixin
from ..dc_config import host_bits_to_prefix_length, resolve_dc_size_layout
from ..devices import DeviceMixin
from ..helpers import name_to_asn_range
from ..helpers.pools import FW_CONTEXT_VLAN_START
from ..helpers.routing import RoutingStrategy
from ..helpers.template_interfaces import template_interface_names_by_role
from ..pod_config import POD_LAYOUTS, templates_by_role
from ..pools import PoolMixin
from ..protocols import (
    TopologyDataCenter,
    TopologyPod,
)
from ..routing import RoutingMixin
from ..types import CablingOptions, NamingConvention, RoutingOptions, naming_convention_of

_DC_VALID_FABRIC_ROLES = frozenset({"super-spine", "hyper-spine", "border-leaf", "firewall", "load-balancer"})


# Strategies under which dc.py pre-seeds its own DC-scoped tiers' BGP.
_SEEDED_STRATEGIES = (
    RoutingStrategy.EBGP_EBGP.value,
    RoutingStrategy.EBGP_IBGP.value,
    RoutingStrategy.OSPF_IBGP.value,
)


class TopologyDcData(TypedDict, total=False):
    """Shape of one clean_data()-processed TopologyDeployment/TopologyDataCenter
    entry (see queries/topology/add/dc.gql) — declared here, at the read site,
    instead of a shared Pydantic model file. No runtime validation: a missing/
    mistyped key surfaces as ``KeyError`` at first read, caught by generate()'s
    own except clause below."""

    id: str
    name: str
    index: int
    size: str
    naming_convention: str
    connectivity_mode: str
    underlay_protocol: str
    routing_strategy: str
    fabric_templates: list[dict[str, Any]]
    loopback_pool: dict[str, Any] | None
    technical_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    fabric_asn_pool: dict[str, Any] | None
    children: list[dict[str, Any]]
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


class DCTopologyGenerator(PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator):
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
            # No Pydantic validation left to catch a malformed/partial GraphQL
            # response — force-read every field generate() treats as required
            # here, inside the try, so a missing one raises KeyError in the
            # same place the old DCModel(**deployment_list[0]) construction did.
            # naming_convention/connectivity_mode are read via .get() below
            # with the same defaults DCModel used to declare.
            dc_id = self.data["id"]
            dc_name = self.data["name"]
            dc_index = self.data["index"]
            dc_design = resolve_dc_size_layout(self.data["size"])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        # This DC's controllers (queries/topology/add/dc.gql's aliases), read
        # by create_devices() — see generators/devices.py's _resolve_role_controller.
        self.set_controllers_from(self.data)

        self.logger.info(f"Processing Data Center: {dc_name}")

        # include=["layout"] lets _generate_dc_scoped_fabric_devices read each
        # pod's own max_border_leafs_per_pod cap via _pod_border_leaf_capacity.
        # The pods are read, never tracked: they are data, not this run's
        # output, and a tracked pod that moved to another DC would be deleted
        # by this DC's next run.
        existing_pods = await self.client.filters(kind=TopologyPod, parent__ids=[dc_id], include=["layout"])
        self._existing_pods = existing_pods

        self.deployment_id = dc_id  # Store for cable linking
        self.fabric_name = dc_name.lower()
        self._validate_fabric_template_roles()
        super_spine_entries = templates_by_role(self.data.get("fabric_templates", []), "super-spine")
        amount_of_super_spines = sum(entry["quantity"] for entry in super_spine_entries)
        self.logger.info(f"Generating topology for data center {self.fabric_name.upper()}")

        if amount_of_super_spines > dc_design["max_super_spines_per_fabric"]:
            raise RuntimeError(
                f"DC {self.fabric_name.upper()} requests {amount_of_super_spines} super-spines but the assigned "
                f"design allows at most {dc_design['max_super_spines_per_fabric']}"
            )

        underlay_protocol = self.data.get("underlay_protocol", "ipv6")
        is_ipv6 = underlay_protocol == "ipv6"
        is_dual_stack = underlay_protocol == "dual_stack"

        # Prefix lengths come from the design; the DC instance can override by pre-attaching pools.
        # Values must already match the underlay_protocol (IPv4 or IPv6) — no conversion needed.
        # Management is always IPv4.
        technical_prefix = dc_design["technical_prefix_length"]
        loopback_prefix = dc_design["loopback_prefix_length"]
        management_prefix = dc_design["management_prefix_length"]

        # Always (re-)allocate every DC pool on every run — never skip an object just
        # because it already exists. allocate_resource_pools()'s identifier-keyed
        # allocation and the pool's own name-based upsert both make this idempotent,
        # and skipping a pool here means it never gets re-registered in this run's
        # group context, so the generator framework's delete-unused-nodes sync treats
        # it as orphaned and deletes it on the next run (matches pod.py/rack.py, which
        # never gate object creation on "does it already exist" either).
        pools_to_allocate: dict[str, int] = {
            "technical": technical_prefix,
            "loopback": loopback_prefix,
            "management": management_prefix,
        }

        # Super-spine and border-leaf devices share one DC-scoped loopback pool
        # (both are DC-level fabric tiers, cabled to spines rather than owned by
        # a pod's own loopback pool — border-leaf using its own pod's pool would
        # also race that pod's add_pod bootstrap, which creates it, during a bulk
        # load). Sized from the design's max caps (capacity, not live quantity) so
        # growing either tier later never exhausts it — same reasoning as every
        # other pool size in this generator, which are all design-capacity based.
        design_mode = "back-to-back" if dc_design["max_super_spines_per_fabric"] == 0 else "super-spine"
        max_super_spines_cap = dc_design["max_super_spines_per_fabric"]
        max_border_leafs_cap = dc_design["max_border_leafs_per_fabric"]
        max_hyper_spines_cap = dc_design["max_hyper_spines_per_fabric"]
        # Allocated whenever there's capacity for ANY DC-level tier that draws
        # from this pool — independent of design_mode. A back-to-back design
        # (no super-spine tier) can still have border-leaf capacity (e.g.
        # "M_BACK_TO_BACK"), and those border-leaf devices need a loopback IP
        # for overlay BGP just like they would under a super-spine design.
        if max_super_spines_cap > 0 or max_border_leafs_cap > 0 or max_hyper_spines_cap > 0:
            pools_to_allocate["dc-fabric-loopback"] = host_bits_to_prefix_length(
                dc_design["dc_fabric_loopback_host_bits"], ipv6=is_ipv6
            )

        self.logger.info(f"Allocating DC pools: {list(pools_to_allocate.keys())}")

        dc_pools = await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=pools_to_allocate,
            ipv6=is_ipv6,
            dual_stack=is_dual_stack,
        )

        # Derive deterministic ASN range from DC name (unique per site).
        # max_border_leafs_per_fabric is included since border-leaf devices draw
        # from this same fabric_asn_pool (see upsert_asn_pool below). super-spine
        # and per-pod spine ASNs are each shared (one ASN per group, not per
        # device — see generators/routing.py's shared_underlay_as_id), so they
        # no longer scale the block size by device count.
        max_pods = dc_design["max_pods"]
        max_border_leafs_per_fabric = dc_design["max_border_leafs_per_fabric"]
        asn_start, asn_end = name_to_asn_range(
            dc_name=dc_name,
            max_pods=max_pods,
            max_border_leafs_per_fabric=max_border_leafs_per_fabric,
        )

        # Only create ASN pool for eBGP-based strategies (one pool per DC, shared by
        # super-spine AND border-leaf devices)
        # ospf-ibgp uses OSPF underlay + shared overlay AS — no per-device pools needed
        routing_strategy = self.data.get("routing_strategy", "ebgp-ebgp")
        fabric_asn_pool_id: str | None = None
        if routing_strategy in (RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value):
            asn_pool_obj = await self.upsert_asn_pool(
                pool_name=f"{self.fabric_name}-asn-pool",
                description=f"ASN pool for {self.fabric_name.upper()} fabric",
                start_range=asn_start,
                end_range=asn_end,
            )
            if asn_pool_obj:
                fabric_asn_pool_id = asn_pool_obj.id

        # VlanSegment.vlan_id is set manually — no pool involved (single-site,
        # no collision-tracking concern requiring automated allocation).
        # VxlanSegment's LOCAL VLAN ID is allocated per VLAN domain (MLAG pair
        # or standalone device), not per-DC — see generators/devices.py's
        # _ensure_mlag_pairs/_ensure_standalone_vlan_domains and
        # generators/topology/segment.py's per-domain ManagedVlanDomainSegment
        # allocation. IEEE 802.1Q VLAN ID has only local significance; a
        # DC-wide pool would artificially cap the whole DC to one shared
        # ~3900-value space instead of ~3900 per independent VLAN domain.

        # L2 VNI pool for the VXLAN overlay (VRF-lite: no VRF stretches over
        # EVPN, so there's no L3 VNI pool — border-leaf VRFs are local-only).
        #
        # Capped at 39999 even though a VNI is a 24-bit field. Three independent
        # constraints, the first two of which the device enforces by rejecting
        # the line:
        #
        #   1. 16-bit RD/RT ceiling (65535). The EVPN route-distinguisher and
        #      route-target are derived from the VNI (transforms/helpers/vxlan.py),
        #      and every ASN here is a 4-byte private ASN — so both a type-1 RD
        #      (IPv4:assigned) and a type-2 RT (4-byte-ASN:assigned) leave only
        #      16 bits for the VNI.
        #   2. Must stay disjoint from the L3 VNI range (50001-59999, supplied
        #      by data — see data/demos/.../01_pools.yml `*-l3vni-pool`). The VNI
        #      space is a single flat namespace per device, NOT one namespace per
        #      VNI type, so an L2 segment allocated on top of a VRF's L3 VNI
        #      collides and the `member vni ... associate` line fails.
        #   3. Must stay disjoint from GLOBAL-L2VNI (40000-49999, data/bootstrap/
        #      18_vni_pools.yml), where stretched segments draw their one shared
        #      VNI. Every site pool hands out this same band, so a stretched VNI
        #      drawn from one site and reused in another would collide with that
        #      site's own local allocations (see generators/topology/segment.py).
        #
        # 39999 satisfies all three. 30k local segments per fabric is far beyond
        # any real fabric, so this costs nothing.
        vni_pool = await self.upsert_number_pool(
            pool_name=f"{self.fabric_name}-vni-pool",
            description=f"L2 VNI pool for {self.fabric_name.upper()}",
            start_range=10001,
            end_range=39999,
            node="ManagedSegmentDeployment",
            node_attribute="vni",
        )

        # Attach every pool reference to the DC with one fetch and one save,
        # every run — the pools above are the same upserted objects whether
        # just created or already there, so this is idempotent. Plain save():
        # dc is a known-existing node (just fetched) — allow_upsert=True would
        # route through create()'s Upsert mutation, which sends every
        # attribute/relationship (even unmodified ones), spuriously re-firing
        # unrelated `updated` triggers (fabric_templates/connectivity_mode/status)
        # on every pool attach and double-firing dc_pod_cascade. Untracked: the
        # DC is this run's target, not its output.
        dc = await self.client.get(kind=TopologyDataCenter, id=dc_id)
        if dc:
            pool_attr_map: dict[str, str] = {
                "loopback": "loopback_pool",
                "management": "management_pool",
                "technical": "technical_pool",
            }
            for pool_name, pool_obj in dc_pools.items():
                if pool_name in pool_attr_map:
                    setattr(dc, pool_attr_map[pool_name], {"id": pool_obj.id})
            if fabric_asn_pool_id:
                dc.fabric_asn_pool = {"id": fabric_asn_pool_id}
            dc.vni_pool = {"id": vni_pool.id}
            await dc.save(update_group_context=False)

        super_spine_names: list[str] = []
        if design_mode == "back-to-back":
            self.logger.info(
                f"DC {self.fabric_name}: design_mode=back-to-back — "
                "skipping super-spine tier, spines will connect directly across pods"
            )
        elif super_spine_entries:
            super_spine_names = await self._create_dc_tier_devices(
                "super-spine", super_spine_entries, indexes=[dc_index], dc_pools=dc_pools, is_ipv6=is_ipv6
            )

        # Hyper-spine: a 4th tier above super-spine, only present in XL fabrics
        # (design.max_hyper_spines_per_fabric > 0). Unlike super-spine (cabled
        # by pod.py against its own pod-scoped spines), hyper-spine is cabled
        # HERE, DC-to-DC-level, since both tiers are DC-scoped — dc.py never
        # cables its own tiers together anywhere else, this is the one case
        # where it does.
        hyper_spine_entries = templates_by_role(self.data.get("fabric_templates", []), "hyper-spine")
        amount_of_hyper_spines = sum(entry["quantity"] for entry in hyper_spine_entries)
        if amount_of_hyper_spines > max_hyper_spines_cap:
            raise RuntimeError(
                f"DC {self.fabric_name.upper()} requests {amount_of_hyper_spines} hyper-spines but the assigned "
                f"design allows at most {max_hyper_spines_cap}"
            )

        hyper_spine_names = await self._create_dc_tier_devices(
            "hyper-spine", hyper_spine_entries, indexes=[dc_index], dc_pools=dc_pools, is_ipv6=is_ipv6
        )

        # Create shared routing objects (overlay AS, OSPF area, and — for eBGP
        # underlay strategies — the single super-spine underlay AS shared
        # fabric-wide, .dev/bgp.txt) at the DC level so pod/rack generators
        # always find them and never create duplicates.
        # overlay_asn is asn_end + 1: outside the per-device pool range [asn_start, asn_end]
        # but still inside this DC's grid slot (see name_to_asn_range)
        super_spine_as_id = await self._create_shared_routing_objects(
            overlay_asn=asn_end + 1,
            asn_pool_id=fabric_asn_pool_id,
            deployment_id=dc_id,
        )

        # Pre-seed each DC-scoped tier's own BGP so it exists before any pod
        # generator (or the hyper-spine cabling below) runs: the later calls
        # treat these tiers as top_devices, which skips underlay/overlay BGP
        # creation for them. Super-spines pre-seed their overlay even under
        # ospf-ibgp, since they sit above the OSPF domain.
        if super_spine_names and routing_strategy in _SEEDED_STRATEGIES:
            await self._preseed_tier_routing(
                "super-spine",
                super_spine_names,
                routing_strategy=routing_strategy,
                asn_pool_id=fabric_asn_pool_id,
                shared_underlay_as_id=super_spine_as_id,
            )

        if hyper_spine_names and routing_strategy in _SEEDED_STRATEGIES:
            await self._preseed_tier_routing(
                "hyper-spine", hyper_spine_names, routing_strategy=routing_strategy, asn_pool_id=fabric_asn_pool_id
            )

            # Cable super-spine <-> hyper-spine full mesh (same "pod" strategy
            # PodCablingStrategy uses for spine<->super-spine — architecturally
            # identical fan-out, just one tier up and both ends DC-scoped).
            super_spine_uplink_interfaces = [
                iface_name
                for entry in super_spine_entries
                for iface_name in template_interface_names_by_role(
                    interfaces=entry["template"].get("interfaces", []),
                    role="uplink",
                )
            ]
            hyper_spine_downlink_interfaces = [
                iface_name
                for entry in hyper_spine_entries
                for iface_name in template_interface_names_by_role(
                    interfaces=entry["template"].get("interfaces", []),
                    role="downlink",
                )
            ]
            if super_spine_names and super_spine_uplink_interfaces and hyper_spine_downlink_interfaces:
                p2p_prefix_length = 127 if is_ipv6 else 31
                # Confirmed live on DC4: the DC-level technical pool itself is
                # NOT duplicated (allocate_resource_pools()'s own lock already
                # rules that out) — the race is here, in create_cabling()'s
                # P2P address creation. Two overlapping invocations of this
                # generator for the same DC (whatever fires them — this
                # section's only caller-visible clue is that it, not device
                # or border-leaf creation just above, is where the collision
                # lands) can both find the SAME two hyper-spine<->super-spine
                # interfaces still uncabled and both create the same
                # addresses for them; IpamIPAddress's uniqueness_constraint
                # is only enforced asynchronously, so both writes succeed and
                # only the later "Process schema integrity" check catches it.
                # Serialize on dc_id: only one overlapping caller ever cables
                # this DC's hyper-spine mesh; the other finds every interface
                # already cabled and does nothing.
                async with self.resource_lock(f"hyperspine-cabling-{dc_id}"):
                    p2p_pairs = await self.create_cabling(
                        bottom_devices=super_spine_names,
                        bottom_interfaces=super_spine_uplink_interfaces,
                        top_devices=hyper_spine_names,
                        top_interfaces=hyper_spine_downlink_interfaces,
                        strategy="pod",
                        options=CablingOptions(
                            pool=dc_pools.get("technical"),
                            p2p_prefix_length=p2p_prefix_length,
                        ),
                    )
                await self.create_routing(
                    bottom_devices=super_spine_names,
                    top_devices=hyper_spine_names,
                    options=RoutingOptions(design=self.data, asn_pool=fabric_asn_pool_id),
                    p2p_interfaces=p2p_pairs,
                    bottom_role="super-spine",
                    top_role="hyper-spine",
                )
            elif super_spine_names:
                self.logger.error(
                    f"DC {self.fabric_name}: cannot cable super-spine<->hyper-spine — "
                    f"super_spine_uplinks={len(super_spine_uplink_interfaces)}, "
                    f"hyper_spine_downlinks={len(hyper_spine_downlink_interfaces)}."
                )

        # Fan-out to every pod's own add_pod run is handled by the sibling
        # dc_pod_cascade generator, not here — see that module's docstring for why
        # (add_pod's own fan-out to add_rack keeps its task RUNNING while waiting on
        # a child; if add_dc waited on add_pod the same way here, a standalone-created
        # pod's own wait-for-parent guard would deadlock against it).
        # Back-to-back inter-pod spine mesh cabling (designs with no super-spine tier)
        # is handled by pod.py itself — each pod cables to its existing lower-index
        # siblings directly (see PodTopologyGenerator._cable_to_existing_sibling_pods).
        # This also makes incremental single-pod-add work correctly with no DC-level
        # orchestration, since add_pod alone (no add_dc) is a supported entry point.

        self._is_ipv6 = is_ipv6
        dc_fabric_loopback_pool = dc_pools.get("dc-fabric-loopback")
        self._dc_fabric_loopback_pool_id = dc_fabric_loopback_pool.id if dc_fabric_loopback_pool else None
        await self._generate_dc_scoped_fabric_devices()

    async def _create_dc_tier_devices(
        self,
        role: str,
        entries: list[dict[str, Any]],
        *,
        indexes: list[int],
        dc_pools: dict[str, Any],
        is_ipv6: bool,
    ) -> list[str]:
        """Create one DC-scoped fabric tier (super-spine/hyper-spine), one
        create_devices() call per fabric_templates entry, looped back from
        the DC-fabric loopback pool."""
        names: list[str] = []
        for entry in entries:
            names.extend(
                await self.create_devices(
                    deployment_id=self.data["id"],
                    device_role=role,
                    quantity=entry["quantity"],
                    template=entry["template"],
                    naming_convention=naming_convention_of(self.data),
                    options=DeviceOptions(
                        indexes=indexes,
                        allocate_loopback=True,
                        loopback_pool=dc_pools.get("dc-fabric-loopback"),
                        loopback_prefix_length=128 if is_ipv6 else 32,
                        management_pool=dc_pools.get("management"),
                    ),
                )
            )
        return names

    async def _preseed_tier_routing(
        self,
        role: str,
        names: list[str],
        *,
        routing_strategy: str,
        asn_pool_id: str | None,
        shared_underlay_as_id: str | None = None,
    ) -> None:
        """Create a DC-scoped tier's own BGP processes, before any cabling:
        overlay only under ospf-ibgp, otherwise underlay (on
        shared_underlay_as_id when given) plus overlay."""
        options = RoutingOptions(design=self.data, asn_pool=asn_pool_id)
        if routing_strategy == RoutingStrategy.OSPF_IBGP.value:
            options["skip_underlay"] = True
        elif shared_underlay_as_id:
            options["shared_underlay_as_id"] = shared_underlay_as_id
        await self.create_routing(
            bottom_devices=names, top_devices=[], options=options, p2p_interfaces=[], bottom_role=role
        )

    def _validate_fabric_template_roles(self) -> None:
        """Log+skip (don't abort) any fabric_templates entry using a role this
        DC-level generator doesn't know how to place — an unrelated bad entry
        shouldn't block the other roles from generating (mirrors rack.py's own
        loop-iteration error convention, not its abort-on-error one, since here
        the roles are independent of each other)."""
        for entry in self.data.get("fabric_templates", []):
            if entry["role"] not in _DC_VALID_FABRIC_ROLES:
                self.logger.warning(
                    f"DC {self.fabric_name}: fabric_templates entry with role={entry['role']!r} is not valid "
                    f"at DC level (expected one of {sorted(_DC_VALID_FABRIC_ROLES)}) — skipping this entry."
                )

    @staticmethod
    def _pod_border_leaf_capacity(pod: Any) -> int:
        """This pod's own layout cap on how many border-leaf devices it can
        receive — a pod whose layout caps max_border_leafs_per_pod=0 is
        deliberately skipped, which is how a specific subset of pods (e.g.
        pod 1 and pod 3, not pod 2) can be chosen to host border-leafs.

        Only ever called on pods from the `include=["layout"]` fetch in
        generate() (see self._existing_pods), so layout is always hydrated —
        TopologyPod.layout is a mandatory attribute."""
        return POD_LAYOUTS[pod.layout.value].get("max_border_leafs_per_pod", 0)

    async def _create_border_leaf_devices(self) -> list[str]:
        """Create border-leaf devices for every fabric_templates(role="border-leaf")
        entry, distributing each entry's quantity across the DC's existing pods by
        walking pods in index order and giving each pod up to its OWN design's
        max_border_leafs_per_pod cap — a pod whose own design caps it at 0 is
        skipped entirely, which is how a specific subset of pods (e.g. pod 1 and
        pod 3, not pod 2) can be chosen to host border-leafs. deployment_id is
        the DC's own id (border-leaf is a DC-level fabric tier, like
        super-spine/hyper-spine — pod.index only picks WHICH pod's spines it
        physically cables to, it does not own the device). Cabling/routing to
        that pod's spines is still pod.py's job, not dc.py's — it already owns
        spine context and can query "which border-leafs deploy under me" (by
        rack/index placement, not by deployment) during its own bootstrap.
        Returns every border-leaf name created this run, DC-wide, for
        BLF<->FW<->LB cabling below."""
        entries = templates_by_role(self.data.get("fabric_templates", []), "border-leaf")
        if not entries:
            return []

        existing_pods = getattr(self, "_existing_pods", [])
        if not existing_pods:
            self.logger.info(f"DC {self.fabric_name}: no pods yet — deferring border-leaf placement")
            return []
        sorted_pods = sorted(existing_pods, key=lambda p: p.index.value)

        max_border_leafs_per_fabric = resolve_dc_size_layout(self.data["size"])["max_border_leafs_per_fabric"]
        all_names: list[str] = []
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
                pod_capacity = self._pod_border_leaf_capacity(pod)
                if pod_capacity <= 0:
                    continue
                share = min(remaining, pod_capacity)
                remaining -= share

                device_options = DeviceOptions(
                    indexes=[self.data["index"], pod.index.value],
                    allocate_loopback=True,
                    loopback_pool=self._dc_fabric_loopback_pool_id,
                    loopback_prefix_length=128 if self._is_ipv6 else 32,
                )
                names = await self.create_devices(
                    deployment_id=self.data["id"],
                    device_role="border-leaf",
                    quantity=share,
                    template=entry["template"],
                    naming_convention=naming_convention_of(self.data),
                    options=device_options,
                )
                all_names.extend(names)

            if remaining > 0:
                # Deliberately a warning, not a failure: a DC may legitimately
                # declare border-leafs before all its pods exist, and the
                # remainder gets placed when dc_pod_cascade runs for the new pod.
                # It is still a data error when the pods are all present, so name
                # the numbers — the symptom otherwise surfaces much later as a
                # border-leaf count that is short of what the DC declared.
                capacity = sum(self._pod_border_leaf_capacity(pod) for pod in sorted_pods)
                self.logger.warning(
                    f"DC {self.fabric_name}: border-leaf entry requested {entry['quantity']} device(s) but "
                    f"{remaining} are left unplaced — the DC's {len(sorted_pods)} pod(s) offer "
                    f"max_border_leafs_per_pod capacity for {capacity} in total. Add pods, or lower the "
                    "entry's quantity to match."
                )

        return all_names

    async def _create_role_devices(
        self,
        *,
        role: Literal["firewall", "load-balancer"],
        entries: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: NamingConvention,
        indexes: list[int],
    ) -> list[str]:
        """Create firewall/load-balancer devices for one fabric_templates role,
        DC-wide (deployment_id=dc.id), via the shared HA-paired creation shape
        (DeviceMixin.create_ha_role_devices — also used by pod.py and
        colocation.py), flattened to the created names."""
        all_names: list[str] = []
        for _, names in await self.create_ha_role_devices(
            role=role,
            entries=entries,
            deployment_id=deployment_id,
            naming_convention=naming_convention,
            indexes=indexes,
        ):
            all_names.extend(names)

        return all_names

    async def _generate_dc_scoped_fabric_devices(self) -> None:
        """Create border-leaf devices and provision the DC's shared service chain.

        No-ops on border-leaf if no pods exist yet — a DC with zero pods has
        nothing to place border-leafs into. A pod added later than this DC's
        border-leaf declaration needs an explicit dc_pod_cascade run to get its
        share — same as any other structural DC-level change (see the manual
        dc_pod_cascade calls after bulk loads); not auto-triggered from
        pod.py's add_pod, which would otherwise fire a concurrent DC-level
        re-bootstrap on every single pod creation during a bulk multi-pod load.
        """
        border_leaf_names = await self._create_border_leaf_devices()
        await self._generate_dc_shared_service_devices(border_leaf_names=border_leaf_names)

    async def _generate_dc_shared_service_devices(self, *, border_leaf_names: list[str]) -> None:
        """Create shared DC firewall/load-balancer devices (HA-paired internally
        by create_devices(), any quantity) and cable them to border-leaf."""

        data = self.data
        naming_convention = naming_convention_of(data)
        fabric_templates = data.get("fabric_templates", [])
        firewall_templates = templates_by_role(fabric_templates, "firewall")
        load_balancer_templates = templates_by_role(fabric_templates, "load-balancer")
        firewall_names = await self._create_role_devices(
            role="firewall",
            entries=firewall_templates,
            deployment_id=data["id"],
            naming_convention=naming_convention,
            indexes=[data["index"]],
        )
        load_balancer_names = await self._create_role_devices(
            role="load-balancer",
            entries=load_balancer_templates,
            deployment_id=data["id"],
            naming_convention=naming_convention,
            indexes=[data["index"]],
        )

        if firewall_names:
            await self._ensure_firewall_context_pools(dc_name=self.fabric_name)

        await self._cable_border_services(
            border_role_for=BORDER_ROLE_FOR_SERVICES,
            connectivity_mode=cast(Literal["pbr", "inline"], data.get("connectivity_mode", "pbr")),
            border_names=border_leaf_names,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )

    async def _ensure_firewall_context_pools(self, *, dc_name: str) -> None:
        """Create this DC's own FirewallContext VLAN pool.

        Per-DC (not global) so each fabric's context sub-interfaces stay
        within its own numbering, matching the existing
        {fabric_name}-vlan-pool/{fabric_name}-vni-pool pattern above. The
        contexts' transit /29s come from the per-VRF FW-Transit-* bootstrap
        pools, so there is no per-DC P2P pool.

        generators/topology/customer_dc.py's _ensure_firewall_context
        allocates from the VLAN pool once a customer boards onto this DC.

        See PoolMixin.ensure_firewall_context_pools for the shared
        pool-creation mechanism (also used by generators/topology/
        colocation.py for the metro equivalent) and its locking rationale.
        """
        await self.ensure_firewall_context_pools(name=dc_name, vlan_start=FW_CONTEXT_VLAN_START)
