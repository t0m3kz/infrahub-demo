"""Shared builders and fixtures for the ``test_segment_generators_*`` modules (VxlanSegmentGenerator unit tests)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.segment import VxlanSegmentGenerator

# Harness helpers


def _make_gen() -> Any:
    """Return a VxlanSegmentGenerator instance with mocked client and logger."""
    gen = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    # generate() waits for an in-flight add_dc/dc_pod_cascade on a target deployment missing its vni_pool (see
    # customer_dc.py's identical wait) — no in-flight parent in these unit tests, so no-op.
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    # The port tag and VLAN row reconciliation has its own tests (tests/unit/test_segment_vlan_reconcile.py,
    # test_app_instance_segment_generator.py) — stubbed at the boundary.
    gen.reconcile_vxlan_segment = AsyncMock()
    # Tenant addressing has its own test class (TestEnsureSegmentAddressing).
    gen.ensure_segment_addressing = AsyncMock()
    # The default-deny policy has its own tests (test_segment_firewall_mixin.py TestEnsureSegmentPolicy).
    gen.ensure_segment_policy = AsyncMock()
    return gen


# Raw GQL response builder


def _seg_response(
    seg_id: str,
    seg_name: str,
    deployments: list[dict],
    stretch_scope: str | None = None,
) -> dict:
    """Build a raw (un-cleaned) GraphQL response for a vxlan_segment_data query."""

    def _pool_node(pool: dict | None) -> dict | None:
        if pool is None:
            return None
        return {"node": {"id": pool["id"], "name": {"value": pool["name"]}}}

    dep_edges = []
    for d in deployments:
        node: dict[str, Any] = {"id": d["id"], "name": {"value": d["name"]}}
        if "parent" in d:
            parent = d["parent"]
            parent_node: dict[str, Any] = {"id": parent["id"], "name": {"value": parent["name"]}}
            if "vni_pool" in parent:
                parent_node["vni_pool"] = _pool_node(parent["vni_pool"])
            node["parent"] = {"node": parent_node}
        if "vni_pool" in d:
            # `... on TopologyCustomerOffice { vni_pool }`: the footprint hosts itself.
            node["vni_pool"] = _pool_node(d["vni_pool"])
        dep_edges.append({"node": node})
    return {
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": seg_id,
                        "__typename": "ManagedVxlanSegment",
                        "name": {"value": seg_name},
                        "environment": {"value": "p"},
                        "customer_deployments": {"edges": dep_edges},
                        **({"stretch_scope": {"value": stretch_scope}} if stretch_scope else {}),
                    }
                }
            ]
        }
    }


# Convenience customer-deployment fixtures — each resolves to hosting parent dc-1/dc-2
_DEP_1 = {
    "id": "cust-1",
    "name": "C001-P-DC1",
    "parent": {
        "id": "dc-1",
        "name": "DC-1",
        "vni_pool": {"id": "pool-vni-1", "name": "DC1-VNI-Pool"},
    },
}
_DEP_2 = {
    "id": "cust-2",
    "name": "C002-P-DC2",
    "parent": {
        "id": "dc-2",
        "name": "DC-2",
        "vni_pool": {"id": "pool-vni-2", "name": "DC2-VNI-Pool"},
    },
}
