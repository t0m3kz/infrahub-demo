"""Pure Pod fabric model compiler: validates pod envelope and computes pool sizes & cabling mode without I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.helpers.routing import underlay_pool_is_ipv6
from generators.helpers.template_interfaces import template_interface_names_by_role
from generators.mixins.fabric_sizing import (
    host_bits_to_prefix_length,
    pod_loopback_bits,
    pod_technical_bits,
)
from generators.mixins.pod_config import (
    pod_deployment_type,
    spine_slot_role,
    spine_slot_templates,
    templates_by_role,
)
from generators.mixins.types import NamingConvention, naming_convention_of


@dataclass(frozen=True)
class PodFabricModel:
    """Compiled, immutable blueprint of a Pod's envelope, pool prefix lengths, and uplink mode."""

    pod_id: str
    pod_name: str
    pod_slug: str
    pod_index: int
    dc_id: str
    dc_name: str
    fabric_name: str
    dc_index: int
    deployment_type: str
    naming_convention: NamingConvention
    underlay_protocol: str
    routing_strategy: str
    spine_role: str
    spine_count: int
    spine_uplink_interfaces: tuple[str, ...]
    super_spine_devices: tuple[str, ...]
    super_spine_interfaces: tuple[str, ...]
    mesh_cabling: bool
    pools_to_allocate: dict[str, int]

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> PodFabricModel:
        """Compile a clean_data() TopologyPod dictionary into a validated PodFabricModel."""
        pod_id: str = data["id"]
        pod_name: str = data["name"]
        pod_index: int = data["index"]
        design: Mapping[str, Any] = data["design"]
        dc: Mapping[str, Any] = data["parent"]
        dc_id: str = dc["id"]
        dc_name: str = dc["name"]
        dc_index: int = dc["index"]
        dc_design: Mapping[str, Any] = dc["design"]

        deployment_type = pod_deployment_type(design)
        fabric_templates: list[dict[str, Any]] = list(data.get("fabric_templates") or [])
        spine_entries = spine_slot_templates(fabric_templates)
        spine_role = spine_slot_role(fabric_templates)
        if not spine_entries:
            raise ValueError(f"Pod {pod_name}: no spine/border-spine fabric_templates entries — cannot build fabric")

        spine_count = sum(int(entry["quantity"]) for entry in spine_entries)
        max_spines = design["max_spines_per_pod"]
        if spine_count > max_spines:
            raise ValueError(
                f"Pod {pod_name} requests {spine_count} {spine_role}s "
                f"but pod design {design['name']!r} allows at most {max_spines}"
            )

        allowed_designs = sorted(template["name"] for template in dc_design["pod_templates"])
        if design["name"] not in allowed_designs:
            raise ValueError(
                f"Pod {pod_name}: design {design['name']!r} is not one of DC {dc_name}'s design "
                f"{dc_design['name']!r} pod_templates {allowed_designs}"
            )

        siblings = [child for child in dc.get("children", []) if child]
        pod_count = len({sibling["id"] for sibling in siblings} | {pod_id})
        if pod_count > dc_design["max_pods"]:
            raise ValueError(
                f"Pod {pod_name}: DC {dc_name} holds {pod_count} pods but its design {dc_design['name']!r} "
                f"allows at most {dc_design['max_pods']}"
            )

        spine_template = spine_entries[0]["template"]
        spine_interfaces = tuple(
            template_interface_names_by_role(interfaces=spine_template.get("interfaces", []), role="uplink")
        )
        if not spine_interfaces:
            raise ValueError(
                f"Pod {pod_name}: No uplink interfaces found in spine template. "
                "Cannot create spine-to-super-spine cabling."
            )

        super_spine_devices = tuple(device["name"] for device in dc.get("devices", []))
        dc_super_spine_entries = templates_by_role(dc.get("fabric_templates", []), "super-spine")
        super_spine_interfaces = tuple(
            [iface["name"] for iface in dc_super_spine_entries[0]["template"].get("interfaces", [])]
            if dc_super_spine_entries
            else []
        )
        mesh_cabling = not super_spine_devices or not super_spine_interfaces

        underlay_protocol: str = dc.get("underlay_protocol", "ipv6")
        routing_strategy: str = dc.get("routing_strategy", "ebgp-ebgp")
        pools_to_allocate: dict[str, int] = {
            "technical": host_bits_to_prefix_length(
                pod_technical_bits(design, dc_design),
                ipv6=underlay_pool_is_ipv6(underlay_protocol, "technical"),
            ),
            "loopback": host_bits_to_prefix_length(
                pod_loopback_bits(design),
                ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback"),
            ),
        }

        return cls(
            pod_id=pod_id,
            pod_name=pod_name,
            pod_slug=pod_name.lower(),
            pod_index=pod_index,
            dc_id=dc_id,
            dc_name=dc_name,
            fabric_name=dc_name.lower(),
            dc_index=dc_index,
            deployment_type=deployment_type,
            naming_convention=naming_convention_of(dc, lower=False),
            underlay_protocol=underlay_protocol,
            routing_strategy=routing_strategy,
            spine_role=spine_role,
            spine_count=spine_count,
            spine_uplink_interfaces=spine_interfaces,
            super_spine_devices=super_spine_devices,
            super_spine_interfaces=super_spine_interfaces,
            mesh_cabling=mesh_cabling,
            pools_to_allocate=pools_to_allocate,
        )
