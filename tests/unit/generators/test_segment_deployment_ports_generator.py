"""Unit tests for SegmentDeploymentPortsGenerator (generators/segments/segment_deployment_ports.py): a port assigned to
a segment deployment re-runs the segment's reconcile."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.segments.segment_deployment_ports import SegmentDeploymentPortsGenerator


def _gen() -> Any:
    """A generator with a mocked reconcile and logger (no client)."""
    gen = SegmentDeploymentPortsGenerator.__new__(SegmentDeploymentPortsGenerator)
    gen.logger = MagicMock()
    gen.reconcile_vxlan_segment = AsyncMock(return_value=(1, 0))
    return gen


def _data(typename: str = "ManagedVxlanSegment", segment_id: str = "seg-1") -> dict[str, Any]:
    """A raw segment_deployment_ports.gql response for one deployment."""
    segment = {"id": segment_id, "__typename": typename, "name": {"value": "c900-web-p"}}
    node = {"id": "sd-1", "deployment": {"node": {"name": {"value": "DC-KTW1"}}}, "segment": {"node": segment}}
    return {"ManagedSegmentDeployment": {"edges": [{"node": node}]}}


def test_the_reconcile_runs_for_the_deployments_segment() -> None:
    """The deployment names the segment the reconcile runs on."""
    gen = _gen()
    asyncio.run(gen.generate(_data()))
    gen.reconcile_vxlan_segment.assert_awaited_once_with("seg-1", "c900-web-p")


def test_a_cloud_segment_or_no_deployment_runs_nothing() -> None:
    """Only a VXLAN segment has VLAN rows; an empty answer is logged as an error."""
    gen = _gen()
    asyncio.run(gen.generate(_data(typename="CloudNetworkSegment")))
    asyncio.run(gen.generate({"ManagedSegmentDeployment": {"edges": []}}))
    gen.reconcile_vxlan_segment.assert_not_awaited()
    gen.logger.error.assert_called_once()


def test_a_segment_that_is_gone_is_logged() -> None:
    """The reconcile finds no VXLAN segment: logged, not raised."""
    gen = _gen()
    gen.reconcile_vxlan_segment = AsyncMock(return_value=None)
    asyncio.run(gen.generate(_data()))
    assert "gone" in str(gen.logger.info.call_args)
