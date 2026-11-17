"""Shared builders for the DC generator tests: a DC deployment payload."""

from __future__ import annotations

from typing import Any

from tests.unit.simulators.fabric_designs import custom_dc_design, dc_design


def _dc_design(
    *,
    max_super_spines_per_fabric: int = 2,
    max_hyper_spines_per_fabric: int = 0,
    max_border_leafs_per_fabric: int = 4,
    max_pods: int = 2,
) -> dict[str, Any]:
    """A throwaway DC design (custom_dc_design: hosting one 2-spine."""
    return custom_dc_design(
        max_super_spines_per_fabric=max_super_spines_per_fabric,
        max_hyper_spines_per_fabric=max_hyper_spines_per_fabric,
        max_border_leafs_per_fabric=max_border_leafs_per_fabric,
        max_pods=max_pods,
    )


def _fabric_templates(
    *,
    amount_of_super_spines: int = 0,
    super_spine_template: dict[str, Any] | None = None,
    amount_of_hyper_spines: int = 0,
    hyper_spine_template: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build a fabric_templates list with super-spine/hyper-spine entries."""
    entries: list[dict[str, Any]] = []
    if amount_of_super_spines > 0 and super_spine_template is not None:
        entries.append(
            {
                "role": "super-spine",
                "quantity": amount_of_super_spines,
                "template": super_spine_template,
            }
        )
    if amount_of_hyper_spines > 0 and hyper_spine_template is not None:
        entries.append(
            {
                "role": "hyper-spine",
                "quantity": amount_of_hyper_spines,
                "template": hyper_spine_template,
            }
        )
    return entries


def _deployment(
    *,
    design: dict[str, Any] | None = None,
    routing_strategy: str = "ebgp-ebgp",
    underlay_protocol: str = "ipv6",
    amount_of_super_spines: int = 0,
    super_spine_template: dict[str, Any] | None = None,
    amount_of_hyper_spines: int = 0,
    hyper_spine_template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`design` is the DC's fabric design dict (see _dc_design()); the S design by default."""
    return {
        "TopologyDeployment": [
            {
                "id": "dc-1",
                "name": "DC1",
                "index": 1,
                "design": design if design is not None else dc_design("S"),
                "routing_strategy": routing_strategy,
                "underlay_protocol": underlay_protocol,
                "naming_convention": "standard",
                "fabric_interface_sorting_method": "bottom_up",
                "fabric_templates": _fabric_templates(
                    amount_of_super_spines=amount_of_super_spines,
                    super_spine_template=super_spine_template,
                    amount_of_hyper_spines=amount_of_hyper_spines,
                    hyper_spine_template=hyper_spine_template,
                ),
                "loopback_pool": None,
                "technical_pool": None,
                "management_pool": None,
                "fabric_asn_pool": None,
                "vni_pool": None,
                "evpn_rt_as": None,
                "children": [],
            }
        ]
    }
