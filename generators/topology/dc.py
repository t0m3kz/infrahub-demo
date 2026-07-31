"""Infrastructure generator for data center topology."""

import secrets
from typing import Any, Literal, cast

from infrahub_sdk.protocols import CoreStandardGroup

from utils.data_cleaning import clean_data

from ..common import CommonGenerator, DeviceOptions
from ..helpers import calculate_dc_fabric_loopback_prefix, name_to_asn_range
from ..helpers.routing import RoutingStrategy
from ..models import DCModel, DeviceRole
from ..protocols import (
    DcimPhysicalDevice,
    RoutingAutonomousSystem,
    RoutingOSPFArea,
    RoutingPassword,
    TopologyPod,
)
from ..types import ChainHop, RoutingOptions

_DC_VALID_FABRIC_ROLES = frozenset({"super-spine", "border-leaf", "firewall", "load-balancer"})
_DC_HA_KINDS: dict[str, Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"]] = {
    "firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}
# FW/LB "uplink" interfaces face border-leaf; border-leaf's "firewall"/"load-balancer"
# interfaces are the dedicated counterpart ports for each service — see
# N9K-C9336C-FX2_BORDER_LEAF's template for the canonical port layout.
_BL_ROLE_FOR: dict[str, str] = {"firewall": "firewall", "load-balancer": "load-balancer"}


class DCTopologyGenerator(CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology."""

        try:
            deployment_list = clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error("No TopologyDeployment data found in GraphQL response")
                return

            self.data = DCModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Processing Data Center: {self.data.name}")

        if self.data.is_managed_by_controller:
            self.logger.info(
                f"DC {self.data.name}: management_mode=managed_by_controller — skipping generator, "
                "topology is owned by an external controller"
            )
            return

        # Bail out before any group_context registration below: registering only
        # the pods (never the pools/super-spine/ASN objects a full run would also
        # register) would make this run's membership a strict subset of a prior
        # successful run's, and the framework's delete-unused-nodes sync would
        # then delete everything not re-registered here as "unused".
        if not self.data.design:
            self.logger.error(f"Cannot create pools for DC {self.data.name.upper()}: design relationship is required")
            return

        # Add existing pods to group context to prevent deletion
        # include=["design"] also lets _generate_dc_scoped_fabric_devices read each
        # pod's own max_border_leafs_per_pod cap via _pod_border_leaf_capacity.
        existing_pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id], include=["design"])
        related_node_ids = self.client.group_context.related_node_ids
        for pod in existing_pods:
            related_node_ids.append(pod.id)

        dc_id = self.data.id
        self.deployment_id = dc_id  # Store for cable linking
        self.fabric_name = self.data.name.lower()
        dc_index = self.data.index  # Get DC index for unique device naming
        self._validate_fabric_template_roles()
        super_spine_entries = self.data.super_spine_templates
        amount_of_super_spines = sum(entry.quantity for entry in super_spine_entries)
        self.logger.info(f"Generating topology for data center {self.fabric_name.upper()}")
        indexes: list[int] = [dc_index]

        if amount_of_super_spines > self.data.design.max_super_spines_per_fabric:
            raise RuntimeError(
                f"DC {self.fabric_name.upper()} requests {amount_of_super_spines} super-spines but the assigned "
                f"design allows at most {self.data.design.max_super_spines_per_fabric}"
            )

        naming_convention = self.data.naming_convention
        dc_design = self.data.design
        is_ipv6 = dc_design.is_ipv6
        is_dual_stack = dc_design.is_dual_stack

        # Prefix lengths come from the design; the DC instance can override by pre-attaching pools.
        # Values must already match the underlay_protocol (IPv4 or IPv6) — no conversion needed.
        # Management is always IPv4.
        technical_prefix = dc_design.technical_prefix_length
        loopback_prefix = dc_design.loopback_prefix_length
        management_prefix = dc_design.management_prefix_length

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
        design_mode = "back-to-back" if getattr(dc_design, "max_super_spines_per_fabric", 0) == 0 else "super-spine"
        max_super_spines_cap = self.data.design.max_super_spines_per_fabric
        max_border_leafs_cap = self.data.design.max_border_leafs_per_fabric or 0
        if design_mode != "back-to-back" and (max_super_spines_cap > 0 or max_border_leafs_cap > 0):
            pools_to_allocate["dc-fabric-loopback"] = calculate_dc_fabric_loopback_prefix(
                max_devices=max_super_spines_cap + max_border_leafs_cap,
                ipv6=is_ipv6,
            )

        self.logger.info(f"Allocating DC pools: {list(pools_to_allocate.keys())}")

        dc_pools = await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=pools_to_allocate,
            ipv6=is_ipv6,
            dual_stack=is_dual_stack,
        )

        # Attach pool references to the DC every run — allocate_resource_pools()
        # returns the same (upserted) pool objects whether they were just created or
        # already existed, so this setattr+save is itself idempotent.
        dc = await self.client.get(kind="TopologyDataCenter", id=dc_id)
        if dc:
            pool_attr_map: dict[str, str] = {
                "loopback": "loopback_pool",
                "management": "management_pool",
                "technical": "technical_pool",
            }
            for pool_name, pool_obj in dc_pools.items():
                if pool_name in pool_attr_map:
                    setattr(dc, pool_attr_map[pool_name], {"id": pool_obj.id})
            await dc.save(allow_upsert=True)

        # Derive deterministic ASN range from DC name (unique per site).
        # max_border_leafs_per_fabric is included since border-leaf devices draw
        # from this same fabric_asn_pool (see upsert_asn_pool below).
        max_pods = self.data.design.max_pods
        max_spines_per_pod = self.data.design.max_spines_per_pod
        max_border_leafs_per_fabric = self.data.design.max_border_leafs_per_fabric or 0
        asn_start, asn_end = name_to_asn_range(
            dc_name=self.data.name,
            max_pods=max_pods,
            amount_of_super_spines=amount_of_super_spines,
            max_spines_per_pod=max_spines_per_pod,
            max_border_leafs_per_fabric=max_border_leafs_per_fabric,
        )

        # Only create ASN pool for eBGP-based strategies (one pool per DC, shared by
        # super-spine AND border-leaf devices)
        # ospf-ibgp uses OSPF underlay + shared overlay AS — no per-device pools needed
        routing_strategy = self.data.design.routing_strategy
        fabric_asn_pool_id: str | None = None
        if routing_strategy in (RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value):
            asn_pool_obj = await self.upsert_asn_pool(
                pool_name=f"{self.fabric_name}-asn-pool",
                description=f"ASN pool for {self.fabric_name.upper()} fabric",
                start_range=asn_start,
                end_range=asn_end,
                parent_kind="TopologyDataCenter",
                parent_id=dc_id,
                parent_attr="fabric_asn_pool",
            )
            if asn_pool_obj:
                fabric_asn_pool_id = asn_pool_obj.id

        # Create VLAN pool for VxlanSegment's local VLAN allocation (per-DC).
        # VlanSegment.vlan_id is set manually — no pool involved (single-site,
        # no collision-tracking concern requiring automated allocation).
        await self.upsert_number_pool(
            pool_name=f"{self.fabric_name}-vlan-pool",
            description=f"VLAN ID pool for {self.fabric_name.upper()}",
            start_range=100,
            end_range=3999,
            node="ManagedSegmentDeployment",
            node_attribute="vlan_id",
            parent_kind="TopologyDataCenter",
            parent_id=dc_id,
            parent_attr="vlan_pool",
        )

        # Create VNI and L3 VNI pools for the VXLAN-EVPN overlay
        await self.upsert_number_pool(
            pool_name=f"{self.fabric_name}-vni-pool",
            description=f"L2 VNI pool for {self.fabric_name.upper()}",
            start_range=10001,
            end_range=16777215,
            node="ManagedSegmentDeployment",
            node_attribute="vni",
            parent_kind="TopologyDataCenter",
            parent_id=dc_id,
            parent_attr="vni_pool",
        )
        await self.upsert_number_pool(
            pool_name=f"{self.fabric_name}-l3vni-pool",
            description=f"L3 VNI pool for {self.fabric_name.upper()} VRFs",
            start_range=50001,
            end_range=59999,
            node="BuiltinIPNamespace",
            node_attribute="l3_vni",
            parent_kind="TopologyDataCenter",
            parent_id=dc_id,
            parent_attr="l3_vni_pool",
        )

        super_spine_names: list[str] = []
        if design_mode == "back-to-back":
            self.logger.info(
                f"DC {self.fabric_name}: design_mode=back-to-back — "
                "skipping super-spine tier, spines will connect directly across pods"
            )
        elif amount_of_super_spines > 0 and super_spine_entries:
            for entry in super_spine_entries:
                entry_names = await self.create_devices(
                    deployment_id=dc_id,
                    device_role="super-spine",
                    amount=entry.quantity,
                    template=entry.template.model_dump(),
                    naming_convention=cast(
                        Literal["standard", "hierarchical", "flat"],
                        naming_convention.lower(),
                    ),
                    options=DeviceOptions(
                        indexes=indexes,
                        allocate_loopback=True,
                        loopback_pool=dc_pools.get("dc-fabric-loopback"),
                        loopback_prefix_length=128 if is_ipv6 else 32,
                        management_pool=dc_pools.get("management"),
                    ),
                )
                super_spine_names.extend(entry_names)

        # Create shared routing objects (overlay AS, OSPF area) at the DC level
        # so pod/rack generators always find them and never create duplicates.
        # overlay_asn is asn_end + 1 to avoid collision with the per-device pool range [asn_start, asn_end]
        await self._create_shared_routing_objects(overlay_asn=asn_end + 1)

        # Create super-spine routing objects here so they exist before any pod generator runs.
        # For eBGP strategies: underlay + overlay BGP processes.
        # For ospf-ibgp: overlay BGP only — super-spines sit above the OSPF domain and are
        # skipped as top_devices in pod-level routing, so their overlay BGP must be seeded here.
        if super_spine_names and routing_strategy in (
            RoutingStrategy.EBGP_EBGP.value,
            RoutingStrategy.EBGP_IBGP.value,
            RoutingStrategy.OSPF_IBGP.value,
        ):
            routing_opts = RoutingOptions(design=self.data.design, asn_pool=fabric_asn_pool_id)
            if routing_strategy == RoutingStrategy.OSPF_IBGP.value:
                routing_opts["skip_underlay"] = True
            await self.create_routing(
                bottom_devices=super_spine_names,
                top_devices=[],
                options=routing_opts,
                p2p_interfaces=[],
                bottom_role="super-spine",
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

        self._existing_pods = existing_pods
        self._is_ipv6 = is_ipv6
        dc_fabric_loopback_pool = dc_pools.get("dc-fabric-loopback")
        self._dc_fabric_loopback_pool_id = dc_fabric_loopback_pool.id if dc_fabric_loopback_pool else None
        await self._generate_dc_scoped_fabric_devices()

    def _validate_fabric_template_roles(self) -> None:
        """Log+skip (don't abort) any fabric_templates entry using a role this
        DC-level generator doesn't know how to place — an unrelated bad entry
        shouldn't block the other roles from generating (mirrors rack.py's own
        loop-iteration error convention, not its abort-on-error one, since here
        the roles are independent of each other)."""
        for entry in self.data.fabric_templates or []:
            if entry.role not in _DC_VALID_FABRIC_ROLES:
                self.logger.warning(
                    f"DC {self.fabric_name}: fabric_templates entry with role={entry.role!r} is not valid "
                    f"at DC level (expected one of {sorted(_DC_VALID_FABRIC_ROLES)}) — skipping this entry."
                )

    @staticmethod
    def _pod_border_leaf_capacity(pod: Any) -> int:
        """This pod's own design cap on how many border-leaf devices it can
        receive — a pod with max_border_leafs_per_pod=0 in its own design is
        deliberately skipped, which is how a specific subset of pods (e.g.
        pod 1 and pod 3, not pod 2) can be chosen to host border-leafs."""
        design = getattr(pod, "design", None)
        design_peer = getattr(design, "peer", None) if design else None
        if design_peer is None:
            return 0
        attr = getattr(design_peer, "max_border_leafs_per_pod", None)
        return getattr(attr, "value", None) or 0

    async def _ensure_dc_ha_pair(
        self,
        device_names: list[str],
        *,
        ha_kind: Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"],
        role_label: str,
    ) -> None:
        """Pair exactly 2 same-role devices (one pod's share) into an HA domain —
        near-verbatim copy of rack.py's _ensure_ha_pair, moved here since
        firewall/load-balancer generation moved to DC level. Never pairs across
        pods: both members of an HA pair must sit in front of the same
        border-leaf/spine fabric to mean anything physically."""
        if len(device_names) != 2:
            return

        first, second = sorted(device_names)
        ha_name = f"{first}-{second}-ha"
        existing = await self.client.filters(kind=ha_kind, name__value=ha_name)
        if existing:
            self.client.group_context.related_node_ids.append(existing[0].id)
            return

        devices = await self.client.filters(kind=DcimPhysicalDevice, name__values=[first, second])
        if len(devices) != 2:
            self.logger.error(f"HA pair {first}/{second}: could not resolve both devices.")
            return

        ha_group = await self.client.get(kind=CoreStandardGroup, name__value="ha_domains")
        ha_obj = await self.client.create(
            kind=ha_kind,
            data={
                "name": ha_name,
                "status": "active",
                "capabilities": [{"id": dev.id} for dev in devices],
                "member_of_groups": [{"id": ha_group.id}],
            },
        )
        await ha_obj.save(allow_upsert=True)
        self.logger.info(f"DC {self.fabric_name}: created HA domain {ha_name} for {role_label}s")

    async def _create_border_leaf_devices(self) -> list[str]:
        """Create border-leaf devices for every fabric_templates(role="border-leaf")
        entry, distributing each entry's quantity across the DC's existing pods by
        walking pods in index order and giving each pod up to its OWN design's
        max_border_leafs_per_pod cap — a pod whose own design caps it at 0 is
        skipped entirely, which is how a specific subset of pods (e.g. pod 1 and
        pod 3, not pod 2) can be chosen to host border-leafs. deployment_id is the
        target pod's id (not the DC's) so devices land in that pod's fabric, but
        cabling/routing them to that pod's spines is pod.py's job, not dc.py's —
        it already owns spine context and can query "which border-leafs deploy
        under me" during its own bootstrap. Returns every border-leaf name created
        this run, DC-wide, for BLF<->FW<->LB cabling below."""
        entries = self.data.border_leaf_templates
        if not entries:
            return []

        existing_pods = getattr(self, "_existing_pods", [])
        if not existing_pods:
            self.logger.info(f"DC {self.fabric_name}: no pods yet — deferring border-leaf placement")
            return []
        sorted_pods = sorted(existing_pods, key=lambda p: p.index.value)

        max_border_leafs_per_fabric = self.data.design.max_border_leafs_per_fabric if self.data.design else None
        all_names: list[str] = []
        for entry in entries:
            if max_border_leafs_per_fabric is not None and entry.quantity > max_border_leafs_per_fabric:
                self.logger.error(
                    f"DC {self.fabric_name}: border-leaf entry requests {entry.quantity} devices but "
                    f"design.max_border_leafs_per_fabric allows at most {max_border_leafs_per_fabric} — skipping."
                )
                continue

            remaining = entry.quantity
            for pod in sorted_pods:
                if remaining <= 0:
                    break
                pod_capacity = self._pod_border_leaf_capacity(pod)
                if pod_capacity <= 0:
                    continue
                share = min(remaining, pod_capacity)
                remaining -= share

                device_options = DeviceOptions(
                    indexes=[self.data.index, pod.index.value],
                    allocate_loopback=True,
                    loopback_pool=self._dc_fabric_loopback_pool_id,
                    loopback_prefix_length=128 if self._is_ipv6 else 32,
                )
                names = await self.create_devices(
                    deployment_id=pod.id,
                    device_role="border-leaf",
                    amount=share,
                    template=entry.template.model_dump(),
                    naming_convention=cast(
                        Literal["standard", "hierarchical", "flat"], self.data.naming_convention.lower()
                    ),
                    options=device_options,
                )
                all_names.extend(names)

            if remaining > 0:
                self.logger.warning(
                    f"DC {self.fabric_name}: border-leaf entry has {remaining} device(s) left unplaced — "
                    "no pod had remaining max_border_leafs_per_pod capacity."
                )

        return all_names

    async def _create_dc_wide_role_devices(
        self, *, role: Literal["firewall", "load-balancer"], entries: list[DeviceRole]
    ) -> list[str]:
        """Create firewall/load-balancer devices DC-wide (deployment_id=dc.id, not
        split across pods — these sit in front of the whole DC's border-leaf tier,
        not any one pod), pairing each entry's devices into an HA domain when
        quantity == 2. No loopback allocation — not part of underlay/overlay routing."""
        device_options = DeviceOptions(indexes=[self.data.index])
        if role == "load-balancer":
            # create_devices()'s default group_name is f"{device_role}s" = "load-balancers",
            # but the bootstrap group is named "loadbalancers" (no hyphen) — override.
            device_options["group_name"] = "loadbalancers"
        ha_kind = _DC_HA_KINDS[role]

        all_names: list[str] = []
        for entry in entries:
            names = await self.create_devices(
                deployment_id=self.data.id,
                device_role=role,
                amount=entry.quantity,
                template=entry.template.model_dump(),
                naming_convention=cast(
                    Literal["standard", "hierarchical", "flat"], self.data.naming_convention.lower()
                ),
                options=device_options,
            )
            all_names.extend(names)
            if entry.quantity == 2:
                await self._ensure_dc_ha_pair(names, ha_kind=ha_kind, role_label=role)

        return all_names

    async def _cable_dc_services(
        self, *, border_leaf_names: list[str], firewall_names: list[str], load_balancer_names: list[str]
    ) -> None:
        """Cable border-leaf<->firewall<->load-balancer per connectivity_mode.
        Index-paired (bl[0]<->fw[0], bl[1]<->fw[1], ...), never any-to-any —
        each border-leaf/firewall/load-balancer triple is one independent
        redundant path. Fewer devices on one side are reused round-robin.
        - pbr: two independent legs, each on the service device's "uplink" ports.
        - inline: one chain — border-leaf<->firewall<->load-balancer<->border-leaf.
          Every device has an "uplink" (toward the previous hop) and "downlink"
          (toward the next), distinct from load-balancer's own "customer"-role
          VIP ports (untouched here).
        No-ops for any leg with nothing to cable (create_chain_cabling's own
        empty-devices handling).
        """
        blf_to_firewall = ChainHop(devices=border_leaf_names, down_role=_BL_ROLE_FOR["firewall"])
        firewall_hop = ChainHop(devices=firewall_names, up_role="uplink")
        await self.create_chain_cabling([blf_to_firewall, firewall_hop])

        if self.data.connectivity_mode == "inline":
            middle_firewall_hop = ChainHop(devices=firewall_names, down_role="downlink")
            middle_lb_hop = ChainHop(devices=load_balancer_names, up_role="uplink")
            await self.create_chain_cabling([middle_firewall_hop, middle_lb_hop])

            blf_to_lb = ChainHop(devices=border_leaf_names, down_role=_BL_ROLE_FOR["load-balancer"])
            return_lb_hop = ChainHop(devices=load_balancer_names, up_role="downlink")
            await self.create_chain_cabling([blf_to_lb, return_lb_hop])
        else:
            blf_to_lb = ChainHop(devices=border_leaf_names, down_role=_BL_ROLE_FOR["load-balancer"])
            lb_hop = ChainHop(devices=load_balancer_names, up_role="uplink")
            await self.create_chain_cabling([blf_to_lb, lb_hop])

    async def _generate_dc_scoped_fabric_devices(self) -> None:
        """Create border-leaf (distributed across pods by their own design caps),
        firewall, and load-balancer devices (DC-wide), then cable border-leaf to
        firewall/load-balancer per connectivity_mode. No-ops on border-leaf if no
        pods exist yet — a DC with zero pods has nothing to place border-leafs
        into. A pod added later than this DC's border-leaf declaration needs an
        explicit dc_pod_cascade run to get its share — same as any other
        structural DC-level change (see tasks/demo.py's own manual dc_pod_cascade
        calls after bulk loads); not auto-triggered from pod.py's add_pod, which
        would otherwise fire a concurrent DC-level re-bootstrap on every single
        pod creation during a bulk multi-pod load."""
        border_leaf_names = await self._create_border_leaf_devices()
        firewall_names = await self._create_dc_wide_role_devices(role="firewall", entries=self.data.firewall_templates)
        load_balancer_names = await self._create_dc_wide_role_devices(
            role="load-balancer", entries=self.data.load_balancer_templates
        )
        await self._cable_dc_services(
            border_leaf_names=border_leaf_names,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )

    async def _ensure_routing_password(self, name: str, description: str) -> None:
        """Find-or-create a shared RoutingPassword by deterministic name.

        Query-first: an existing password's value is never regenerated or
        upserted, since ``secrets.token_urlsafe()`` is non-deterministic —
        re-running this on every DC generate would silently rotate the
        BGP/OSPF auth key underneath already-deployed devices.
        """
        try:
            existing = await self.client.get(kind=RoutingPassword, name__value=name, raise_when_missing=False)
            if existing:
                self.client.group_context.related_node_ids.append(existing.id)
                self.logger.info(f"Found existing RoutingPassword: {name} ({existing.id})")
                return
        except Exception as e:
            self.logger.warning(f"Error querying RoutingPassword {name}: {e}")
            return

        try:
            obj = await self.client.create(
                kind=RoutingPassword,
                data={
                    "name": name,
                    "password": secrets.token_urlsafe(24),
                    "description": description,
                },
            )
            await obj.save(allow_upsert=True)
            self.client.group_context.related_node_ids.append(obj.id)
            self.logger.info(f"Created shared RoutingPassword: {name} ({obj.id})")
        except Exception as e:
            self.logger.error(f"Failed to create shared RoutingPassword {name}: {e}")

    async def _create_shared_routing_objects(self, overlay_asn: int) -> None:
        """Create fabric-wide shared routing objects once at DC level.

        Based on the routing strategy:
        - ``*-ibgp``: creates a single shared RoutingAutonomousSystem for iBGP overlay
        - ``ospf-*``: creates a single shared RoutingOSPFArea (area 0) for OSPF underlay
        - all strategies: creates shared underlay + overlay BGP/OSPF auth keys
          (RoutingPassword), each referenced by every underlay/overlay peering.

        These objects are created idempotently (allow_upsert) so re-running
        the DC generator is safe. RoutingPassword is the one exception: its
        secret value is generated once and never touched again on re-run
        (see ``_ensure_routing_password``).
        """
        if not self.data.design:
            return

        strategy = self.data.design.routing_strategy

        await self._ensure_routing_password(
            name=f"{self.fabric_name}-underlay-key",
            description=f"Shared eBGP/OSPF underlay auth key for {self.fabric_name}",
        )
        await self._ensure_routing_password(
            name=f"{self.fabric_name}-overlay-key",
            description=f"Shared BGP overlay/EVPN auth key for {self.fabric_name}",
        )

        # iBGP overlay → ensure exactly one shared ASN exists with deterministic value
        if strategy in (RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value):
            overlay_desc = f"{self.fabric_name} overlay ASN for iBGP EVPN"
            try:
                existing = await self.client.filters(
                    kind=RoutingAutonomousSystem,
                    description__value=overlay_desc,
                )
                if existing:
                    as_obj = existing[0]
                    as_obj.asn.value = overlay_asn
                    await as_obj.save(allow_upsert=True)
                    self.logger.info(f"Updated shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
                else:
                    as_obj = await self.client.create(
                        kind=RoutingAutonomousSystem,
                        data={"asn": overlay_asn, "description": overlay_desc},
                    )
                    await as_obj.save(allow_upsert=True)
                    self.logger.info(f"Created shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
                self.client.group_context.related_node_ids.append(as_obj.id)
            except Exception as e:
                self.logger.error(f"Failed to create shared overlay AS: {e}")

        # OSPF underlay → create shared area 0
        if strategy == RoutingStrategy.OSPF_IBGP.value:
            area_name = f"{self.fabric_name}-ospf-area-0"
            try:
                area_obj = await self.client.create(
                    kind=RoutingOSPFArea,
                    data={
                        "name": area_name,
                        "area": 0,
                        "area_type": "standard",
                        "description": f"OSPF backbone area for {self.fabric_name}",
                    },
                )
                await area_obj.save(allow_upsert=True)
                self.client.group_context.related_node_ids.append(area_obj.id)
                self.logger.info(f"Created shared OSPF area: {area_name}")
            except Exception as e:
                self.logger.error(f"Failed to create shared OSPF area: {e}")
