"""Shared builders for the ``test_dc_generator*`` modules: a DC deployment payload and a generator with its mixins mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.shared_routing import SharedRoutingIds
from generators.topology.dc import DCTopologyGenerator
from tests.unit.simulators.fabric_designs import custom_dc_design, dc_design


def _dc_design(
    *,
    max_super_spines_per_fabric: int = 2,
    max_hyper_spines_per_fabric: int = 0,
    max_border_leafs_per_fabric: int = 4,
    max_pods: int = 2,
) -> dict[str, Any]:
    """A throwaway DC design (custom_dc_design: hosting one 2-spine, 2-leaf pod
    design) with a test scenario's own tier caps. routing_strategy/underlay_protocol
    live directly on the DC instance (see _deployment()), not here."""
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
    """Build a fabric_templates list with super-spine/hyper-spine entries, mirroring
    the pre-refactor amount_of_super_spines/super_spine_template scalar kwargs."""
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


def _make_generator() -> Any:
    gen = DCTopologyGenerator.__new__(DCTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.filters = AsyncMock(return_value=[])
    # The DC node attach_refs fetches by id (the generator's own target).
    gen.client.get = AsyncMock(return_value=MagicMock(save=AsyncMock()))

    # Stub out every CommonGenerator/RoutingMixin collaborator generate() calls —
    # these are exercised by their own dedicated test modules, not here.
    # acquire_resource_lock/release_resource_lock serialize the super-spine<->
    # hyper-spine cabling call against a concurrent, overlapping generator run
    # for the same DC — not under test here, and the lock's own client.create()
    # call has no CoreStandardGroup-shaped mock on this plain MagicMock client.
    gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
    gen.release_resource_lock = AsyncMock()
    gen.allocate_resource_pools = AsyncMock(
        return_value={name: MagicMock(id=f"{name}-pool-1") for name in ("loopback", "management", "technical")}
    )
    gen.upsert_asn_pool = AsyncMock(return_value=MagicMock(id="asn-pool-1"))
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="num-pool-1"))
    gen.create_devices = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()
    gen.create_cabling = AsyncMock(return_value=[])
    gen._create_shared_routing_objects = AsyncMock(
        return_value=SharedRoutingIds(super_spine_as_id=None, overlay_as_id="fabric-as-1")
    )
    return gen
