"""Unit tests for ServicePortMixin (generators/service_ports.py)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.common import CommonGenerator
from generators.service_ports import ServicePortMixin


def _make_gen() -> Any:
    gen = ServicePortMixin.__new__(ServicePortMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen._safe_rel_add = CommonGenerator._safe_rel_add
    return gen


class TestReconcileComponentServicePorts:
    """AppEndpoint is user-authored data this generator only enriches, not a
    generated artifact it owns. self.client and self._init_client are the
    SAME object — start_tracking() mutates it in place (sets .mode =
    TRACKING) rather than swapping in a separate instance — so fetching via
    one vs. the other makes no difference. The only real lever is passing
    update_group_context=False to save(): without it, save() while
    self.client.mode == TRACKING registers the endpoint as a group member
    only on runs that link a *new* port; an idempotent re-run linking
    nothing then leaves it unregistered and the SDK's delete_unused_nodes
    tries to delete it."""

    @staticmethod
    def _edge(endpoint_id: str = "ep-1") -> tuple[dict, dict, dict]:
        src_comp = {"id": "comp-frontend", "name": "frontend"}
        dep = {"id": "dep-1", "name": "frontend-to-backend", "protocol": "tcp", "port_start": 443, "port_end": None}
        dst_endpoint = {"id": endpoint_id, "name": "payment-gateway"}
        return src_comp, dep, dst_endpoint

    @staticmethod
    def _components(endpoint_id: str = "ep-1") -> list[dict]:
        return [
            {
                "id": "comp-frontend",
                "children": [{"id": endpoint_id, "name": "payment-gateway"}],
            }
        ]

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        endpoint_obj = MagicMock()
        endpoint_obj.save = AsyncMock()
        service_ports_rel = MagicMock()
        service_ports_rel.fetch = AsyncMock()
        service_ports_rel.peers = []
        service_ports_rel.add = MagicMock()
        endpoint_obj.service_ports = service_ports_rel
        gen.client.get = AsyncMock(return_value=endpoint_obj)
        port_obj = MagicMock()
        port_obj.id = "port-443-tcp"
        port_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=port_obj)
        return gen, endpoint_obj

    def test_endpoint_saved_with_update_group_context_false_when_new_port_linked(self):
        gen, endpoint_obj = self._make_gen_ready()

        asyncio.run(gen._reconcile_component_service_ports(self._components(), [self._edge()]))

        endpoint_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_service_port_object_still_created_via_tracked_client(self):
        gen, _endpoint_obj = self._make_gen_ready()

        asyncio.run(gen._reconcile_component_service_ports(self._components(), [self._edge()]))

        gen.client.create.assert_awaited_once()

    def test_endpoint_not_saved_when_port_already_linked(self):
        gen, endpoint_obj = self._make_gen_ready()
        existing_peer = MagicMock()
        existing_peer.id = "port-443-tcp"
        endpoint_obj.service_ports.peers = [existing_peer]

        asyncio.run(gen._reconcile_component_service_ports(self._components(), [self._edge()]))

        endpoint_obj.save.assert_not_awaited()
