"""Unit tests for SdwanEdgeGenerator (generators/topology/sdwan_edge.py).

Payloads are written in the already-clean shape generate() sees after its own
clean_data() call — clean_data() is a no-op on input that already lacks
GraphQL {"value": ...}/{"edges": [{"node": ...}]} envelopes, the same
convention test_customer_colocation.py/test_customer_dc.py use.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.sdwan_edge import SdwanEdgeGenerator

_USE_DEFAULT_GATEWAY: Literal["__default__"] = "__default__"


def _make_gen() -> SdwanEdgeGenerator:
    gen = SdwanEdgeGenerator.__new__(SdwanEdgeGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    return gen


def _gateway(
    *,
    gateway_id: str = "gw-1",
    name: str = "EQX-FR2-SDWAN-GW1",
    zone_name: str | None = "FR2",
    trunk_role: str | None = "uplink",
) -> dict[str, Any]:
    interfaces = [{"id": "trunk-1", "name": "eth0", "role": trunk_role}] if trunk_role else []
    return {
        "id": gateway_id,
        "name": name,
        "deployment": {"id": "zone-1", "name": zone_name} if zone_name else None,
        "interfaces": interfaces,
    }


def _office_payload(
    *,
    office_id: str | None = "office-1",
    office_name: str = "C002-P",
    owner_id: str | None = "org-c002",
    gateway: dict[str, Any] | None | Literal["__default__"] = _USE_DEFAULT_GATEWAY,
) -> dict[str, Any]:
    if gateway == _USE_DEFAULT_GATEWAY:
        gateway = _gateway()
    # clean_data() collapses a single-key {"id": x} dict to the bare string x
    # (utils/data_cleaning.py's _EXTRACTION_RULES) — generate() always runs
    # clean_data() on whatever is passed in, so "owner" needs a second key
    # (matching what customer_office_sdwan.gql actually fetches: id + name)
    # or it silently stops being a dict.
    owner = {"id": owner_id, "name": "SwiftGo GmbH"} if owner_id else None
    return {
        "TopologyCustomerOffice": [
            {
                "id": office_id,
                "name": office_name,
                "environment": "p",
                "branch_to_branch": "full_mesh",
                "parent": {"id": "officecust-1", "owner": owner},
                "sdwan_gateway": gateway,
            }
        ]
    }


def _node(node_id: str, **extra: Any) -> Any:
    node = MagicMock(id=node_id)
    node.save = AsyncMock()
    for key, value in extra.items():
        setattr(node, key, value)
    return node


class TestGenerateGuardClauses:
    @pytest.mark.asyncio
    async def test_no_matching_kind_in_response_is_noop(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate({"TopologyCustomerDC": []})

        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_office_id_logs_error(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate(_office_payload(office_id=None))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_sdwan_gateway_is_a_noop(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate(_office_payload(gateway=None))

        gen.client.create.assert_not_called()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_owner_logs_error(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate(_office_payload(owner_id=None))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_gateway_with_no_deployment_logs_error(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate(_office_payload(gateway=_gateway(zone_name=None)))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_gateway_with_no_uplink_trunk_logs_error(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate(_office_payload(gateway=_gateway(trunk_role=None)))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()


class TestGenerateHappyPath:
    def _wire_full_success(self) -> SdwanEdgeGenerator:
        gen = _make_gen()

        edge = _node("edge-1")
        internet_circuit = _node("inet-1")
        overlay_circuit = _node("vc-1")
        overlay_circuit.tunnel_id = MagicMock(value=2000)
        iface_capabilities = MagicMock()
        iface_capabilities.fetch = AsyncMock()
        iface_capabilities.peers = []
        overlay_circuit.interface_capabilities = iface_capabilities
        gateway_subiface = _node("subiface-1")

        gen.client.create = AsyncMock(side_effect=[edge, internet_circuit, overlay_circuit, gateway_subiface])

        provider = MagicMock(id="provider-1")
        edge_template = MagicMock(id="template-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        orchestrator = MagicMock()
        orchestrator.name.value = "EQX-FR2-SDWAN-VCO1"
        managed_devices = MagicMock()
        managed_devices.fetch = AsyncMock()
        managed_devices.peers = []
        orchestrator.managed_devices = managed_devices
        orchestrator.save = AsyncMock()
        # Call order: (1) default-provider org lookup; (2) existing-edge-by-
        # name lookup -> none, so the edge is new and the template gets
        # resolved; (3) VCE-610_EDGE template lookup; (4) edge uplink
        # interface lookup; (5) re-fetch the just-created overlay circuit
        # (real __typename for interface_capabilities peers, see
        # _link_overlay_to_gateway_subinterface's docstring); (6) orchestrator
        # lookup.
        gen.client.filters = AsyncMock(
            side_effect=[[provider], [], [edge_template], [edge_uplink], [overlay_circuit], [orchestrator]]
        )

        pool = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(return_value=pool)

        self._edge = edge
        self._internet_circuit = internet_circuit
        self._overlay_circuit = overlay_circuit
        self._gateway_subiface = gateway_subiface
        self._orchestrator = orchestrator
        return gen

    @pytest.mark.asyncio
    async def test_full_flow_creates_every_object_in_order(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        create_kinds = [call.kwargs["kind"] for call in gen.client.create.call_args_list]
        assert [k.__name__ for k in create_kinds] == [
            "DcimPhysicalDevice",
            "TopologyPhysicalCircuit",
            "TopologyVirtualCircuit",
            "DcimVirtualInterface",
        ]

    @pytest.mark.asyncio
    async def test_new_edge_sends_object_template(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        edge_call = gen.client.create.call_args_list[0]
        assert edge_call.kwargs["data"]["object_template"] == {"id": "template-1"}
        assert "id" not in edge_call.kwargs["data"]

    @pytest.mark.asyncio
    async def test_existing_edge_upserts_by_id_without_resending_template(self) -> None:
        """Resending object_template on an existing device triggers a
        server-side re-instantiation error (generators/devices.py's own
        create_devices() avoids this the same way) — an existing edge must
        be upserted by id instead, with no template lookup at all."""
        gen = _make_gen()
        edge = _node("edge-1")
        internet_circuit = _node("inet-1")
        overlay_circuit = _node("vc-1")
        overlay_circuit.tunnel_id = MagicMock(value=2000)
        iface_capabilities = MagicMock()
        iface_capabilities.fetch = AsyncMock()
        iface_capabilities.peers = []
        overlay_circuit.interface_capabilities = iface_capabilities
        gateway_subiface = _node("subiface-1")
        gen.client.create = AsyncMock(side_effect=[edge, internet_circuit, overlay_circuit, gateway_subiface])

        existing_edge = MagicMock(id="edge-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        orchestrator = MagicMock()
        managed_devices = MagicMock()
        managed_devices.fetch = AsyncMock()
        managed_devices.peers = []
        orchestrator.managed_devices = managed_devices
        orchestrator.save = AsyncMock()
        provider = MagicMock(id="provider-1")
        # Existing-edge lookup now returns a match — no template lookup follows.
        # The 4th entry is the post-create re-fetch of the overlay circuit
        # (real __typename for interface_capabilities peers, see
        # _link_overlay_to_gateway_subinterface's docstring) — reusing the
        # same overlay_circuit mock, since only its .interface_capabilities
        # matters here, not a distinct identity.
        gen.client.filters = AsyncMock(
            side_effect=[[provider], [existing_edge], [edge_uplink], [overlay_circuit], [orchestrator]]
        )
        gen.client.get = AsyncMock(return_value=MagicMock(id="pool-1"))

        await gen.generate(_office_payload())

        edge_call = gen.client.create.call_args_list[0]
        assert edge_call.kwargs["data"]["id"] == "edge-1"
        assert "object_template" not in edge_call.kwargs["data"]

    @pytest.mark.asyncio
    async def test_gateway_subinterface_named_from_resolved_tunnel_id(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        subiface_call = gen.client.create.call_args_list[3]
        assert subiface_call.kwargs["data"]["name"] == "eth0.2000"

    @pytest.mark.asyncio
    async def test_circuits_use_real_ids_for_locations_not_names(self) -> None:
        """locations peers TopologyConnectableLocation — it must be given
        real resolved ids (office_id/gateway_zone_id), never the office's or
        gateway zone's *name* string wrapped in {"id": ...}, which would
        silently fail to resolve against a real backend."""
        gen = self._wire_full_success()

        await gen.generate(_office_payload(office_id="office-1"))

        internet_call = gen.client.create.call_args_list[1]
        assert internet_call.kwargs["data"]["locations"] == [{"id": "office-1"}, {"id": "zone-1"}]
        overlay_call = gen.client.create.call_args_list[2]
        assert overlay_call.kwargs["data"]["locations"] == [{"id": "office-1"}, {"id": "zone-1"}]

    @pytest.mark.asyncio
    async def test_circuits_use_resolved_provider_id_not_org_id(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        internet_call = gen.client.create.call_args_list[1]
        assert internet_call.kwargs["data"]["provider"] == {"id": "provider-1"}
        overlay_call = gen.client.create.call_args_list[2]
        assert overlay_call.kwargs["data"]["provider"] == {"id": "provider-1"}

    @pytest.mark.asyncio
    async def test_overlay_circuit_allocates_tunnel_id_from_pool(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        overlay_call = gen.client.create.call_args_list[2]
        assert overlay_call.kwargs["data"]["tunnel_id"] == {
            "from_pool": {"id": "pool-1"},
            "identifier": "C002-P-sdwan-tunnel_id",
        }

    @pytest.mark.asyncio
    async def test_overlay_circuit_linked_to_gateway_subinterface(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        self._overlay_circuit.interface_capabilities.add.assert_called_once_with({"id": "subiface-1"})
        self._overlay_circuit.save.assert_awaited()

    @pytest.mark.asyncio
    async def test_edge_attached_to_orchestrator_managed_devices(self) -> None:
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        self._orchestrator.managed_devices.add.assert_called_once_with({"id": "edge-1"})
        # Never tracked: the orchestrator is hand-authored, and a rerun that
        # finds the edge attached skips the save, so tracking it would get it
        # deleted by delete_unused_nodes.
        self._orchestrator.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    @pytest.mark.asyncio
    async def test_already_linked_gateway_subinterface_is_not_re_added(self) -> None:
        gen = self._wire_full_success()
        self._overlay_circuit.interface_capabilities.peers = [MagicMock(id="subiface-1")]

        await gen.generate(_office_payload())

        self._overlay_circuit.interface_capabilities.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_edge_already_managed_is_not_re_added(self) -> None:
        gen = self._wire_full_success()
        self._orchestrator.managed_devices.peers = [MagicMock(id="edge-1")]

        await gen.generate(_office_payload())

        self._orchestrator.managed_devices.add.assert_not_called()
        self._orchestrator.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_orchestrator_manages_gateway_logs_warning_but_does_not_fail(self) -> None:
        gen = self._wire_full_success()
        provider = MagicMock(id="provider-1")
        edge_template = MagicMock(id="template-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        gen.client.filters = AsyncMock(
            side_effect=[[provider], [], [edge_template], [edge_uplink], [self._overlay_circuit], []]
        )

        await gen.generate(_office_payload())

        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()


class TestGenerateFailurePaths:
    @pytest.mark.asyncio
    async def test_no_edge_uplink_interface_stops_before_circuits(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock(return_value=_node("edge-1"))
        provider = MagicMock(id="provider-1")
        edge_template = MagicMock(id="template-1")
        # Call order: provider lookup, existing-edge lookup (none), template
        # lookup (found), edge uplink lookup (none) — stops here.
        gen.client.filters = AsyncMock(side_effect=[[provider], [], [edge_template], []])

        await gen.generate(_office_payload())

        gen.logger.error.assert_called_once()
        assert gen.client.create.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_edge_template_stops_before_uplink_lookup(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()
        provider = MagicMock(id="provider-1")
        # provider found, no existing edge, no template found
        gen.client.filters = AsyncMock(side_effect=[[provider], [], []])

        await gen.generate(_office_payload())

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_tunnel_id_pool_stops_before_subinterface(self) -> None:
        gen = _make_gen()
        edge = _node("edge-1")
        internet_circuit = _node("inet-1")
        gen.client.create = AsyncMock(side_effect=[edge, internet_circuit])
        provider = MagicMock(id="provider-1")
        edge_template = MagicMock(id="template-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        gen.client.filters = AsyncMock(side_effect=[[provider], [], [edge_template], [edge_uplink]])
        gen.client.get = AsyncMock(side_effect=Exception("pool not found"))

        await gen.generate(_office_payload())

        assert gen.client.create.await_count == 2
        gen.logger.error.assert_called()
