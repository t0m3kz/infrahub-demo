"""Unit tests for segment generators (VlanSegmentGenerator, VxlanSegmentGenerator).

Both classes share BaseSegmentGenerator logic:
  - generate() cleans GraphQL data, guards on missing id/name/deployments,
    then calls _activate_segment_in_deployment() for each deployment.
  - _activate_segment_in_deployment() does an idempotency check via
    client.filters(), allocates VLAN ID (always) and VNI (VXLAN only),
    then calls client.create() / save().

Tests use asyncio.run() directly — same pattern as test_circuit_generators.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.add.segment import VlanSegmentGenerator, VxlanSegmentGenerator

# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_gen(cls) -> Any:
    """Return an instance of cls with mocked client, logger, and empty DC cache.

    Typed as Any so that ty does not flag mock attribute assignments or
    mock method calls (e.g. gen.logger.error.assert_called_once()).
    """
    gen = cls.__new__(cls)
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
    kind: str,
    seg_id: str,
    seg_name: str,
    deployments: list[dict],
) -> dict:
    """Build a raw (un-cleaned) GraphQL response for a segment query."""
    dep_edges = [{"node": {"id": d["id"], "name": {"value": d["name"]}}} for d in deployments]
    return {
        kind: {
            "edges": [
                {
                    "node": {
                        "id": seg_id,
                        "name": {"value": seg_name},
                        "deployments": {"edges": dep_edges},
                    }
                }
            ]
        }
    }


# Convenience deployment fixtures
_DEP_1 = {"id": "dc-1", "name": "DC-1"}
_DEP_2 = {"id": "dc-2", "name": "DC-2"}

# ===========================================================================
# TestVlanSegmentGeneratorGenerate
# ===========================================================================


class TestVlanSegmentGeneratorGenerate:
    """Tests for generate() on VlanSegmentGenerator (allocate_vni=False)."""

    def test_empty_response_logs_error(self):
        gen = _make_gen(VlanSegmentGenerator)
        data = {"ManagedVlanSegment": {"edges": []}}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "No ManagedVlanSegment" in gen.logger.error.call_args[0][0]

    def test_missing_segment_id_logs_error(self):
        gen = _make_gen(VlanSegmentGenerator)
        # id="" triggers the "missing id or name" guard
        data = _seg_response("ManagedVlanSegment", seg_id="", seg_name="vlan-100", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "missing id or name" in gen.logger.error.call_args[0][0]

    def test_no_deployments_logs_warning(self):
        gen = _make_gen(VlanSegmentGenerator)
        data = _seg_response("ManagedVlanSegment", seg_id="seg-1", seg_name="vlan-100", deployments=[])
        asyncio.run(gen.generate(data))
        gen.logger.warning.assert_called_once()
        warning_msg = gen.logger.warning.call_args[0][0]
        assert (
            "no target deployments" in warning_msg.lower()
            or "no deployments" in warning_msg.lower()
            or "deployments" in warning_msg.lower()
        )
        # Must not have called _activate_segment_in_deployment (no create calls)
        gen.client.create.assert_not_called()

    def test_happy_path_calls_activate_for_each_deployment(self):
        """Two deployments produce two _activate_segment_in_deployment calls."""
        gen = _make_gen(VlanSegmentGenerator)
        gen._activate_segment_in_deployment = AsyncMock()

        data = _seg_response(
            "ManagedVlanSegment",
            seg_id="seg-1",
            seg_name="vlan-100",
            deployments=[_DEP_1, _DEP_2],
        )
        asyncio.run(gen.generate(data))

        assert gen._activate_segment_in_deployment.call_count == 2
        # Verify the segment and deployment ids passed through correctly
        calls = gen._activate_segment_in_deployment.call_args_list
        dep_ids_called = {c.kwargs["deployment_id"] for c in calls}
        assert dep_ids_called == {"dc-1", "dc-2"}
        for c in calls:
            assert c.kwargs["segment_id"] == "seg-1"

    def test_deployment_missing_id_is_skipped(self):
        """A deployment entry with id='' is skipped; only the valid one triggers activation."""
        gen = _make_gen(VlanSegmentGenerator)
        gen._activate_segment_in_deployment = AsyncMock()

        # Build response manually so one dep has an empty id
        data = {
            "ManagedVlanSegment": {
                "edges": [
                    {
                        "node": {
                            "id": "seg-1",
                            "name": {"value": "vlan-100"},
                            "deployments": {
                                "edges": [
                                    {"node": {"id": "dc-1", "name": {"value": "DC-1"}}},
                                    {"node": {"id": "", "name": {"value": "DC-BAD"}}},
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


# ===========================================================================
# TestActivateSegmentInDeployment  (VlanSegmentGenerator, allocate_vni=False)
# ===========================================================================


class TestActivateSegmentInDeployment:
    """Tests for _activate_segment_in_deployment on VlanSegmentGenerator."""

    # Common invocation kwargs used in every test
    _CALL = dict(
        segment_id="seg-1",
        segment_name="vlan-100",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen) -> None:
        asyncio.run(gen._activate_segment_in_deployment(**self._CALL))

    def test_idempotent_existing_deployment_skips_create(self):
        """When client.filters returns an existing record, create is never called."""
        gen = _make_gen(VlanSegmentGenerator)
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
        gen = _make_gen(VlanSegmentGenerator)
        gen.client.filters = AsyncMock(return_value=[])  # no existing
        gen._get_dc_pool = AsyncMock(return_value=None)

        self._run(gen)

        gen.client.create.assert_not_called()
        error_msg = gen.logger.error.call_args[0][0]
        assert "vlan_pool" in error_msg

    def test_creates_segment_deployment_with_vlan_pool(self):
        """Happy path: no existing record, pool found → create() called with correct data."""
        gen = _make_gen(VlanSegmentGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        vlan_pool = _mock_pool("pool-vlan", "DC1-VLAN-Pool")
        gen._get_dc_pool = AsyncMock(return_value=vlan_pool)

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen)

        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == "ManagedSegmentDeployment"
        call_data = call_kwargs["data"]

        assert call_data["segment"] == {"id": "seg-1"}
        assert call_data["deployment"] == {"id": "dc-1"}
        assert "from_pool" in call_data["vlan_id"]
        assert call_data["vlan_id"]["from_pool"]["id"] == "pool-vlan"
        activation.save.assert_called_once()

    def test_create_exception_logs_error(self):
        """client.create() raising an exception results in an error log."""
        gen = _make_gen(VlanSegmentGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        gen._get_dc_pool = AsyncMock(return_value=_mock_pool())
        gen.client.create = AsyncMock(side_effect=Exception("network timeout"))

        self._run(gen)

        error_msg = gen.logger.error.call_args[0][0]
        assert "Failed to create" in error_msg

    def test_idempotency_check_exception_still_proceeds(self):
        """If client.filters raises, a warning is logged but execution continues
        to the vlan_pool lookup (does not silently drop the activation)."""
        gen = _make_gen(VlanSegmentGenerator)
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
# TestVxlanVniAllocation  (VxlanSegmentGenerator, allocate_vni=True)
# ===========================================================================


class TestVxlanVniAllocation:
    """Tests for VNI allocation logic inside _activate_segment_in_deployment
    on VxlanSegmentGenerator."""

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
        gen = _make_gen(VxlanSegmentGenerator)

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
        gen = _make_gen(VxlanSegmentGenerator)

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
        gen = _make_gen(VxlanSegmentGenerator)

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


# ===========================================================================
# TestVxlanGenerateCallsInterfaceAssignment
# ===========================================================================


class TestVxlanGenerateCallsInterfaceAssignment:
    """VxlanSegmentGenerator.generate() must call both base activation and
    _assign_to_deployment_interfaces after processing segment data."""

    def test_generate_calls_assign_to_deployment_interfaces(self):
        gen = _make_gen(VxlanSegmentGenerator)
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()

        data = _seg_response(
            "ManagedVxlanSegment",
            seg_id="seg-3",
            seg_name="vxlan-2000",
            deployments=[_DEP_1],
        )
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_awaited_once()
        gen._assign_to_deployment_interfaces.assert_awaited_once_with(data)
