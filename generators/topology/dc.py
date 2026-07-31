"""Infrastructure generator for data center topology."""

import secrets
from typing import Any, Literal, cast

from infrahub_sdk.protocols import CoreStandardGroup

from utils.data_cleaning import clean_data

from ..common import CablingOptions, CommonGenerator, DeviceOptions
from ..helpers import calculate_super_spine_loopback_prefix, name_to_asn_range
from ..helpers.routing import RoutingStrategy
from ..models import DCModel, Template
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    RoutingAutonomousSystem,
    RoutingOSPFArea,
    RoutingPassword,
    TopologyPod,
)
from ..types import RoutingOptions

_DC_VALID_FABRIC_ROLES = frozenset({"super-spine", "border-leaf", "firewall", "load-balancer"})
_DC_HA_KINDS: dict[str, Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"]] = {
    "firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}


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

        # Add existing pods to group context to prevent deletion
        # include=["loopback_pool", "asn_pool", "design"] also lets
        # _generate_dc_scoped_fabric_devices give each pod's border-leaf share a
        # loopback + underlay/overlay BGP, and compute a deterministic spine
        # downlink offset from the pod's own design capacity.
        existing_pods = await self.client.filters(
            kind=TopologyPod, parent__ids=[self.data.id], include=["loopback_pool", "asn_pool", "design"]
        )
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

        if not self.data.design:
            self.logger.error(f"Cannot create pools for DC {self.fabric_name.upper()}: design relationship is required")
            return

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

        design_mode = "back-to-back" if getattr(dc_design, "max_super_spines_per_fabric", 0) == 0 else "super-spine"
        if amount_of_super_spines > 0 and super_spine_entries and design_mode != "back-to-back":
            super_spine_loopback_prefix = calculate_super_spine_loopback_prefix(
                max_super_spines=amount_of_super_spines,
                ipv6=is_ipv6,
            )
            pools_to_allocate["super-spine-loopback"] = super_spine_loopback_prefix

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

        # Derive deterministic ASN range from DC name (unique per site)
        max_pods = self.data.design.max_pods
        max_spines_per_pod = self.data.design.max_spines_per_pod
        asn_start, asn_end = name_to_asn_range(
            dc_name=self.data.name,
            max_pods=max_pods,
            amount_of_super_spines=amount_of_super_spines,
            max_spines_per_pod=max_spines_per_pod,
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
                        loopback_pool=dc_pools.get("super-spine-loopback"),
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
        self._dc_design = dc_design
        self._is_ipv6 = is_ipv6
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
    def _split_quantity(quantity: int, pod_count: int) -> list[int]:
        """Split ``quantity`` across ``pod_count`` pods, first pods (lowest index)
        get the remainder. E.g. split(4, 2) -> [2, 2]; split(3, 2) -> [2, 1]."""
        if pod_count == 0:
            return []
        base, remainder = divmod(quantity, pod_count)
        return [base + 1 if i < remainder else base for i in range(pod_count)]

    @staticmethod
    def _pod_spine_port_reservation(pod: Any) -> int:
        """Deterministic count of spine downlink ports leaf/tor devices reserve
        for this pod, derived from design CAPACITY (not live device count) —
        mirrors pod.py's own pool-sizing calc. Border-leaf cabling starts after
        this reservation, so it never races the rack generator's own
        deterministic, capacity-based offsets for leaf/tor devices cabling into
        the same spines concurrently (a live cable-count read would race those
        writers under concurrent load — this must stay capacity-based)."""
        design = getattr(pod, "design", None)
        design_peer = getattr(design, "peer", None) if design else None
        if design_peer is None:
            return 0

        def _val(name: str) -> int:
            attr = getattr(design_peer, name, None)
            value = getattr(attr, "value", None) if attr is not None else None
            return value or 0

        network_racks_per_row = _val("network_racks_per_row")
        compute_racks_per_row = _val("compute_racks_per_row")
        max_tors_per_compute_rack = _val("max_tors_per_compute_rack")
        rows = _val("rows")
        max_leafs_per_network_rack = _val("max_leafs_per_network_rack")

        # tor deployment: ToRs connect directly to spines. middle_rack/mixed:
        # only leafs connect to spines (ToRs connect to the local leaf pair).
        if network_racks_per_row == 0:
            return rows * compute_racks_per_row * max_tors_per_compute_rack
        return rows * network_racks_per_row * max_leafs_per_network_rack

    async def _pod_spine_fabric(self, pod_id: str) -> tuple[list[str], list[str]]:
        """Return (spine_device_names, spine_downlink_interface_names) for
        cabling new border-leaf devices into ``pod_id``'s spines."""
        spines = await self.client.filters(kind=DcimPhysicalDevice, deployment__ids=[pod_id], role__value="spine")
        spine_names = sorted(spine.name.value for spine in spines)
        if not spine_names:
            return [], []

        downlinks = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=spine_names,
            role__value="downlink",
        )
        interface_names = sorted({iface.name.value for iface in downlinks})
        return spine_names, interface_names

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

    async def _create_and_cable_dc_role_share(
        self,
        *,
        role: str,
        quantity: int,
        template: Template,
        pod: Any,
    ) -> list[str]:
        """Create one pod's share of a DC-level border-leaf/firewall/load-balancer
        entry, deployment_id=pod.id (not the DC) so the devices are cabled into
        that pod's own spine fabric even though declared once at DC level.

        Border-leaf gets a loopback (from the pod's own loopback pool) and
        underlay/overlay BGP into that pod's spines — same treatment leaf/tor/
        border-leaf all got pre-refactor via rack.py's _generate_spine_attached_role.
        Firewall/load-balancer stay device-only (no loopback, no routing) — this
        matches their pre-refactor behavior; cabling them to border-leaf is a
        separate, still-deferred follow-up (see connectivity_mode's docstring).
        """
        allocate_loopback = role == "border-leaf"
        pod_loopback_pool_id = pod.loopback_pool.id if getattr(pod, "loopback_pool", None) else None
        device_options = DeviceOptions(
            indexes=[self.data.index, pod.index.value],
            allocate_loopback=allocate_loopback,
            loopback_pool=pod_loopback_pool_id,
            loopback_prefix_length=128 if self._is_ipv6 else 32,
        )
        if role == "load-balancer":
            # create_devices()'s default group_name is f"{device_role}s" = "load-balancers",
            # but the bootstrap group is named "loadbalancers" (no hyphen) — override.
            device_options["group_name"] = "loadbalancers"
        devices = await self.create_devices(
            deployment_id=pod.id,
            device_role=role,
            amount=quantity,
            template=template.model_dump(),
            naming_convention=cast(Literal["standard", "hierarchical", "flat"], self.data.naming_convention.lower()),
            options=device_options,
        )

        if role == "border-leaf":
            uplink_names = [iface.name for iface in template.interfaces if iface.role == "uplink"]
            if not uplink_names:
                self.logger.error(
                    f"DC {self.fabric_name}: border-leaf template has no uplink interfaces — cannot cable fabric."
                )
                return devices
            spine_names, spine_downlinks = await self._pod_spine_fabric(pod.id)
            if not spine_names or not spine_downlinks:
                self.logger.error(
                    f"DC {self.fabric_name}: no spine devices/downlinks found in pod (id={pod.id}) — "
                    "cannot cable border-leaf fabric uplinks."
                )
                return devices
            # Deterministic offset (design capacity, not live cable count) so this
            # never races the rack generator's own leaf/tor cabling into the same
            # spines; already-placed border-leafs THIS run (multiple fabric_templates
            # entries landing on the same pod) stack on top, tracked in-memory.
            if not hasattr(self, "_border_leaf_placed"):
                self._border_leaf_placed: dict[str, int] = {}
            placed = self._border_leaf_placed.get(pod.id, 0)
            offset = self._pod_spine_port_reservation(pod) + placed
            self._border_leaf_placed[pod.id] = placed + quantity
            p2p_pairs = await self.create_cabling(
                bottom_devices=devices,
                bottom_interfaces=uplink_names,
                top_devices=spine_names,
                top_interfaces=spine_downlinks,
                strategy="rack",
                options=CablingOptions(cabling_offset=offset),
            )
            pod_asn_pool_id = pod.asn_pool.id if getattr(pod, "asn_pool", None) else None
            routing_opts = RoutingOptions(design=self._dc_design, asn_pool=pod_asn_pool_id)
            if routing_opts.get("design"):
                await self.create_routing(
                    bottom_devices=devices,
                    top_devices=spine_names,
                    options=routing_opts,
                    p2p_interfaces=p2p_pairs,
                    bottom_role="border-leaf",
                    top_role="spine",
                )

        return devices

    async def _generate_dc_scoped_fabric_devices(self) -> None:
        """Split border-leaf/firewall/load-balancer fabric_templates entries
        evenly across the DC's existing pods (first pods get the remainder) and
        create+cable each pod's share. No-ops if no pods exist yet — a DC with
        zero pods has nothing to cable into. A pod added later than this DC's
        declaration needs an explicit dc_pod_cascade run to get its retroactive
        share — same as any other structural DC-level change (see tasks/demo.py's
        own manual dc_pod_cascade calls after bulk loads); not auto-triggered from
        pod.py's add_pod, which would otherwise fire a concurrent DC-level
        re-bootstrap on every single pod creation during a bulk multi-pod load."""
        existing_pods = getattr(self, "_existing_pods", [])
        if not existing_pods:
            self.logger.info(f"DC {self.fabric_name}: no pods yet — deferring border-leaf/FW/LB placement")
            return

        sorted_pods = sorted(existing_pods, key=lambda p: p.index.value)
        self._border_leaf_placed: dict[str, int] = {}
        max_border_leafs_per_pod = self.data.design.max_border_leafs_per_pod if self.data.design else None
        for role, entries in (
            ("border-leaf", self.data.border_leaf_templates),
            ("firewall", self.data.firewall_templates),
            ("load-balancer", self.data.load_balancer_templates),
        ):
            ha_kind = _DC_HA_KINDS.get(role)
            for entry in entries:
                shares = self._split_quantity(entry.quantity, len(sorted_pods))
                for pod, share in zip(sorted_pods, shares):
                    if share == 0:
                        continue
                    if (
                        role == "border-leaf"
                        and max_border_leafs_per_pod is not None
                        and share > max_border_leafs_per_pod
                    ):
                        self.logger.error(
                            f"DC {self.fabric_name}: pod {pod.name.value}'s border-leaf share ({share}) "
                            f"exceeds design.max_border_leafs_per_pod ({max_border_leafs_per_pod}) — skipping."
                        )
                        continue
                    devices = await self._create_and_cable_dc_role_share(
                        role=role,
                        quantity=share,
                        template=entry.template,
                        pod=pod,
                    )
                    if ha_kind and share == 2:
                        await self._ensure_dc_ha_pair(devices, ha_kind=ha_kind, role_label=role)

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
