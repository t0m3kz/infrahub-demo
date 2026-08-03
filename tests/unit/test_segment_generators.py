"""Unit tests for VxlanSegmentGenerator.

VlanSegment has no generator — vlan_id is a plain manual attribute
(single-site, no pool allocation). Only VXLAN needs generator-driven
per-deployment realization (SegmentDeployment) since it can stretch
across multiple sites.

VxlanSegmentGenerator.generate() cleans GraphQL data, guards on missing
id/name/customer_deployments, resolves each customer footprint to its
hosting parent (TopologyDataCenter or TopologyColocationMetro) via
_resolve_hosting_parent(), then calls _activate_segment_in_deployment()
for each resolved parent, followed by interface assignment and inline
sub-interface creation.

_activate_segment_in_deployment() does an idempotency check via
client.filters(), allocates VLAN ID and VNI, then calls client.create() / save().

Tests use asyncio.run() directly — same pattern as test_circuit_generators.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import ManagedSegmentDeployment, TopologySegmentHosting
from generators.topology.segment import VxlanSegmentGenerator

# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    """Return a VxlanSegmentGenerator instance with mocked client, logger, and
    empty DC cache.

    Typed as Any so that ty does not flag mock attribute assignments or
    mock method calls (e.g. gen.logger.error.assert_called_once()).
    """
    gen = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen._dc_cache = {}
    return gen


def _mock_pool(pool_id: str = "pool-1", name: str = "Test-Pool") -> MagicMock:
    pool = MagicMock()
    pool.id = pool_id
    pool.name.value = name
    return pool


# ---------------------------------------------------------------------------
# Raw GQL response builder
# ---------------------------------------------------------------------------


def _seg_response(
    seg_id: str,
    seg_name: str,
    deployments: list[dict],
) -> dict:
    """Build a raw (un-cleaned) GraphQL response for a vxlan_segment_data query.

    Each entry in `deployments` is a customer footprint (CustomerDC/Colocation)
    dict with "id", "name", and "parent" (the hosting DC/Metro dict) — mirroring
    the `... on TopologyCustomerDC/Colocation { parent { node {...} } }` fragment
    in the real vxlan_segment query.
    """
    dep_edges = [
        {
            "node": {
                "id": d["id"],
                "name": {"value": d["name"]},
                "parent": {"node": {"id": d["parent"]["id"], "name": {"value": d["parent"]["name"]}}},
            }
        }
        for d in deployments
    ]
    return {
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": seg_id,
                        "name": {"value": seg_name},
                        "customer_deployments": {"edges": dep_edges},
                    }
                }
            ]
        }
    }


# Convenience customer-deployment fixtures — each resolves to hosting parent dc-1/dc-2
_DEP_1 = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
_DEP_2 = {"id": "cust-2", "name": "C002-P-DC2", "parent": {"id": "dc-2", "name": "DC-2"}}

# ===========================================================================
# TestVxlanSegmentGeneratorGenerate
# ===========================================================================


class TestVxlanSegmentGeneratorGenerate:
    """Tests for generate() on VxlanSegmentGenerator."""

    def test_empty_response_logs_error(self):
        gen = _make_gen()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()
        data = {"ManagedVxlanSegment": {"edges": []}}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "No ManagedVxlanSegment" in gen.logger.error.call_args[0][0]

    def test_missing_segment_id_logs_error(self):
        gen = _make_gen()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()
        # id="" triggers the "missing id or name" guard
        data = _seg_response(seg_id="", seg_name="vxlan-1000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "missing id or name" in gen.logger.error.call_args[0][0]

    def test_no_deployments_logs_error(self):
        gen = _make_gen()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[])
        asyncio.run(gen.generate(data))
        gen.logger.warning.assert_called_once()
        warning_msg = gen.logger.warning.call_args[0][0]
        assert "customer deployment" in warning_msg.lower()
        gen.logger.error.assert_called_once()
        assert "could not resolve" in gen.logger.error.call_args[0][0].lower()
        # Must not have called client.create (no SegmentDeployment creation)
        gen.client.create.assert_not_called()

    def test_happy_path_calls_activate_for_each_deployment(self):
        """Two deployments produce two _activate_segment_in_deployment calls."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])
        asyncio.run(gen.generate(data))

        assert gen._activate_segment_in_deployment.call_count == 2
        calls = gen._activate_segment_in_deployment.call_args_list
        dep_ids_called = {c.kwargs["deployment_id"] for c in calls}
        assert dep_ids_called == {"dc-1", "dc-2"}
        for c in calls:
            assert c.kwargs["segment_id"] == "seg-1"

    def test_deployment_missing_id_is_skipped(self):
        """A customer deployment entry with id='' is skipped; only the valid one triggers activation."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = {
            "ManagedVxlanSegment": {
                "edges": [
                    {
                        "node": {
                            "id": "seg-1",
                            "name": {"value": "vxlan-1000"},
                            "customer_deployments": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "cust-1",
                                            "name": {"value": "C001-P-DC1"},
                                            "parent": {"node": {"id": "dc-1", "name": {"value": "DC-1"}}},
                                        }
                                    },
                                    {"node": {"id": "", "name": {"value": "CUST-BAD"}}},
                                ]
                            },
                        }
                    }
                ]
            }
        }
        asyncio.run(gen.generate(data))

        assert gen._activate_segment_in_deployment.call_count == 1
        assert gen._activate_segment_in_deployment.call_args.kwargs["deployment_id"] == "dc-1"

    def test_generate_calls_interface_assignment_and_inline_creation(self):
        """generate() calls _activate_segment_in_deployment, _assign_to_deployment_interfaces,
        and _create_inline_sub_interfaces."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-3", seg_name="vxlan-2000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_awaited_once()
        gen._assign_to_deployment_interfaces.assert_awaited_once()
        gen._create_inline_sub_interfaces.assert_awaited_once()


# ===========================================================================
# TestResolveHostingParent
# ===========================================================================


class TestResolveHostingParent:
    """Tests for _resolve_hosting_parent — resolves a customer footprint
    (CustomerDC/CustomerColocation) to its hosting parent (DC/Metro dict)."""

    def test_returns_parent_when_present(self):
        gen = _make_gen()
        customer_deployment = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
        result = gen._resolve_hosting_parent(customer_deployment, "vxlan-1000")
        assert result == {"id": "dc-1", "name": "DC-1"}

    def test_missing_id_logs_warning_returns_none(self):
        gen = _make_gen()
        result = gen._resolve_hosting_parent({"name": "no-id"}, "vxlan-1000")
        assert result is None
        gen.logger.warning.assert_called_once()

    def test_missing_parent_logs_error_returns_none(self):
        """No fallback fetch: if the query response has no parent, log and skip."""
        gen = _make_gen()
        result = gen._resolve_hosting_parent({"id": "cust-1", "name": "C001-P-DC1"}, "vxlan-1000")
        assert result is None
        gen.logger.error.assert_called_once()
        assert "no parent in the query response" in gen.logger.error.call_args[0][0]
        gen.client.get.assert_not_called()


# ===========================================================================
# TestGetDcPool
# ===========================================================================


class TestGetDcPool:
    """Tests for _get_dc_pool — fetches vlan_pool/vni_pool/l3_vni_pool via the
    generic TopologySegmentHosting kind (resolves both DC and Metro ids)."""

    def test_fetches_via_segment_hosting_generic_kind(self):
        gen = _make_gen()
        pool_peer = MagicMock()
        pool_peer.id = "pool-1"
        pool_rel = MagicMock()
        pool_rel.peer = pool_peer
        parent_obj = MagicMock()
        parent_obj.vlan_pool = pool_rel
        gen.client.get = AsyncMock(return_value=parent_obj)

        result = asyncio.run(gen._get_dc_pool("metro-1", "FR", "vlan_pool"))

        gen.client.get.assert_awaited_once()
        call_kwargs = gen.client.get.call_args.kwargs
        assert call_kwargs["kind"] == TopologySegmentHosting
        assert call_kwargs["id"] == "metro-1"
        assert result is pool_peer

    def test_caches_parent_across_calls(self):
        gen = _make_gen()
        pool_rel = MagicMock()
        pool_rel.peer = MagicMock(id="pool-1")
        parent_obj = MagicMock()
        parent_obj.vlan_pool = pool_rel
        parent_obj.vni_pool = pool_rel
        gen.client.get = AsyncMock(return_value=parent_obj)

        asyncio.run(gen._get_dc_pool("dc-1", "DC-1", "vlan_pool"))
        asyncio.run(gen._get_dc_pool("dc-1", "DC-1", "vni_pool"))

        gen.client.get.assert_awaited_once()


# ===========================================================================
# TestActivateSegmentInDeployment
# ===========================================================================


class TestActivateSegmentInDeployment:
    """Tests for _activate_segment_in_deployment on VxlanSegmentGenerator."""

    # Common invocation kwargs used in every test
    _CALL = dict(
        segment_id="seg-1",
        segment_name="vxlan-1000",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen) -> None:
        asyncio.run(gen._activate_segment_in_deployment(**self._CALL))

    def test_idempotent_existing_deployment_skips_create(self):
        """When client.filters returns an existing record, create is never called."""
        gen = _make_gen()
        existing = MagicMock()
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])

        self._run(gen)

        gen.client.create.assert_not_called()
        existing.save.assert_called_once()
        # An info message mentioning "already exists" should be logged
        info_msgs = " ".join(str(c) for c in gen.logger.info.call_args_list)
        assert "already exists" in info_msgs

    def test_missing_vlan_pool_logs_error(self):
        """No vlan_pool → error logged, no create call."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing
        gen._get_dc_pool = AsyncMock(return_value=None)

        self._run(gen)

        gen.client.create.assert_not_called()
        error_msg = gen.logger.error.call_args[0][0]
        assert "vlan_pool" in error_msg

    def test_creates_segment_deployment_with_vlan_pool(self):
        """Happy path: no existing record, pool found → create() called with correct data."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        vlan_pool = _mock_pool("pool-vlan", "DC1-VLAN-Pool")
        vni_pool = _mock_pool("pool-vni", "DC1-VNI-Pool")
        gen._get_dc_pool = AsyncMock(side_effect=[vlan_pool, vni_pool])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen)

        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == ManagedSegmentDeployment
        call_data = call_kwargs["data"]

        assert call_data["segment"] == {"id": "seg-1"}
        assert call_data["deployment"] == {"id": "dc-1"}
        assert "from_pool" in call_data["vlan_id"]
        assert call_data["vlan_id"]["from_pool"]["id"] == "pool-vlan"
        activation.save.assert_called_once()

    def test_create_exception_logs_error(self):
        """client.create() raising an exception results in an error log."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen._get_dc_pool = AsyncMock(return_value=_mock_pool())
        gen.client.create = AsyncMock(side_effect=Exception("network timeout"))

        self._run(gen)

        error_msg = gen.logger.error.call_args[0][0]
        assert "Failed to create" in error_msg

    def test_idempotency_check_exception_still_proceeds(self):
        """If client.filters raises, a warning is logged but execution continues
        to the vlan_pool lookup (does not silently drop the activation)."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(side_effect=Exception("timeout"))
        # After the warning, the method falls through to vlan_pool lookup
        gen._get_dc_pool = AsyncMock(return_value=None)  # no pool → error path

        self._run(gen)

        # Warning about the exception during idempotency check
        warning_msgs = " ".join(str(c) for c in gen.logger.warning.call_args_list)
        assert "Error checking existing activations" in warning_msgs or len(gen.logger.warning.call_args_list) >= 1
        # Fell through to pool lookup, which returned None → error about vlan_pool
        gen.logger.error.assert_called_once()
        assert "vlan_pool" in gen.logger.error.call_args[0][0]


# ===========================================================================
# TestVxlanVniAllocation
# ===========================================================================


class TestVxlanVniAllocation:
    """Tests for VNI allocation logic inside _activate_segment_in_deployment."""

    _CALL = dict(
        segment_id="seg-2",
        segment_name="vxlan-1001",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen) -> None:
        asyncio.run(gen._activate_segment_in_deployment(**self._CALL))

    def test_reuses_existing_vni_from_other_deployment(self):
        """When another DC already has a SegmentDeployment with a VNI, that value
        is reused as a literal integer — no pool allocation for VNI."""
        gen = _make_gen()

        existing_dep = MagicMock()
        existing_dep.resolve = AsyncMock()
        existing_dep.vni = MagicMock()
        existing_dep.vni.value = 10100

        # First call: idempotency check → no existing for this dc
        # Second call: VNI reuse check → one existing SegmentDeployment with VNI
        gen.client.filters = AsyncMock(side_effect=[[], [existing_dep]])

        vlan_pool = _mock_pool("pool-vlan", "DC1-VLAN-Pool")
        gen._get_dc_pool = AsyncMock(return_value=vlan_pool)

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen)

        call_data = gen.client.create.call_args.kwargs["data"]
        assert call_data["vni"] == 10100

    def test_allocates_vni_from_pool_when_first_dc(self):
        """When no prior SegmentDeployment exists, VNI is allocated from vni_pool
        via from_pool dict syntax."""
        gen = _make_gen()

        # Both idempotency and VNI-reuse checks return empty
        gen.client.filters = AsyncMock(side_effect=[[], []])

        vlan_pool = _mock_pool("pool-vlan", "DC1-VLAN-Pool")
        vni_pool = _mock_pool("pool-vni", "DC1-VNI-Pool")
        gen._get_dc_pool = AsyncMock(side_effect=[vlan_pool, vni_pool])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen)

        call_data = gen.client.create.call_args.kwargs["data"]
        assert "from_pool" in call_data["vni"]
        assert call_data["vni"]["from_pool"]["id"] == "pool-vni"

    def test_no_vni_pool_logs_warning_but_still_creates(self):
        """If no vni_pool is found (and no prior VNI to reuse), a warning is logged
        but client.create() is still called — 'vni' is simply absent from call_data."""
        gen = _make_gen()

        gen.client.filters = AsyncMock(side_effect=[[], []])

        vlan_pool = _mock_pool("pool-vlan", "DC1-VLAN-Pool")
        # vlan_pool is returned first, then None for vni_pool
        gen._get_dc_pool = AsyncMock(side_effect=[vlan_pool, None])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen)

        warning_msgs = " ".join(str(c) for c in gen.logger.warning.call_args_list)
        assert "vni_pool" in warning_msgs

        gen.client.create.assert_called_once()
        call_data = gen.client.create.call_args.kwargs["data"]
        assert "vni" not in call_data
