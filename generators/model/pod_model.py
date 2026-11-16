"""The pod fabric model: envelope checks, spine-slot shape, pool prefix lengths and cabling offsets (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.dcim.template_interfaces import template_interface_names_by_role
from generators.helpers.naming import NamingConvention, naming_convention_of
from generators.helpers.routing import underlay_pool_is_ipv6
from generators.model.errors import FabricModelError
from generators.model.pod_profile import (
    pod_deployment_type,
    spine_slot_role,
    spine_slot_templates,
)
from generators.model.sizing import (
    host_bits_to_prefix_length,
    max_sibling_spines,
    pod_loopback_bits,
    pod_technical_bits,
)


@dataclass(frozen=True)
class PodFabricModel:
    """What a TopologyPod declares inside its DC's envelope, compiled."""

    pod_id: str
    pod_name: str
    pod_index: int
    dc_id: str
    dc_name: str
    dc_index: int
    deployment_type: str
    naming_convention: NamingConvention
    underlay_protocol: str
    spine_role: str
    # The spine-slot entries (fabric_templates with positive quantity), whose templates create the devices
    spine_entries: tuple[dict[str, Any], ...]
    spine_uplink_interfaces: tuple[str, ...]
    spine_downlink_interfaces: tuple[str, ...]
    # the DC's pods, the pod itself included when it is among its children
    siblings: tuple[dict[str, Any], ...]
    # pool slot -> prefix length
    pools_to_allocate: dict[str, int]
    # first super-spine port of this pod: every pod owns the widest pod design's spine count of ports
    super_spine_offset: int

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> PodFabricModel:
        """Compile one clean_data() TopologyPod entry. A missing required key raises KeyError; a pod outside its design
        envelope raises FabricModelError.
        """
        pod_id: str = data["id"]
        pod_name: str = data["name"]
        design: Mapping[str, Any] = data["design"]
        dc: Mapping[str, Any] = data["parent"]
        dc_name: str = dc["name"]
        dc_design: Mapping[str, Any] = dc["design"]

        # border-spine collapses spine+border-leaf (micro-fabric)
        fabric_templates = data.get("fabric_templates", [])
        spine_entries = spine_slot_templates(fabric_templates)
        spine_role = spine_slot_role(fabric_templates)
        if not spine_entries:
            raise FabricModelError(
                f"Pod {pod_name}: no spine/border-spine fabric_templates entries — cannot build fabric"
            )
        spine_count = sum(entry["quantity"] for entry in spine_entries)
        if spine_count > design["max_spines_per_pod"]:
            raise FabricModelError(
                f"Pod {pod_name} requests {spine_count} {spine_role}s "
                f"but pod design '{design['name']}' allows at most {design['max_spines_per_pod']}"
            )

        # Enforce the DC design's envelope (this pod counted once, so a re-run passes).
        allowed_designs = sorted(template["name"] for template in dc_design["pod_templates"])
        if design["name"] not in allowed_designs:
            raise FabricModelError(
                f"Pod {pod_name}: design '{design['name']}' is not one of DC {dc_name}'s design "
                f"'{dc_design['name']}' pod_templates {allowed_designs}"
            )
        # The DC's pods, from its children in the query payload (its customer footprints come back empty).
        siblings = [child for child in dc.get("children", []) if child]
        pod_count = len({sibling["id"] for sibling in siblings} | {pod_id})
        if pod_count > dc_design["max_pods"]:
            raise FabricModelError(
                f"Pod {pod_name}: DC {dc_name} holds {pod_count} pods but its design '{dc_design['name']}' "
                f"allows at most {dc_design['max_pods']}"
            )

        # Interface names come from the first template only; assumes consistent naming across entries.
        spine_interfaces = spine_entries[0]["template"].get("interfaces", [])
        uplinks = template_interface_names_by_role(interfaces=spine_interfaces, role="uplink")
        if not uplinks:
            raise FabricModelError(
                f"Pod {pod_name}: No uplink interfaces found in spine template. "
                "Cannot create spine-to-super-spine cabling."
            )

        # Each pool's family follows its DC-level parent (dual-stack: IPv6 technical, IPv4 loopback).
        underlay_protocol: str = dc.get("underlay_protocol", "ipv6")
        pools = {
            "technical": host_bits_to_prefix_length(
                pod_technical_bits(design, dc_design), ipv6=underlay_pool_is_ipv6(underlay_protocol, "technical")
            ),
            "loopback": host_bits_to_prefix_length(
                pod_loopback_bits(design), ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback")
            ),
        }
        return cls(
            pod_id=pod_id,
            pod_name=pod_name,
            pod_index=data["index"],
            dc_id=dc["id"],
            dc_name=dc_name,
            dc_index=dc["index"],
            deployment_type=pod_deployment_type(design),
            naming_convention=naming_convention_of(dc, lower=False),
            underlay_protocol=underlay_protocol,
            spine_role=spine_role,
            spine_entries=tuple(spine_entries),
            spine_uplink_interfaces=tuple(uplinks),
            spine_downlink_interfaces=tuple(
                template_interface_names_by_role(interfaces=spine_interfaces, role="downlink")
            ),
            siblings=tuple(siblings),
            pools_to_allocate=pools,
            super_spine_offset=(data["index"] - 1) * max_sibling_spines(dc_design),
        )
