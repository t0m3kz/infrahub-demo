"""Unit tests for generators/far_end.py's batched far_end_interfaces()."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.far_end import far_end_interfaces


def _iface(iface_id: str, cable_id: str) -> Any:
    return SimpleNamespace(id=iface_id, cable=SimpleNamespace(id=cable_id))


def _cable(cable_id: str, *endpoint_ids: str) -> Any:
    return SimpleNamespace(
        id=cable_id, endpoints=SimpleNamespace(peers=[SimpleNamespace(id=peer) for peer in endpoint_ids])
    )


def _client(cables: list[Any], far_ends: list[Any]) -> Any:
    client = MagicMock()

    async def filters(*, kind: Any, ids: list[str], include: list[str]) -> list[Any]:
        if kind.__name__ == "DcimCable":
            return [cable for cable in cables if cable.id in ids]
        assert kind.__name__ == "DcimPhysicalInterface"
        return [node for node in far_ends if node.id in ids]

    client.filters = AsyncMock(side_effect=filters)
    client.get = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_no_interfaces_sends_no_query() -> None:
    """Nothing cabled to follow: no round trip at all."""
    client = _client([], [])

    assert await far_end_interfaces(client, [], include=["device"]) == {}
    client.filters.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_queries_for_any_number_of_interfaces() -> None:
    """The cables in one filters(ids), the far ends in one filters(ids, include), never a get."""
    port_a, port_b = SimpleNamespace(id="port-a"), SimpleNamespace(id="port-b")
    client = _client([_cable("c-1", "up-1", "port-a"), _cable("c-2", "port-b", "up-2")], [port_a, port_b])

    result = await far_end_interfaces(client, [_iface("up-1", "c-1"), _iface("up-2", "c-2")], include=["device"])

    assert result == {"up-1": port_a, "up-2": port_b}
    assert client.filters.await_count == 2
    cable_call, far_end_call = client.filters.call_args_list
    assert cable_call.kwargs == {"kind": cable_call.kwargs["kind"], "ids": ["c-1", "c-2"], "include": ["endpoints"]}
    assert far_end_call.kwargs["ids"] == ["port-a", "port-b"]
    assert far_end_call.kwargs["include"] == ["device"]
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_cable_without_another_endpoint_is_absent() -> None:
    """A cable with only this interface on it has no far end; the far-end query is skipped."""
    client = _client([_cable("c-1", "up-1")], [])

    assert await far_end_interfaces(client, [_iface("up-1", "c-1")], include=[]) == {}
    assert client.filters.await_count == 1


@pytest.mark.asyncio
async def test_far_end_that_is_not_a_physical_interface_is_absent(caplog: pytest.LogCaptureFixture) -> None:
    """The other endpoint is a patch panel port (not a DcimPhysicalInterface): no far end for that interface only.

    Skipped with a WARNING naming the port, never an error: the generators fail on an error log.
    """
    port_b = SimpleNamespace(id="port-b")
    client = _client([_cable("c-1", "up-1", "front-port"), _cable("c-2", "up-2", "port-b")], [port_b])

    with caplog.at_level(logging.DEBUG, logger="generators.far_end"):
        result = await far_end_interfaces(client, [_iface("up-1", "c-1"), _iface("up-2", "c-2")], include=[])

    assert result == {"up-2": port_b}
    assert [(record.levelno, "front-port" in record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, True)
    ]


@pytest.mark.asyncio
async def test_two_given_interfaces_on_one_cable_are_each_others_far_end() -> None:
    """Only the interface itself is excluded from its cable's endpoints, not every given interface."""
    up_1, up_2 = _iface("up-1", "c-1"), _iface("up-2", "c-1")
    client = _client([_cable("c-1", "up-1", "up-2")], [up_1, up_2])

    result = await far_end_interfaces(client, [up_1, up_2], include=[])

    assert result == {"up-1": up_2, "up-2": up_1}
    assert client.filters.call_args_list[0].kwargs["ids"] == ["c-1"]
