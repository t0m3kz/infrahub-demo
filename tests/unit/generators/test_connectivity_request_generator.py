"""Unit tests for ConnectivityRequestGenerator."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.connectivity.request import ConnectableEndpoint, ConnectivityRequestGenerator, matching_circuit_ids
from generators.protocols import TopologyConnectivityRequest


def _make_gen() -> Any:
    gen = ConnectivityRequestGenerator.__new__(ConnectivityRequestGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock()
    return gen


def _payload(src: dict[str, Any] | None, dst: dict[str, Any] | None, conn_type: str = "cloud_onramp") -> dict[str, Any]:
    def _edge(loc: dict[str, Any] | None) -> dict[str, Any]:
        if loc is None:
            return {"node": None}
        return {
            "node": {
                "id": loc["id"],
                "__typename": loc.get("typename", "TopologyDataCenter"),
                "display_label": loc.get("label", loc["id"]),
                "circuits": {"edges": [{"node": {"id": c}} for c in loc.get("circuits", [])]},
            }
        }

    return {
        "TopologyConnectivityRequest": {
            "edges": [
                {
                    "node": {
                        "id": "req-1",
                        "name": {"value": "REQ-DC1-AWS"},
                        "connectivity_type": {"value": conn_type},
                        "status": {"value": "requested"},
                        "source": _edge(src),
                        "destination": _edge(dst),
                        "realized_circuits": {"edges": []},
                    }
                }
            ]
        }
    }


def test_matching_circuit_ids_intersects_endpoints() -> None:
    src = ConnectableEndpoint("dc-1", "TopologyDataCenter", "DC1", frozenset({"c-1", "c-shared"}))
    dst = ConnectableEndpoint("aws-1", "TopologyCloudRegion", "AWS", frozenset({"c-shared", "c-2"}))
    assert matching_circuit_ids(src, dst) == ["c-shared"]


@pytest.mark.asyncio
async def test_links_shared_circuits_and_sets_active() -> None:
    gen = _make_gen()
    node = MagicMock()
    node.status.value = "requested"
    node.add_relationships = AsyncMock()
    node.save = AsyncMock()
    gen.client.get.return_value = node

    await gen.generate(_payload({"id": "dc-1", "circuits": ["c-shared"]}, {"id": "aws-1", "circuits": ["c-shared"]}))

    gen.client.get.assert_awaited_once_with(kind=TopologyConnectivityRequest, id="req-1")
    node.add_relationships.assert_awaited_once_with(relation_to_update="realized_circuits", related_nodes=["c-shared"])
    assert node.status.value == "active"


@pytest.mark.asyncio
async def test_sets_provisioning_when_no_circuits_exist() -> None:
    gen = _make_gen()
    node = MagicMock()
    node.status.value = "requested"
    node.add_relationships = AsyncMock()
    node.save = AsyncMock()
    gen.client.get.return_value = node

    await gen.generate(_payload({"id": "colo-1", "circuits": []}, {"id": "saas-1", "circuits": []}, "saas_peering"))

    node.add_relationships.assert_not_awaited()
    assert node.status.value == "provisioning"


@pytest.mark.asyncio
async def test_rejects_same_source_and_destination() -> None:
    gen = _make_gen()
    with pytest.raises(Exception, match="cannot be the same"):
        await gen.generate(_payload({"id": "dc-1"}, {"id": "dc-1"}))
