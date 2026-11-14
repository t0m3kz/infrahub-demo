"""Infrastructure generator for pod topology creation."""

import asyncio
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from generators.fabric.fabric_shared import DC_GENERATORS, ServiceDevicesMixin
from generators.fabric.pod_mesh import PodMeshMixin, mesh_slot_width, pod_routing_options
from generators.helpers.naming import DeviceNamingConfig
from generators.helpers.rack import expected_device_names
from generators.helpers.routing import RoutingStrategy, loopback_prefix_length, p2p_prefix_length, underlay_pool_is_ipv6
from generators.helpers.template_interfaces import template_interface_names_by_role
from generators.mixins.common import CommonGenerator
from generators.mixins.connections import CablingMixin
from generators.mixins.devices import DeviceMixin
from generators.mixins.fabric_sizing import (
    host_bits_to_prefix_length,
    max_sibling_spines,
    pod_loopback_bits,
    pod_technical_bits,
)
from generators.mixins.pod_config import (
    pod_deployment_type,
    spine_downlink_layout,
    spine_slot_role,
    spine_slot_templates,
    templates_by_role,
)
from generators.mixins.pools import PoolMixin
from generators.mixins.routing import RoutingMixin
from generators.mixins.types import CablingOptions, DeviceOptions, RoutingOptions, naming_convention_of
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface
from utils.data_cleaning import clean_data
from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES
from utils.limits import BORDER_LEAF_WAIT, WAIT_FOR_PEER_GENERATOR

# FW/LB "uplink" interfaces face border-spine's "firewall"/"load-balancer" ports (BORDER_ROLE_FOR_SERVICES).


class TopologyPodParentData(TypedDict, total=False):
    """Nested TopologyDataCenter projection under TopologyPod.parent."""

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
    """One clean_data()-processed TopologyPod entry; a missing key raises KeyError at first read."""

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
    """Generate pod topology: resource pools and spine devices; never fans out to add_rack. Waits for an in-flight
    add_dc/dc_pod_cascade.
    """

    data: TopologyPodData

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure."""

        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod Deployment data found in GraphQL response")
                return

            # Wait for add_dc/dc_pod_cascade on the parent DC, then re-parse, rather than run on partial data.
            dc_id = deployment_list[0].get("parent", {}).get("id")
            if dc_id:
                refreshed = await self.wait_for_parent_generator_and_refetch(DC_GENERATORS, dc_id)
                if refreshed is not None:
                    deployment_list = clean_data(refreshed).get("TopologyPod", [])
                    if not deployment_list:
                        self.logger.error("No Pod Deployment data found in GraphQL response")
                        return

            self.data = cast(TopologyPodData, deployment_list[0])
            # Force-read every required field here so a malformed response raises KeyError inside the try.
            pod_id = self.data["id"]
            pod_name = self.data["name"]
            pod_index = self.data["index"]
            dc = self.data["parent"]
            dc_id = dc["id"]
            dc_name = dc["name"]

            # add_dc's task can finish before its writes are visible: poll the DC for what is still needed.
            needs_asn_pool = dc.get("routing_strategy", "ebgp-ebgp") in (
                RoutingStrategy.EBGP_EBGP.value,
                RoutingStrategy.EBGP_IBGP.value,
            )
            for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
                asn_pool_ready = not needs_asn_pool or dc.get("fabric_asn_pool")
                super_spines_expected = templates_by_role(dc.get("fabric_templates", []), "super-spine")
                super_spine_ready = not super_spines_expected or dc.get("devices")
                if asn_pool_ready and super_spine_ready:
                    break
                if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                    delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                    self.logger.info(
                        f"Pod {pod_name}: parent DC's fabric_asn_pool/super-spine devices not visible yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                    )
                    await asyncio.sleep(delay)
                    data = await self.collect_data()
                    deployment_list = clean_data(data).get("TopologyPod", [])
                    if not deployment_list:
                        self.logger.error("No Pod Deployment data found in GraphQL response")
                        return
                    self.data = cast(TopologyPodData, deployment_list[0])
                    dc = self.data["parent"]
            # This pod's and its DC's design: the envelope every check and pool size reads.
            design = self.data["design"]
            dc_design = dc["design"]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {pod_name}")

        # Parent DC controllers, read by create_devices().
        self.set_controllers_from(dc)

        self.deployment_id = dc_id
        self.pod_name = pod_name.lower()
        self.fabric_name = dc_name.lower()

        deployment_type = pod_deployment_type(design)

        # border-spine collapses spine+border-leaf (micro-fabric); only it gets its own per-pod FW/LB.
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

        # Enforce the DC design's envelope before any write (this pod counted once, so a re-run passes).
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

        # Interface names come from the first template only; assumes consistent downlink naming.
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

        # Check every precondition before the first save: returning after a save leaves a partial tracked set.
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

        # Pool sizes come from the pod's design envelope (fabric_sizing.py), not live counts.
        underlay_protocol = dc.get("underlay_protocol", "ipv6")

        # Each pool's family follows its DC-level parent (dual-stack: IPv6 technical, IPv4 loopback).
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

        # Upsert pools (idempotent); must always run so the framework tracks the objects.
        pod_pools = await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=pool_sizes,
        )

        # Reference the DC-level ASN pool (one per DC, created by the DC generator).
        dc_asn_pool_id: str | None = None
        dc_fabric_asn_pool = dc.get("fabric_asn_pool")
        if dc_fabric_asn_pool and dc_fabric_asn_pool.get("name"):
            dc_asn_pool_id = dc_fabric_asn_pool["id"]
            self.logger.info(f"Pod {pod_name}: using DC ASN pool '{dc_fabric_asn_pool['name']}'")

        # Management pool ID from the DC parent; create_devices resolves it.
        dc_management_pool = dc.get("management_pool")
        management_pool_id = dc_management_pool["id"] if dc_management_pool else None

        # border-spine is a DC-level fabric tier: it deploys to the DC even though created here.
        spine_deployment_id = dc_id if spine_role == "border-spine" else pod_id

        # role_index continues across entries so a second entry never re-creates names.
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

        # Pre-seed spine eBGP (one shared underlay ASN) so parallel rack generators find it; OSPF_IBGP overlay BGP
        # follows post-cable.
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

            # No super-spine tier: pre-seed spine overlay BGP (skip_underlay) so rack routing calls find it.
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
        # Back-to-back cabling is in _cable_to_existing_sibling_pods; add_rack fan-out is in pod_rack_cascade.

        # Cable+route this pod's border-leafs (created by dc.py, deployed to the DC) to its spines.
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

        # Per-pod FW/LB, only with spine_role border-spine.
        if spine_role == "border-spine":
            await self._generate_pod_scoped_border_services(spines=spines)

        # A pod added after its DC declared border-leaf/FW/LB templates needs an explicit dc_pod_cascade; not
        # auto-triggered here, it would race the DC bootstrap.

    async def _wait_for_super_spine_overlay(self, super_spine_devices: list[str], dc_id: str, pod_name: str) -> bool:
        """Whether the parent DC's super-spines have their overlay BGP: retried with backoff, fails if they never land
        (nothing re-runs an early return).
        """
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            if await self.bgp_processes_ready(super_spine_devices, "overlay"):
                return True
            if await self.wait_for_parent_generator_and_refetch(DC_GENERATORS, dc_id) is not None:
                continue
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"Pod {pod_name}: parent DC super-spine overlay BGP not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)
        self.logger.error(
            f"Pod {pod_name}: parent DC super-spine overlay BGP never appeared after {WAIT_FOR_PEER_GENERATOR.attempts} "
            "attempts — run add_dc for its DC first"
        )
        return False

    async def _generate_pod_scoped_border_services(self, *, spines: list[str]) -> None:
        """Create this pod's own FW/LB and cable them to its border-spines; no-op if it declares none."""
        fabric_templates = self.data.get("fabric_templates", [])
        firewall_entries = templates_by_role(fabric_templates, "internal-firewall")
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
        """Find-or-create the one underlay AS shared by the pod's spines; None (warning) if no ASN pool yet."""
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
        """Cable and route the pod's border-leafs (created by dc.py) to its spines, matched by deterministic name; no-op
        if the pod hosts none.
        """
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
        # Bounded retry: border-leaf may be read before add_dc's write is visible; short because most pods host none.
        border_leafs: list[Any] = []
        for attempt in range(BORDER_LEAF_WAIT.attempts):
            border_leafs = await self.client.filters(
                kind=DcimPhysicalDevice, name__values=sorted(candidate_names), role__value="border-leaf"
            )
            if border_leafs or attempt == BORDER_LEAF_WAIT.attempts - 1:
                break
            await asyncio.sleep(self._retry_delay(BORDER_LEAF_WAIT.delay, attempt))
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
        # create_routing()'s own routing_strategy check is the gate; gating here would skip ospf-ibgp.
        await self.create_routing(
            bottom_devices=border_leaf_names,
            top_devices=spines,
            options=pod_routing_options(dc, dc_asn_pool_id),
            p2p_interfaces=p2p_pairs,
            bottom_role="border-leaf",
            top_role="spine",
        )
