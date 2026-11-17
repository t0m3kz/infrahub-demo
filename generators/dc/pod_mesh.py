"""Back-to-back mesh of the pod generator: a pod without a super-spine tier cables itself to lower-index siblings.

What the mesh needs of its siblings (their spines and uplink ports) is another generator's output (add_pod): it is read
before the plan, waiting with the shared retry policy (an explicit wait: the names of a sibling's spines are not in the
query, so they cannot be referenced by HFID).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from generators.dcim.cabling_plan import PortFacts
from generators.model.pod_profile import spine_slot_device_names
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface
from utils.limits import WAIT_FOR_PEER_GENERATOR


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


@dataclass(frozen=True)
class SiblingSpines:
    """The spines of a lower-index sibling pod and their uplink ports, as they exist."""

    index: int
    spines: tuple[str, ...]
    uplinks: tuple[PortFacts, ...]


def mesh_offsets(pod_index: int, sibling_index: int, slot_width: int) -> tuple[int, int]:
    """``(slot_start, cabling_offset)`` of the pair: the deterministic uplink slot of the sibling (sibling.index - 1) of
    ``slot_width`` ports on this pod's side, and the peer-facing offset that starts past the sibling's own outbound slots
    (first sibling.index - 1) or pair_slot 0 collides.
    """
    sibling_own_outbound_slots = sibling_index - 1
    pair_slot = sibling_own_outbound_slots + max(0, pod_index - sibling_index - 1)
    return (sibling_index - 1) * slot_width, pair_slot * slot_width


async def read_sibling_spines(
    generator: Any,
    *,
    siblings: Sequence[Mapping[str, Any]],
    dc: Mapping[str, Any],
    fabric_name: str,
    spine_role: str,
    slot_width: int,
    pod_name: str,
) -> list[SiblingSpines]:
    """The spines and uplink ports of each lower-index sibling pod, by index. Waits for a sibling whose add_pod has not
    created them yet, and fails when it never does: only the higher-index pod of a pair cables it, so skipping would
    leave it uncabled (a re-run retries).
    """
    found: list[SiblingSpines] = []
    for sibling in sorted(siblings, key=lambda s: s["index"]):
        # By name, not deployment: a border-spine is deployed to the DC; a sibling holds at most slot_width spines.
        candidate_names = spine_slot_device_names(
            dc=dc, fabric_name=fabric_name, pod_index=sibling["index"], role=spine_role, quantity=slot_width
        )
        spines: list[str] = []
        uplinks: list[Any] = []
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            devices = await generator.client.filters(
                kind=DcimPhysicalDevice, name__values=candidate_names, role__value=spine_role
            )
            spines = sorted(device.name.value for device in devices)
            if spines:
                uplinks = await generator.client.filters(
                    kind=DcimPhysicalInterface, device__name__values=spines, role__value="uplink", include=["device"]
                )
                if uplinks:
                    break
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                delay = generator._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                generator.logger.info(
                    f"Pod {pod_name}: sibling pod idx={sibling['index']} spines/uplinks not ready yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)
        if not spines or not uplinks:
            generator.fail(
                f"Pod {pod_name}: sibling pod idx={sibling['index']} still has no spine "
                f"devices/uplinks after {WAIT_FOR_PEER_GENERATOR.attempts} attempts — cannot mesh-cable this pair"
            )
        found.append(
            SiblingSpines(
                index=sibling["index"],
                spines=tuple(spines),
                uplinks=tuple(
                    PortFacts(
                        port.device.peer.name.value,
                        port.name.value,
                        getattr(getattr(port, "interface_type", None), "value", None),
                    )
                    for port in sorted(uplinks, key=lambda p: p.name.value)
                ),
            )
        )
    return found
