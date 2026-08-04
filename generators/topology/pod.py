"""Infrastructure generator for pod topology creation."""

import asyncio
from typing import Any, Literal, TypedDict, cast

from utils.data_cleaning import clean_data

from ..cabling import CablingMixin
from ..common import CablingOptions, CommonGenerator, DeviceOptions, RoutingOptions
from ..dc_config import host_bits_to_prefix_length, resolve_dc_size_layout
from ..devices import DeviceMixin
from ..helpers.naming import DeviceNamingConfig
from ..helpers.rack import expected_device_names
from ..helpers.routing import RoutingStrategy
from ..helpers.routing import p2p_addressing as p2p_addressing_for
from ..helpers.routing import underlay_is_dual_stack, underlay_is_ipv6
from ..helpers.template_interfaces import template_interface_names_by_role
from ..pod_config import resolve_pod_layout, spine_slot_role, spine_slot_templates, templates_by_role
from ..pools import PoolMixin
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, TopologyPod
from ..routing import RoutingMixin

_SIBLING_SPINE_MAX_RETRIES = 10
_SIBLING_SPINE_RETRY_DELAY = 3.0
_DC_ASN_POOL_MAX_RETRIES = 10
_DC_ASN_POOL_RETRY_DELAY = 3.0

# FW/LB "uplink" interfaces face border-spine; border-spine's "firewall"/
# "load-balancer" interfaces are the dedicated counterpart ports — same
# convention as dc.py's DC-wide border-leaf, just scoped to one pod's own
# border-spine instead. See DCS-7050CX3-32C-R_BORDER_SPINE's template for
# the canonical port layout.
_BS_ROLE_FOR: dict[str, str] = {"firewall": "firewall", "load-balancer": "load-balancer"}

# device_role -> HA node kind. HA pairing itself happens inside
# DeviceMixin.create_devices() (see DeviceOptions.ha_kind) — only the right
# kind per role needs picking here.
_HA_KIND_BY_ROLE: dict[str, str] = {
    "firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}


class TopologyPodParentData(TypedDict, total=False):
    """Shape of the nested TopologyDataCenter projection under
    TopologyPod.parent (see queries/topology/add/pod.gql's DataCenterFields
    fragment) — declared here, at the read site, instead of a shared
    Pydantic model file."""

    id: str
    name: str
    index: int
    naming_convention: str
    fabric_interface_sorting_method: str
    connectivity_mode: str
    management_mode: str
    routing_strategy: str
    underlay_protocol: str
    fabric_templates: list[dict[str, Any]]
    size: str
    fabric_asn_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    devices: list[Any]


class TopologyPodData(TypedDict, total=False):
    """Shape of one clean_data()-processed TopologyPod entry (see
    queries/topology/add/pod.gql) — declared here, at the read site, instead
    of a shared Pydantic model file. No runtime validation: a missing/
    mistyped key surfaces as ``KeyError`` at first read, caught by
    generate()'s own except clause below."""

    id: str
    name: str
    index: int
    deployment_type: Literal["middle_rack", "tor", "mixed"]
    layout: str
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    rack_numbering_start_index: int
    leaf_link_numbering_start: int
    spine_link_numbering_start: int
    tor_link_numbering_start: int
    fabric_templates: list[dict[str, Any]]
    parent: TopologyPodParentData
    loopback_pool: dict[str, Any] | None
    prefix_pool: dict[str, Any] | None
    asn_pool: dict[str, Any] | None


def _base_offset(numbering_start: int) -> int:
    return max(0, numbering_start - 1)


class PodTopologyGenerator(PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator):
    """Generate pod topology with resource pools and spine infrastructure.

    Creates resource pools (technical and management) and creates spine devices
    within a pod topology.

    This is a pure bootstrap: it never fans out to add_rack itself. That fan-out
    lives in PodRackCascadeGenerator, a subclass that reuses this generate() via
    super() — see generators/topology/pod_rack_cascade.py's docstring for why.

    Waits for an in-flight add_dc/dc_pod_cascade run on its parent DC before
    reading DC-level data (see CommonGenerator.wait_for_parent_generator_and_refetch).
    """

    data: TopologyPodData

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure."""

        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod Deployment data found in GraphQL response")
                return

            # This bootstrap reads DC-level data (super-spine devices, ASN pool,
            # management pool) written by add_dc/dc_pod_cascade. If either is
            # still running for our parent DC (e.g. a bulk DC+pod+rack load, or a
            # structural DC change firing add_dc + dc_pod_cascade in parallel),
            # wait for it and re-parse rather than risk running on partial data.
            dc_id = deployment_list[0].get("parent", {}).get("id")
            if dc_id:
                for parent_generator in ("add_dc", "dc_pod_cascade"):
                    refreshed = await self.wait_for_parent_generator_and_refetch(parent_generator, dc_id)
                    if refreshed is not None:
                        data = refreshed
                        deployment_list = clean_data(data).get("TopologyPod", [])
                        if not deployment_list:
                            self.logger.error("No Pod Deployment data found in GraphQL response")
                            return

            self.data = cast(TopologyPodData, deployment_list[0])
            # No Pydantic validation left to catch a malformed/partial GraphQL
            # response — force-read every field generate() treats as required
            # here, inside the try, so a missing one raises KeyError in the
            # same place the old PodModel(**deployment_list[0]) construction did.
            pod_id = self.data["id"]
            pod_name = self.data["name"]
            pod_index = self.data["index"]
            dc = self.data["parent"]
            dc_id = dc["id"]
            dc_name = dc["name"]

            # add_dc's own task can finish (dropping out of the tasklist check above)
            # a moment before its write of fabric_asn_pool is visible to this query —
            # the wait above only catches "still running", not that narrow window.
            # Poll for the pool directly when the DC's routing strategy requires one,
            # so the pre-seed BGP call below never silently skips for a missing pool
            # that's actually just not visible yet.
            needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in (
                RoutingStrategy.EBGP_EBGP.value,
                RoutingStrategy.EBGP_IBGP.value,
            )
            for attempt in range(_DC_ASN_POOL_MAX_RETRIES):
                if not needs_asn_pool or dc.get("fabric_asn_pool"):
                    break
                if attempt < _DC_ASN_POOL_MAX_RETRIES - 1:
                    delay = self._retry_delay(_DC_ASN_POOL_RETRY_DELAY, attempt)
                    self.logger.info(
                        f"Pod {pod_name}: parent DC's fabric_asn_pool not visible yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_DC_ASN_POOL_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    data = await self.collect_data()
                    deployment_list = clean_data(data).get("TopologyPod", [])
                    if not deployment_list:
                        self.logger.error("No Pod Deployment data found in GraphQL response")
                        return
                    self.data = cast(TopologyPodData, deployment_list[0])
                    dc = self.data["parent"]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {pod_name}")

        if dc.get("management_mode", "fully_managed") == "managed_by_controller":
            self.logger.info(f"Pod {pod_name}: parent DC management_mode=managed_by_controller — skipping generator")
            return
        self.deployment_id = dc_id  # Store for cable linking
        self.pod_name = pod_name.lower()
        self.fabric_name = dc_name.lower()

        design = resolve_pod_layout(self.data["layout"])

        deployment_type = self.data["deployment_type"]

        # spine and border-spine fill the same slot in a pod's fabric — never
        # both (border-spine collapses spine+border-leaf into one device for
        # micro-fabrics, see role: border-spine in schemas/extensions/topology/
        # topology_dc.yml). Whichever is declared drives device creation,
        # naming, and leaf/tor cabling identically; only border-spine also
        # gets its own per-pod firewall/load-balancer (see
        # _generate_pod_scoped_border_services below).
        fabric_templates = self.data.get("fabric_templates", [])
        spine_entries = spine_slot_templates(fabric_templates)
        spine_role = spine_slot_role(fabric_templates)
        if not spine_entries:
            self.logger.error(f"Pod {pod_name}: no spine/border-spine fabric_templates entries — cannot build fabric")
            return
        spine_count = sum(entry["quantity"] for entry in spine_entries)
        naming_conv = cast(
            Literal["standard", "hierarchical", "flat", "computed"],
            dc.get("naming_convention", "standard"),
        )

        if spine_count > design["max_spines_per_pod"]:
            self.logger.error(
                f"Pod {pod_name} requests {spine_count} {spine_role}s "
                f"but pod design '{design['name']}' allows at most {design['max_spines_per_pod']}"
            )
            return

        indexes: list[int] = [dc["index"], pod_index]

        # Fixed per-pod pool sizes from this pod's own layout (see
        # pod_config.py's technical_host_bits/loopback_host_bits) — sized
        # generously for the layout's own worst-case device count, instead of
        # computed per pod from its live spine/leaf/tor counts. Host bits are
        # protocol-agnostic — the real prefix length depends on this DC's
        # underlay_protocol (IPv4 /32 vs IPv6 /128 address space).
        dc_underlay_protocol = dc.get("underlay_protocol", "ipv6")
        is_ipv6 = underlay_is_ipv6(dc_underlay_protocol)
        is_dual_stack = underlay_is_dual_stack(dc_underlay_protocol)
        p2p_addressing_value = p2p_addressing_for(dc_underlay_protocol)
        dc_design = resolve_dc_size_layout(dc["size"])

        # Dual-stack pods keep loopback on IPv4 (same convention as
        # allocate_resource_pools' own dual_stack handling) — only technical/
        # P2P moves to IPv6.
        pool_sizes = {
            "technical": host_bits_to_prefix_length(design["technical_host_bits"], ipv6=is_ipv6 or is_dual_stack),
            "loopback": host_bits_to_prefix_length(design["loopback_host_bits"], ipv6=is_ipv6),
        }

        self.logger.info(
            f"Pod {pod_name} pool sizes: technical=/{pool_sizes['technical']}, "
            f"loopback=/{pool_sizes['loopback']} (p2p={p2p_addressing_value}, "
            f"ipv6={is_ipv6}, dual_stack={is_dual_stack}, deployment={deployment_type})"
        )

        # Allocate/upsert pools (idempotent via identifier + allow_upsert)
        # Must always run so objects are tracked by the generator framework
        pod_pools = await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=pool_sizes,
        )

        # Reference DC-level ASN pool (one pool per DC, shared by all devices)
        # DC generator creates the pool; pod just references it for routing and propagates to pod.asn_pool
        dc_asn_pool_id: str | None = None
        dc_asn_pool_name: str | None = None
        dc_fabric_asn_pool = dc.get("fabric_asn_pool")
        if dc_fabric_asn_pool and dc_fabric_asn_pool.get("name"):
            dc_asn_pool_id = dc_fabric_asn_pool["id"]
            dc_asn_pool_name = dc_fabric_asn_pool["name"]

            # Propagate DC pool reference to pod so rack generator can find it via pod.asn_pool
            pod_obj = await self.client.get(kind=TopologyPod, id=pod_id)
            if pod_obj:
                pod_obj.asn_pool = {"id": dc_fabric_asn_pool["id"]}
                await pod_obj.save(allow_upsert=True)
                self.logger.info(f"Pod {pod_name}: linked to DC ASN pool '{dc_asn_pool_name}'")

        # Pass management pool ID from DC parent (create_devices resolves ID to SDK object)
        dc_management_pool = dc.get("management_pool")
        management_pool_id = dc_management_pool["id"] if dc_management_pool else None

        # border-spine collapses spine+border-leaf into one device for
        # micro-fabrics (see spine_role's own docstring above) — it's a
        # DC-level fabric tier like border-leaf/super-spine/hyper-spine, so
        # it deploys to the DC, not this pod, even though it's created here
        # (pod.py owns spine-slot context). Plain "spine" stays pod-scoped.
        spine_deployment_id = dc_id if spine_role == "border-spine" else pod_id

        spines: list[str] = []
        for entry in spine_entries:
            entry_spines = await self.create_devices(
                deployment_id=spine_deployment_id,
                device_role=spine_role,
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_conv,
                options=DeviceOptions(
                    indexes=indexes,
                    allocate_loopback=True,
                    loopback_pool=pod_pools.get("loopback"),
                    loopback_prefix_length=128 if is_ipv6 else 32,
                    management_pool=management_pool_id,
                ),
            )
            spines.extend(entry_spines)

        # Interface names come from the first entry's template only — same accepted
        # limitation as dc.py's super-spine device creation: assumes every spine
        # template shares consistent downlink-interface naming.
        spine_template = spine_entries[0]["template"]

        super_spine_devices = [device.name for device in dc.get("devices", [])]
        dc_super_spine_entries = templates_by_role(dc.get("fabric_templates", []), "super-spine")
        super_spine_interfaces = (
            [iface["name"] for iface in dc_super_spine_entries[0]["template"].get("interfaces", [])]
            if dc_super_spine_entries
            else []
        )

        # Pre-seed spine eBGP processes before cabling exists so parallel rack generators
        # find them and don't try to create duplicates.
        # OSPF_IBGP is excluded: spine overlay BGP needs overlay_as_id (resolved inside
        # create_routing via _resolve_shared_objects) and is created in the post-cable call below.
        if dc_asn_pool_id and dc.get("routing_strategy", "ebgp-ebgp") in (
            RoutingStrategy.EBGP_EBGP.value,
            RoutingStrategy.EBGP_IBGP.value,
        ):
            await self.create_routing(
                bottom_devices=spines,
                top_devices=super_spine_devices,
                options=RoutingOptions(design=dc, asn_pool=dc_asn_pool_id),
                p2p_interfaces=[],
                bottom_role=spine_role,
                top_role="super-spine",
            )

        spine_interfaces = template_interface_names_by_role(
            interfaces=spine_template.get("interfaces", []),
            role="uplink",
        )
        if not spine_interfaces:
            self.logger.error(
                f"Pod {pod_name}: No uplink interfaces found in spine template. "
                "Cannot create spine-to-super-spine cabling."
            )
            return

        # Skip cabling if no super-spines (single-pod DC scenario)
        skip_cabling = False
        if not super_spine_devices or not super_spine_interfaces:
            self.logger.info(
                f"Pod {pod_name}: Skipping spine-to-super-spine cabling (single-pod DC or no super-spines)"
            )
            skip_cabling = True

            # No super-spine tier: pre-seed this pod's own spine overlay BGP now
            # (skip_underlay=True — underlay comes from rack.py's leaf<->spine cabling)
            # so it exists before any rack generator's leaf-to-spine routing call runs.
            # Those calls treat spines as top_devices and rely on an existing overlay
            # BGP process for them.
            await self.create_routing(
                bottom_devices=spines,
                top_devices=[],
                options=RoutingOptions(design=dc, asn_pool=dc_asn_pool_id, skip_underlay=True),
                p2p_interfaces=[],
                bottom_role=spine_role,
            )

            await self._cable_to_existing_sibling_pods(
                spines=spines,
                spine_interfaces=spine_interfaces,
                spine_role=spine_role,
                dc=dc,
                dc_asn_pool_id=dc_asn_pool_id,
                pod_pools=pod_pools,
                is_ipv6=is_ipv6,
            )

        if not skip_cabling:
            dc_max_spines = dc_design["max_spines_per_pod"]
            cabling_offset = _base_offset(self.data.get("spine_link_numbering_start", 1)) + (
                (pod_index - 1) * dc_max_spines
            )
            p2p_prefix_length = 127 if is_ipv6 else 31
            # design is always set here (needed for routing_strategy lookup inside
            # create_routing) — asn_pool is only meaningful for eBGP strategies
            # (ospf-ibgp draws no per-device ASN from a pool), so omit it rather
            # than pass None (matches dc.py's own RoutingOptions construction).
            routing_opts = RoutingOptions(design=dc)
            if dc_asn_pool_id:
                routing_opts["asn_pool"] = dc_asn_pool_id
            p2p_pairs = await self.create_cabling(
                bottom_devices=spines,
                bottom_interfaces=spine_interfaces,
                top_devices=super_spine_devices,
                top_interfaces=super_spine_interfaces,
                strategy="pod",
                options=CablingOptions(
                    cabling_offset=cabling_offset,
                    pool=pod_pools.get("technical"),
                    p2p_prefix_length=p2p_prefix_length,
                ),
                bottom_sorting=cast(
                    Literal["top_down", "bottom_up"], self.data.get("spine_interface_sorting_method", "bottom_up")
                ),
                top_sorting=cast(
                    Literal["top_down", "bottom_up"], dc.get("fabric_interface_sorting_method", "bottom_up")
                ),
            )
            await self.create_routing(
                bottom_devices=spines,
                top_devices=super_spine_devices,
                options=routing_opts,
                p2p_interfaces=p2p_pairs,
                bottom_role=spine_role,
                top_role="super-spine",
            )
        # When there are no super-spines to cable to (skip_cabling), inter-pod
        # back-to-back spine cabling is handled above by _cable_to_existing_sibling_pods.
        # Fan-out to add_rack is handled by the sibling pod_rack_cascade generator,
        # not here — see PodTopologyGenerator's class docstring.

        # Cable+route this pod's own border-leaf devices (created by dc.py's
        # _create_border_leaf_devices with deployment_id=this pod, but NOT cabled
        # there — pod.py already owns spine context, so it does the uplink cabling
        # during its own bootstrap instead of dc.py re-deriving it).
        await self._cable_border_leafs_to_spines(
            spines=spines,
            spine_downlink_interfaces=template_interface_names_by_role(
                interfaces=spine_template.get("interfaces", []),
                role="downlink",
            ),
            dc=dc,
            dc_asn_pool_id=dc_asn_pool_id,
            pod_pools=pod_pools,
            is_ipv6=is_ipv6,
        )

        # This pod's own firewall/load-balancer, only present when spine_role
        # is "border-spine" (micro-fabric mode) — a pod with a plain spine
        # tier declares none of these (see PodModel.firewall_templates/
        # load_balancer_templates).
        if spine_role == "border-spine":
            await self._generate_pod_scoped_border_services(spines=spines)

        # A pod added AFTER its DC already declared border-leaf/firewall/load-balancer
        # fabric_templates needs a retroactive share of those devices — same as any
        # other structural DC-level reconciliation, this requires an explicit
        # dc_pod_cascade run (see tasks/demo.py's own manual dc_pod_cascade calls
        # after bulk loads). Deliberately NOT auto-triggered from here: every
        # add_pod run (including each pod during a bulk multi-DC load) would fire
        # its own concurrent dc_pod_cascade re-run against the same DC-level pools/
        # ASN pool, racing the DC's own already-in-flight bootstrap.

    async def _generate_pod_scoped_border_services(self, *, spines: list[str]) -> None:
        """Create this pod's own firewall/load-balancer and cable them to this
        pod's border-spine devices. Per-pod counterpart to dc.py's DC-wide
        _generate_dc_scoped_fabric_devices — a border-spine micro-fabric has
        no DC-level border-leaf tier to sit in front of, so each pod's own
        border-spine gets its own dedicated FW/LB instead (see
        PodModel.firewall_templates/load_balancer_templates and
        TopologyPodDesign "S_BORDER_SPINE_POD"). No-ops if this pod declares
        neither (the common case for a plain-spine pod, and legal even for
        a border-spine pod with no FW/LB of its own)."""
        fabric_templates = self.data.get("fabric_templates", [])
        firewall_entries = templates_by_role(fabric_templates, "firewall")
        load_balancer_entries = templates_by_role(fabric_templates, "load-balancer")
        if not firewall_entries and not load_balancer_entries:
            return

        dc = self.data["parent"]
        naming_conv = cast(
            Literal["standard", "hierarchical", "flat", "computed"],
            dc.get("naming_convention", "standard").lower(),
        )
        device_indexes = [dc["index"], self.data["index"]]

        firewall_names = await self._create_role_devices(
            role="firewall",
            entries=firewall_entries,
            deployment_id=self.data["id"],
            naming_convention=naming_conv,
            indexes=device_indexes,
        )
        load_balancer_names = await self._create_role_devices(
            role="load-balancer",
            entries=load_balancer_entries,
            deployment_id=self.data["id"],
            naming_convention=naming_conv,
            indexes=device_indexes,
        )
        await self._cable_border_services(
            border_role_for=_BS_ROLE_FOR,
            connectivity_mode=cast(Literal["pbr", "inline"], dc.get("connectivity_mode", "pbr")),
            border_names=spines,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )

    async def _create_role_devices(
        self,
        *,
        role: Literal["firewall", "load-balancer"],
        entries: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: Literal["standard", "hierarchical", "flat", "computed"],
        indexes: list[int],
    ) -> list[str]:
        """Create firewall/load-balancer devices for this pod's own border-spine
        (deployment_id=pod.id). Each entry's devices are paired into an HA
        domain two-at-a-time by create_devices() itself (any quantity, not
        just 2 — an odd device is left unpaired). No loopback allocation — not
        part of underlay/overlay routing."""
        device_options = DeviceOptions(indexes=indexes, ha_kind=_HA_KIND_BY_ROLE[role])
        if role == "load-balancer":
            # create_devices()'s default group_name is f"{device_role}s" = "load-balancers",
            # but the bootstrap group is named "loadbalancers" (no hyphen) — override.
            device_options["group_name"] = "loadbalancers"

        all_names: list[str] = []
        for entry in entries:
            names = await self.create_devices(
                deployment_id=deployment_id,
                device_role=role,
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_convention,
                options=device_options,
            )
            all_names.extend(names)

        return all_names

    async def _cable_border_leafs_to_spines(
        self,
        *,
        spines: list[str],
        spine_downlink_interfaces: list[str],
        dc: Any,
        dc_asn_pool_id: str | None,
        pod_pools: dict[str, Any],
        is_ipv6: bool,
    ) -> None:
        """Cable+route this pod's own border-leaf devices (already created by
        dc.py, deployment_id=the DC — border-leaf is a DC-level fabric tier,
        like super-spine/hyper-spine) to this pod's spines. Devices are
        matched by name, not by deployment: names are deterministic
        ([dc.index, pod.index, role_index]), so querying by name__values for
        every possible role_index up to the DC's max_border_leafs_per_fabric
        (a safe upper bound — any pod's real share is at most that) finds
        exactly this pod's own border-leafs among the DC-wide device pool,
        without needing a per-pod relationship on the device itself. Offset
        is deterministic (design's leaf/tor CAPACITY, not live device count)
        so it never collides with rack.py's own leaf/tor offsets into the
        same spine downlink ports — rack.py's offsets start at 0 and fill up
        to capacity; border-leaf's offset starts exactly where that capacity
        ends. No-ops if this pod has no border-leaf devices (the common case
        — most pods don't host any)."""
        max_border_leafs_per_fabric = resolve_dc_size_layout(dc["size"])["max_border_leafs_per_fabric"]
        if max_border_leafs_per_fabric <= 0:
            return
        naming = DeviceNamingConfig(
            strategy=cast(
                Literal["standard", "hierarchical", "flat", "computed"], dc.get("naming_convention", "standard")
            )
        )
        candidate_names = expected_device_names(
            naming_config=naming,
            fabric_name=self.fabric_name,
            device_indexes=[dc["index"], self.data["index"]],
            role="border-leaf",
            quantity=max_border_leafs_per_fabric,
        )
        border_leafs = await self.client.filters(
            kind=DcimPhysicalDevice, name__values=sorted(candidate_names), role__value="border-leaf"
        )
        if not border_leafs:
            return
        border_leaf_names = sorted(bl.name.value for bl in border_leafs)

        bl_uplinks = await self.client.filters(
            kind=DcimPhysicalInterface, device__name__values=border_leaf_names, role__value="uplink"
        )
        bl_uplink_names = sorted({iface.name.value for iface in bl_uplinks})
        if not bl_uplink_names or not spine_downlink_interfaces:
            self.logger.error(
                f"Pod {self.data['name']}: cannot cable border-leaf uplinks — "
                f"bl_uplinks={len(bl_uplink_names)}, spine_downlinks={len(spine_downlink_interfaces)}."
            )
            return

        design = resolve_pod_layout(self.data["layout"])
        spine_link_base_offset = _base_offset(self.data.get("spine_link_numbering_start", 1))
        if self.data["deployment_type"] == "tor":
            offset = spine_link_base_offset + (
                design["rows"] * design["compute_racks_per_row"] * design["max_tors_per_compute_rack"]
            )
        else:
            offset = spine_link_base_offset + (
                design["rows"] * design["network_racks_per_row"] * design["max_leafs_per_network_rack"]
            )

        p2p_prefix_length = 127 if is_ipv6 else 31
        p2p_pairs = await self.create_cabling(
            bottom_devices=border_leaf_names,
            bottom_interfaces=bl_uplink_names,
            top_devices=spines,
            top_interfaces=spine_downlink_interfaces,
            strategy="rack",
            options=CablingOptions(
                cabling_offset=offset,
                pool=pod_pools.get("technical"),
                p2p_prefix_length=p2p_prefix_length,
            ),
        )
        # design is always set (needed for routing_strategy lookup inside
        # create_routing) — asn_pool is only meaningful for eBGP strategies
        # (ospf-ibgp draws no per-device ASN from a pool). Gating the call
        # itself on dc_asn_pool_id would silently skip routing entirely for
        # ospf-ibgp fabrics — create_routing()'s own routing_strategy check
        # is the real gate.
        routing_opts = RoutingOptions(design=dc)
        if dc_asn_pool_id:
            routing_opts["asn_pool"] = dc_asn_pool_id
        await self.create_routing(
            bottom_devices=border_leaf_names,
            top_devices=spines,
            options=routing_opts,
            p2p_interfaces=p2p_pairs,
            bottom_role="border-leaf",
            top_role="spine",
        )

    async def _cable_to_existing_sibling_pods(
        self,
        spines: list[str],
        spine_interfaces: list[str],
        spine_role: str,
        dc: Any,
        dc_asn_pool_id: str | None,
        pod_pools: dict[str, Any],
        is_ipv6: bool,
    ) -> None:
        """Cable this pod's spines to every EXISTING lower-index sibling pod (back-to-back mesh).

        spine_role is this pod's own spine-slot role ("spine" or
        "border-spine") — every pod in a back-to-back DC uses the same one
        (a DC's design fixes the pattern DC-wide), so it's also used to find
        each sibling's own spine-slot devices below.

        Decentralized: each pod cables itself to its lower-index siblings, rather
        than a DC-level step waiting for every pod to finish before cabling the
        whole mesh in one pass. Only the higher-index pod in any given pair ever
        writes that pair's cabling/routing — pod 1 never initiates anything, pod 2
        only cables to pod 1, pod 3 to pods 1 and 2, etc. — so there is exactly one
        writer per pair regardless of how many pods run concurrently; the only
        remaining concern is data availability (has the sibling's spine/overlay
        BGP landed yet), which create_cabling's own interface-readiness retry and
        create_routing's overlay-BGP-readiness retry already absorb.

        This also makes incremental single-pod-add correct for free: a pod added
        later simply finds its existing lower-index siblings already fully formed
        and cables to them immediately — no dc.py-level orchestration required.
        """
        pod_name = self.data["name"]
        pod_index = self.data["index"]
        if not spine_interfaces:
            self.logger.warning(f"Pod {pod_name}: no spine uplink interfaces — skipping inter-pod mesh cabling")
            return

        siblings = await self.client.filters(kind=TopologyPod, parent__ids=[dc["id"]])
        lower_siblings = [s for s in siblings if s.index.value < pod_index]
        if not lower_siblings:
            self.logger.info(f"Pod {pod_name}: no lower-index sibling pods yet — nothing to mesh-cable")
            return

        p2p_prefix_length = 127 if is_ipv6 else 31
        routing_opts = RoutingOptions(design=dc, asn_pool=dc_asn_pool_id)
        sorted_lower_siblings = sorted(lower_siblings, key=lambda s: s.index.value)
        for sibling_slot, sibling in enumerate(sorted_lower_siblings):
            # Retry: during INITIAL bulk DC creation, every pod's TopologyPod object
            # already exists (loaded together before any generator runs), but a lower-
            # index sibling's own add_pod run may not have reached spine creation yet.
            # For an incremental single-pod-add, the sibling is already fully formed
            # and this resolves on the first attempt. Mirrors create_cabling's own
            # interface-readiness retry.
            sibling_spines: list[str] = []
            sibling_uplink_names: list[str] = []
            for attempt in range(_SIBLING_SPINE_MAX_RETRIES):
                sibling_spines_devices = await self.client.filters(
                    kind=DcimPhysicalDevice,
                    deployment__ids=[sibling.id],
                    role__value=spine_role,
                )
                sibling_spines = [s.name.value for s in sibling_spines_devices]
                if sibling_spines:
                    sibling_uplinks = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__name__values=sibling_spines,
                        role__value="uplink",
                    )
                    sibling_uplink_names = sorted({iface.name.value for iface in sibling_uplinks})
                    if sibling_uplink_names:
                        break
                if attempt < _SIBLING_SPINE_MAX_RETRIES - 1:
                    delay = self._retry_delay(_SIBLING_SPINE_RETRY_DELAY, attempt)
                    self.logger.info(
                        f"Pod {pod_name}: sibling pod idx={sibling.index.value} spines/uplinks not ready yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_SIBLING_SPINE_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)

            if not sibling_spines or not sibling_uplink_names:
                self.logger.warning(
                    f"Pod {pod_name}: sibling pod idx={sibling.index.value} still has no spine "
                    f"devices/uplinks after {_SIBLING_SPINE_MAX_RETRIES} attempts — skipping this pair"
                )
                continue

            # Each sibling-pod pair gets a deterministic local uplink slot range.
            # Reserve exactly the number of links this pair consumes per spine pair.
            links_per_sibling = len(sibling_spines)
            slot_start = sibling_slot * links_per_sibling
            sibling_local_uplinks = spine_interfaces[slot_start : slot_start + links_per_sibling]
            if len(sibling_local_uplinks) < links_per_sibling:
                self.logger.error(
                    f"Pod {pod_name}: insufficient local {spine_role} uplinks for inter-pod mesh. "
                    f"Needed {links_per_sibling} free uplink name(s) for sibling pod idx={sibling.index.value}, "
                    f"but only {len(sibling_local_uplinks)} remain. "
                    "Increase uplink-role interfaces in the spine template or reduce mesh fan-out."
                )
                return

            # Peer-facing offset must be unique per (source pod, sibling pod) pair,
            # otherwise multiple higher-index pods can select the same sibling
            # spine uplink slot and collide on one endpoint.
            pair_slot = max(0, pod_index - sibling.index.value - 1)
            spine_link_base_offset = _base_offset(self.data.get("spine_link_numbering_start", 1))
            cabling_offset = spine_link_base_offset + (pair_slot * links_per_sibling)
            self.logger.info(
                f"Pod {pod_name} (idx={pod_index}): cabling to sibling pod idx={sibling.index.value} "
                f"[offset={cabling_offset}, pair_slot={pair_slot}, slot_start={slot_start}, "
                f"local_uplinks={sibling_local_uplinks}]"
            )
            p2p_pairs = await self.create_cabling(
                bottom_devices=spines,
                bottom_interfaces=sibling_local_uplinks,
                top_devices=sibling_spines,
                top_interfaces=sibling_uplink_names,
                strategy="pod",
                options=CablingOptions(
                    cabling_offset=cabling_offset,
                    pool=pod_pools.get("technical"),
                    p2p_prefix_length=p2p_prefix_length,
                ),
            )
            await self.create_routing(
                bottom_devices=spines,
                top_devices=sibling_spines,
                options=routing_opts,
                p2p_interfaces=p2p_pairs,
                bottom_role=spine_role,
                top_role=spine_role,
            )
