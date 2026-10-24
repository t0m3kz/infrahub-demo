"""Infrastructure generator for pod topology creation."""

import asyncio
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import BORDER_ROLE_FOR_SERVICES, CablingMixin
from ..devices import DeviceMixin
from ..fabric_sizing import host_bits_to_prefix_length, max_sibling_spines, pod_loopback_bits, pod_technical_bits
from ..helpers.naming import DeviceNamingConfig
from ..helpers.rack import expected_device_names
from ..helpers.routing import RoutingStrategy, loopback_prefix_length, p2p_prefix_length, underlay_pool_is_ipv6
from ..helpers.template_interfaces import template_interface_names_by_role
from ..pod_config import (
    pod_deployment_type,
    spine_downlink_layout,
    spine_slot_role,
    spine_slot_templates,
    templates_by_role,
)
from ..pools import PoolMixin
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface
from ..routing import RoutingMixin
from ..types import CablingOptions, DeviceOptions, RoutingOptions, naming_convention_of
from .fabric_shared import DC_GENERATORS, ServiceDevicesMixin
from .pod_mesh import PodMeshMixin, mesh_slot_width, pod_routing_options

_DC_READY_MAX_RETRIES = 10
_DC_READY_RETRY_DELAY = 3.0
# Short/bounded on purpose: most pods host zero border-leaf devices (only
# the design-designated pod(s) do), so this fires on every pod's bootstrap —
# a long retry here would slow down the common no-op case for every pod that
# will never find any, just to bridge the rare, short residual lag on the one
# that actually hosts them (border-leaf is created in the same add_dc pass as
# super-spine, so it's usually already visible by the time _DC_READY_* above
# is satisfied).
_BORDER_LEAF_MAX_RETRIES = 3
_BORDER_LEAF_RETRY_DELAY = 2.0

# FW/LB "uplink" interfaces face border-spine; border-spine's "firewall"/
# "load-balancer" interfaces are the dedicated counterpart ports — same
# convention as dc.py's DC-wide border-leaf, just scoped to one pod's own
# border-spine instead. See DCS-7050CX3-32C-R_BORDER_SPINE's template for
# the canonical port layout. BORDER_ROLE_FOR_SERVICES (generators/
# connections.py) supplies the border_role_for mapping below.


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
    routing_strategy: str
    underlay_protocol: str
    fabric_templates: list[dict[str, Any]]
    design: dict[str, Any]
    children: list[dict[str, Any]]
    fabric_asn_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    devices: list[Any]
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


class TopologyPodData(TypedDict, total=False):
    """Shape of one clean_data()-processed TopologyPod entry (see
    queries/topology/add/pod.gql) — declared here, at the read site, instead
    of a shared Pydantic model file. No runtime validation: a missing/
    mistyped key surfaces as ``KeyError`` at first read, caught by
    generate()'s own except clause below."""

    id: str
    name: str
    index: int
    design: dict[str, Any]
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    fabric_templates: list[dict[str, Any]]
    parent: TopologyPodParentData
    loopback_pool: dict[str, Any] | None
    prefix_pool: dict[str, Any] | None


class PodTopologyGenerator(
    PodMeshMixin, ServiceDevicesMixin, PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator
):
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
                refreshed = await self.wait_for_parent_generator_and_refetch(DC_GENERATORS, dc_id)
                if refreshed is not None:
                    deployment_list = clean_data(refreshed).get("TopologyPod", [])
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
            # a moment before its writes (fabric_asn_pool, super-spine devices) are
            # visible to this query — the wait above only catches "still running",
            # not that narrow window (confirmed live: this pod started generating
            # before the DC's super-spine devices existed, with zero underlay
            # peerings as the result). Poll the DC directly, via collect_data() —
            # the same query this generator was invoked with — for whatever it
            # still needs, so the pre-seed BGP/cabling calls below never silently
            # skip for data that's actually just not visible yet.
            needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in (
                RoutingStrategy.EBGP_EBGP.value,
                RoutingStrategy.EBGP_IBGP.value,
            )
            for attempt in range(_DC_READY_MAX_RETRIES):
                asn_pool_ready = not needs_asn_pool or dc.get("fabric_asn_pool")
                super_spines_expected = templates_by_role(dc.get("fabric_templates", []), "super-spine")
                super_spine_ready = not super_spines_expected or dc.get("devices")
                if asn_pool_ready and super_spine_ready:
                    break
                if attempt < _DC_READY_MAX_RETRIES - 1:
                    delay = self._retry_delay(_DC_READY_RETRY_DELAY, attempt)
                    self.logger.info(
                        f"Pod {pod_name}: parent DC's fabric_asn_pool/super-spine devices not visible yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_DC_READY_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    data = await self.collect_data()
                    deployment_list = clean_data(data).get("TopologyPod", [])
                    if not deployment_list:
                        self.logger.error("No Pod Deployment data found in GraphQL response")
                        return
                    self.data = cast(TopologyPodData, deployment_list[0])
                    dc = self.data["parent"]
            # This pod's design (TopologyTemplatePod) and its DC's
            # (TopologyTemplateDC): the envelope every check and pool size reads.
            design = self.data["design"]
            dc_design = dc["design"]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {pod_name}")

        # The parent DC's controllers (queries/topology/add/pod.gql's
        # DataCenterFields fragment), read by create_devices().
        self.set_controllers_from(dc)

        self.deployment_id = dc_id  # Store for cable linking
        self.pod_name = pod_name.lower()
        self.fabric_name = dc_name.lower()

        deployment_type = pod_deployment_type(design)

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
        naming_conv = naming_convention_of(dc, lower=False)

        if spine_count > design["max_spines_per_pod"]:
            self.logger.error(
                f"Pod {pod_name} requests {spine_count} {spine_role}s "
                f"but pod design '{design['name']}' allows at most {design['max_spines_per_pod']}"
            )
            return

        # The DC design's envelope, enforced before any write: the pod's design
        # must be one the DC design may host, and the DC may not hold more pods
        # than max_pods (this pod counted once, so a re-run is not refused).
        allowed_designs = sorted(template["name"] for template in dc_design["pod_templates"])
        if design["name"] not in allowed_designs:
            self.logger.error(
                f"Pod {pod_name}: design '{design['name']}' is not one of DC {dc_name}'s design "
                f"'{dc_design['name']}' pod_templates {allowed_designs}"
            )
            return
        # The DC's pods, from its children in the query payload (its customer footprints come back empty).
        siblings = [child for child in dc.get("children", []) if child]
        pod_count = len({sibling["id"] for sibling in siblings} | {pod_id})
        if pod_count > dc_design["max_pods"]:
            self.logger.error(
                f"Pod {pod_name}: DC {dc_name} holds {pod_count} pods but its design '{dc_design['name']}' "
                f"allows at most {dc_design['max_pods']}"
            )
            return

        indexes: list[int] = [dc["index"], pod_index]

        # Interface names come from the first entry's template only — same accepted
        # limitation as dc.py's super-spine device creation: assumes every spine
        # template shares consistent downlink-interface naming.
        spine_template = spine_entries[0]["template"]
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

        super_spine_devices = [device["name"] for device in dc.get("devices", [])]
        dc_super_spine_entries = templates_by_role(dc.get("fabric_templates", []), "super-spine")
        super_spine_interfaces = (
            [iface["name"] for iface in dc_super_spine_entries[0]["template"].get("interfaces", [])]
            if dc_super_spine_entries
            else []
        )

        # Every precondition is checked before the first save. A run that
        # returned after saving would end with a partial tracked set, and its
        # cleanup would delete everything else it owned last run (cables, BGP,
        # border-leaf/FW/LB wiring). Returning before any save leaves the
        # tracking group untouched.
        if super_spine_devices and not await self._wait_for_super_spine_overlay(super_spine_devices, dc_id, pod_name):
            return
        # No super-spine tier to cable to: the pods mesh back-to-back.
        mesh_cabling = not super_spine_devices or not super_spine_interfaces
        slot_width = 0
        if mesh_cabling:
            try:
                slot_width = mesh_slot_width(
                    design, dc_design, [sibling for sibling in siblings if sibling["index"] < pod_index]
                )
            except ValueError as exc:
                self.logger.error(f"Pod {pod_name}: {exc}")
                return

        # Per-pod pool sizes from this pod's design envelope in its DC's design
        # (generators/fabric_sizing.py), never from its live spine/leaf/tor
        # counts. Host bits are protocol-agnostic — the real prefix length
        # depends on this DC's underlay_protocol (IPv4 /32 vs IPv6 /128).
        underlay_protocol = dc.get("underlay_protocol", "ipv6")

        # Each pool's address family is the one its DC-level parent was sliced
        # from (a dual-stack pod: IPv6 technical, IPv4 loopback).
        pool_sizes = {
            "technical": host_bits_to_prefix_length(
                pod_technical_bits(design, dc_design), ipv6=underlay_pool_is_ipv6(underlay_protocol, "technical")
            ),
            "loopback": host_bits_to_prefix_length(
                pod_loopback_bits(design), ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback")
            ),
        }

        self.logger.info(
            f"Pod {pod_name} pool sizes: technical=/{pool_sizes['technical']}, "
            f"loopback=/{pool_sizes['loopback']} (p2p=/{p2p_prefix_length(underlay_protocol)}, "
            f"underlay={underlay_protocol}, deployment={deployment_type})"
        )

        # Allocate/upsert pools (idempotent via identifier + allow_upsert)
        # Must always run so objects are tracked by the generator framework
        pod_pools = await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=pool_sizes,
        )

        # Reference DC-level ASN pool (one pool per DC, shared by all devices)
        # DC generator creates the pool; pod just references it for routing (racks read it from the DC too)
        dc_asn_pool_id: str | None = None
        dc_fabric_asn_pool = dc.get("fabric_asn_pool")
        if dc_fabric_asn_pool and dc_fabric_asn_pool.get("name"):
            dc_asn_pool_id = dc_fabric_asn_pool["id"]
            self.logger.info(f"Pod {pod_name}: using DC ASN pool '{dc_fabric_asn_pool['name']}'")

        # Pass management pool ID from DC parent (create_devices resolves ID to SDK object)
        dc_management_pool = dc.get("management_pool")
        management_pool_id = dc_management_pool["id"] if dc_management_pool else None

        # border-spine collapses spine+border-leaf into one device for
        # micro-fabrics (see spine_role's own docstring above) — it's a
        # DC-level fabric tier like border-leaf/super-spine/hyper-spine, so
        # it deploys to the DC, not this pod, even though it's created here
        # (pod.py owns spine-slot context). Plain "spine" stays pod-scoped.
        spine_deployment_id = dc_id if spine_role == "border-spine" else pod_id

        # role_index continues across the entries (pod_spine_slot_device_names),
        # so a second entry never re-creates the first one's names.
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
                    role_index_start=len(spines) + 1,
                    allocate_loopback=True,
                    loopback_pool=pod_pools.get("loopback"),
                    loopback_prefix_length=loopback_prefix_length(underlay_protocol),
                    management_pool=management_pool_id,
                ),
            )
            spines.extend(entry_spines)

        # Pre-seed spine eBGP processes before cabling exists so parallel rack generators
        # find them and don't try to create duplicates.
        # OSPF_IBGP is excluded: spine overlay BGP needs overlay_as_id (resolved inside
        # create_routing via _resolve_shared_objects) and is created in the post-cable call below.
        # All spines in this pod share ONE underlay ASN (.dev/bgp.txt) — resolved once,
        # reused by both this pre-seed call and the post-cable call below.
        pod_spine_as_id: str | None = None
        if dc_asn_pool_id and dc.get("routing_strategy", "ebgp-ebgp") in (
            RoutingStrategy.EBGP_EBGP.value,
            RoutingStrategy.EBGP_IBGP.value,
        ):
            pod_spine_as_id = await self._ensure_pod_spine_as(dc_asn_pool_id)
            await self.create_routing(
                bottom_devices=spines,
                top_devices=super_spine_devices,
                options=pod_routing_options(dc, dc_asn_pool_id, pod_spine_as_id),
                p2p_interfaces=[],
                bottom_role=spine_role,
                top_role="super-spine",
            )

        # Skip cabling if no super-spines (single-pod DC scenario)
        if mesh_cabling:
            self.logger.info(
                f"Pod {pod_name}: Skipping spine-to-super-spine cabling (single-pod DC or no super-spines)"
            )

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
                siblings=siblings,
                spines=spines,
                spine_interfaces=spine_interfaces,
                spine_role=spine_role,
                dc=dc,
                dc_asn_pool_id=dc_asn_pool_id,
                pod_pools=pod_pools,
                underlay_protocol=underlay_protocol,
                slot_width=slot_width,
                pod_spine_as_id=pod_spine_as_id,
            )

        if not mesh_cabling:
            # Every pod owns the widest pod design's spine count of super-spine ports.
            cabling_offset = (pod_index - 1) * max_sibling_spines(dc_design)
            p2p_pairs = await self.create_cabling(
                bottom_devices=spines,
                bottom_interfaces=spine_interfaces,
                top_devices=super_spine_devices,
                top_interfaces=super_spine_interfaces,
                strategy="pod",
                options=CablingOptions(
                    cabling_offset=cabling_offset,
                    pool=pod_pools.get("technical"),
                    p2p_prefix_length=p2p_prefix_length(underlay_protocol),
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
                options=pod_routing_options(dc, dc_asn_pool_id, pod_spine_as_id),
                p2p_interfaces=p2p_pairs,
                bottom_role=spine_role,
                top_role="super-spine",
            )
        # When there are no super-spines to cable to (mesh_cabling), inter-pod
        # back-to-back spine cabling is handled above by _cable_to_existing_sibling_pods.
        # Fan-out to add_rack is handled by the sibling pod_rack_cascade generator,
        # not here — see PodTopologyGenerator's class docstring.

        # Cable+route this pod's own border-leaf devices (created by dc.py's
        # _create_border_leaf_devices (deployed to the DC, see dc.py), but NOT cabled
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
            underlay_protocol=underlay_protocol,
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
        # dc_pod_cascade run (see the manual dc_pod_cascade calls after bulk
        # loads). Deliberately NOT auto-triggered from here: every
        # add_pod run (including each pod during a bulk multi-DC load) would fire
        # its own concurrent dc_pod_cascade re-run against the same DC-level pools/
        # ASN pool, racing the DC's own already-in-flight bootstrap.

    async def _wait_for_super_spine_overlay(self, super_spine_devices: list[str], dc_id: str, pod_name: str) -> bool:
        """Whether the parent DC's super-spines have their overlay BGP — waited for, never skipped.

        The spines' overlay sessions peer with those processes, so the pod
        cannot bootstrap before them. They come from add_dc, which this run
        normally waits out up front; under load its task can surface only
        after that check, while it is still writing. Nothing re-runs a pod
        that returned here (a created DC fires plain add_dc, not
        dc_pod_cascade), so returning would leave it with no pools for good
        and its racks failing: wait for the add_dc once it is visible, retry
        with backoff otherwise, and fail the run if the processes never land.
        """
        for attempt in range(_DC_READY_MAX_RETRIES):
            if await self.bgp_processes_ready(super_spine_devices, "overlay"):
                return True
            if await self.wait_for_parent_generator_and_refetch(DC_GENERATORS, dc_id) is not None:
                continue
            if attempt < _DC_READY_MAX_RETRIES - 1:
                delay = self._retry_delay(_DC_READY_RETRY_DELAY, attempt)
                self.logger.info(
                    f"Pod {pod_name}: parent DC super-spine overlay BGP not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_DC_READY_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
        self.logger.error(
            f"Pod {pod_name}: parent DC super-spine overlay BGP never appeared after {_DC_READY_MAX_RETRIES} "
            "attempts — run add_dc for its DC first"
        )
        return False

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
        naming_conv = naming_convention_of(dc)

        firewall_names, load_balancer_names = await self.create_firewall_and_load_balancer_devices(
            fabric_templates=fabric_templates,
            deployment_id=self.data["id"],
            naming_convention=naming_conv,
            indexes=[dc["index"], self.data["index"]],
        )
        await self._cable_border_services(
            border_role_for=BORDER_ROLE_FOR_SERVICES,
            connectivity_mode=cast(Literal["pbr", "inline"], dc.get("connectivity_mode", "pbr")),
            border_names=spines,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )

    async def _ensure_pod_spine_as(self, asn_pool_id: str | None) -> str | None:
        """Find-or-create the ONE underlay AS shared by every spine in this
        pod (.dev/bgp.txt: all spines within a pod share the same ASN,
        different pods get different ASNs). Returns None (and logs a
        warning) if no ASN pool is available yet."""
        try:
            as_id = await self.ensure_shared_as(
                description=f"{self.pod_name} spine underlay ASN", asn_pool_id=asn_pool_id
            )
        except Exception as exc:
            self.logger.error(f"Failed to ensure shared spine AS for pod {self.pod_name}: {exc}")
            return None
        if as_id is None:
            self.logger.warning(f"Pod {self.pod_name}: no ASN pool available — cannot allocate shared spine AS")
        return as_id

    async def _cable_border_leafs_to_spines(
        self,
        *,
        spines: list[str],
        spine_downlink_interfaces: list[str],
        dc: Any,
        dc_asn_pool_id: str | None,
        pod_pools: dict[str, Any],
        underlay_protocol: str,
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
        is deterministic (the design's spine_downlink_layout, not live device
        count) so it never collides with rack.py's own leaf/tor offsets into
        the same spine downlink ports. No-ops if this pod has no border-leaf
        devices (the common case — most pods don't host any)."""
        max_border_leafs_per_fabric = dc["design"]["max_border_leafs_per_fabric"]
        if max_border_leafs_per_fabric <= 0:
            return
        naming = DeviceNamingConfig(strategy=naming_convention_of(dc, lower=False))
        candidate_names = expected_device_names(
            naming_config=naming,
            fabric_name=self.fabric_name,
            device_indexes=[dc["index"], self.data["index"]],
            role="border-leaf",
            quantity=max_border_leafs_per_fabric,
        )
        # Bounded retry: candidate_names is a live query (not the DC snapshot
        # _DC_READY_* above already waited on), so a pod that *does* host
        # border-leaf can still read it before add_dc's write is visible here
        # — confirmed live: a border-leaf ended up with 0 underlay peerings
        # because this lookup found nothing and silently no-opped instead of
        # retrying, unlike every other "might not exist yet" lookup in this
        # file.
        border_leafs: list[Any] = []
        for attempt in range(_BORDER_LEAF_MAX_RETRIES):
            border_leafs = await self.client.filters(
                kind=DcimPhysicalDevice, name__values=sorted(candidate_names), role__value="border-leaf"
            )
            if border_leafs or attempt == _BORDER_LEAF_MAX_RETRIES - 1:
                break
            await asyncio.sleep(self._retry_delay(_BORDER_LEAF_RETRY_DELAY, attempt))
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

        offset = spine_downlink_layout(self.data["design"])["border-leaf"]

        p2p_pairs = await self.create_cabling(
            bottom_devices=border_leaf_names,
            bottom_interfaces=bl_uplink_names,
            top_devices=spines,
            top_interfaces=spine_downlink_interfaces,
            strategy="rack",
            options=CablingOptions(
                cabling_offset=offset,
                pool=pod_pools.get("technical"),
                p2p_prefix_length=p2p_prefix_length(underlay_protocol),
            ),
        )
        # Gating the call itself on dc_asn_pool_id would silently skip routing
        # entirely for ospf-ibgp fabrics (no per-device ASN pool) —
        # create_routing()'s own routing_strategy check is the real gate.
        await self.create_routing(
            bottom_devices=border_leaf_names,
            top_devices=spines,
            options=pod_routing_options(dc, dc_asn_pool_id),
            p2p_interfaces=p2p_pairs,
            bottom_role="border-leaf",
            top_role="spine",
        )
