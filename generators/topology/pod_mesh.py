"""Back-to-back mesh of the pod generator: a pod without a super-spine tier cables itself to lower-index siblings."""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Callable

from utils.limits import WAIT_FOR_PEER_GENERATOR

from ..helpers.routing import p2p_prefix_length
from ..pod_config import spine_slot_device_names
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface
from ..types import CablingOptions, RoutingOptions

if TYPE_CHECKING:
    from ..common import CommonGenerator


def pod_routing_options(dc: Any, asn_pool_id: str | None, shared_underlay_as_id: str | None = None) -> RoutingOptions:
    """create_routing() options for a spine/border-leaf tier of this pod's DC."""
    options = RoutingOptions(design=dc, asn_pool=asn_pool_id)
    if shared_underlay_as_id:
        options["shared_underlay_as_id"] = shared_underlay_as_id
    return options


def mesh_slot_width(
    design: Mapping[str, Any], dc_design: Mapping[str, Any], lower_siblings: Sequence[Mapping[str, Any]]
) -> int:
    """Uplink ports each back-to-back pod pair reserves per side (max_spines_per_pod); a sibling with another width is
    refused.
    """
    width = design["max_spines_per_pod"]
    widths = {template.get("id"): template["max_spines_per_pod"] for template in dc_design["pod_templates"]}
    mismatched = sorted(sibling["index"] for sibling in lower_siblings if widths.get(sibling["design"]["id"]) != width)
    if mismatched:
        raise ValueError(
            f"back-to-back mesh needs one spine width for every pod: design '{design['name']}' allows {width} "
            f"spines, lower-index sibling pod(s) {mismatched} have a design with another max_spines_per_pod"
        )
    return width


class PodMeshMixin:
    """Mixin cabling a pod's spines to its lower-index sibling pods (back-to-back mesh); the host provides the
    attributes it uses.
    """

    client: Any
    logger: logging.Logger
    data: Any
    fabric_name: str
    create_cabling: Callable[..., Any]
    create_routing: Callable[..., Any]
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        _retry_delay = staticmethod(CommonGenerator._retry_delay)

    async def _cable_to_existing_sibling_pods(
        self,
        siblings: Sequence[Mapping[str, Any]],
        spines: list[str],
        spine_interfaces: list[str],
        spine_role: str,
        dc: Any,
        dc_asn_pool_id: str | None,
        pod_pools: dict[str, Any],
        underlay_protocol: str,
        slot_width: int,
        pod_spine_as_id: str | None = None,
    ) -> None:
        """Cable this pod's spines to every existing lower-index sibling pod; only the higher-index pod of a pair writes
        it.
        """
        pod_name = self.data["name"]
        pod_index = self.data["index"]
        lower_siblings = [s for s in siblings if s["index"] < pod_index]
        if not lower_siblings:
            self.logger.info(f"Pod {pod_name}: no lower-index sibling pods yet — nothing to mesh-cable")
            return
        if not spine_interfaces:
            self.fail(
                f"Pod {pod_name}: no {spine_role} uplink interfaces to mesh-cable to "
                f"{len(lower_siblings)} lower-index sibling pod(s) — add uplink-role ports to the spine template"
            )

        routing_opts = pod_routing_options(dc, dc_asn_pool_id, pod_spine_as_id)
        for sibling in sorted(lower_siblings, key=lambda s: s["index"]):
            sibling_index = sibling["index"]
            # Retry: a lower-index sibling's add_pod may not have created its spines yet.
            sibling_spines: list[str] = []
            sibling_uplink_names: list[str] = []
            # By name, not deployment: a border-spine is deployed to the DC; a sibling holds at most slot_width spines.
            candidate_names = spine_slot_device_names(
                dc=dc,
                fabric_name=self.fabric_name,
                pod_index=sibling["index"],
                role=spine_role,
                quantity=slot_width,
            )
            for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
                sibling_spines_devices = await self.client.filters(
                    kind=DcimPhysicalDevice,
                    name__values=candidate_names,
                    role__value=spine_role,
                )
                sibling_spines = sorted(s.name.value for s in sibling_spines_devices)
                if sibling_spines:
                    sibling_uplinks = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__name__values=sibling_spines,
                        role__value="uplink",
                    )
                    sibling_uplink_names = sorted({iface.name.value for iface in sibling_uplinks})
                    if sibling_uplink_names:
                        break
                if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                    delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                    self.logger.info(
                        f"Pod {pod_name}: sibling pod idx={sibling['index']} spines/uplinks not ready yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                    )
                    await asyncio.sleep(delay)

            if not sibling_spines or not sibling_uplink_names:
                # Only the higher-index pod cables a pair, so skipping would leave it uncabled: fail, a re-run retries.
                self.fail(
                    f"Pod {pod_name}: sibling pod idx={sibling['index']} still has no spine "
                    f"devices/uplinks after {WAIT_FOR_PEER_GENERATOR.attempts} attempts — cannot mesh-cable this pair"
                )

            # Deterministic uplink slot (sibling.index - 1), slot_width ports wide, one port per sibling spine.
            links_per_sibling = len(sibling_spines)
            slot_start = (sibling_index - 1) * slot_width
            sibling_local_uplinks = spine_interfaces[slot_start : slot_start + links_per_sibling]
            if len(sibling_local_uplinks) < links_per_sibling:
                self.logger.error(
                    f"Pod {pod_name}: insufficient local {spine_role} uplinks for inter-pod mesh. "
                    f"Needed {links_per_sibling} free uplink name(s) for sibling pod idx={sibling['index']}, "
                    f"but only {len(sibling_local_uplinks)} remain. "
                    "Increase uplink-role interfaces in the spine template or reduce mesh fan-out."
                )
                return

            # Peer-facing offset must start past the sibling's own outbound slots (first sibling.index - 1), or
            # pair_slot 0 collides.
            sibling_own_outbound_slots = sibling_index - 1
            pair_slot = sibling_own_outbound_slots + max(0, pod_index - sibling_index - 1)
            cabling_offset = pair_slot * slot_width
            self.logger.info(
                f"Pod {pod_name} (idx={pod_index}): cabling to sibling pod idx={sibling['index']} "
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
                    p2p_prefix_length=p2p_prefix_length(underlay_protocol),
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
