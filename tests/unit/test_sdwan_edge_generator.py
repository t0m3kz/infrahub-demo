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

from generators.logger import GeneratorError
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


def _providers(internet_id: str = "provider-inet", overlay_id: str = "provider-overlay") -> list[Any]:
    """The OrganizationProvider lookup's answer: the ISP (P019) and the SD-WAN operator (P035)."""
    internet = MagicMock(id=internet_id)
    internet.org_id.value = "P019"
    overlay = MagicMock(id=overlay_id)
    overlay.org_id.value = "P035"
    return [internet, overlay]


def _node(node_id: str, **extra: Any) -> Any:
    node = MagicMock(id=node_id)
    node.save = AsyncMock()
    for key, value in extra.items():
        setattr(node, key, value)
    return node


class TestGenerateGuardClauses:
    """A missing prerequisite fails the run (GeneratorError) before anything is created."""

    @pytest.mark.asyncio
    async def test_no_matching_kind_in_response_is_noop(self) -> None:
        """A response without TopologyCustomerOffice is not this generator's: no-op, no error."""
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate({"TopologyCustomerDC": []})

        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_sdwan_gateway_is_a_noop(self) -> None:
        """An office that did not opt in is a green no-op."""
        gen = _make_gen()
        gen.client.create = AsyncMock()

        await gen.generate(_office_payload(gateway=None))

        gen.client.create.assert_not_called()
        gen.logger.error.assert_not_called()

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(_office_payload(office_id=None), "missing id", id="no-office-id"),
            pytest.param(_office_payload(owner_id=None), "no owner", id="no-owner"),
            pytest.param(_office_payload(gateway=_gateway(zone_name=None)), "no deployment", id="no-gateway-zone"),
            pytest.param(_office_payload(gateway=_gateway(trunk_role=None)), "no role=uplink trunk", id="no-trunk"),
        ],
    )
    @pytest.mark.asyncio
    async def test_missing_prerequisite_raises_and_creates_nothing(self, payload: dict[str, Any], message: str) -> None:
        """Each guard logs the error and raises, so the task fails instead of going green."""
        gen = _make_gen()
        gen.client.create = AsyncMock()

        with pytest.raises(GeneratorError, match=message):
            await gen.generate(payload)

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
        iface_capabilities.peers = []
        overlay_circuit.interface_capabilities = iface_capabilities
        gateway_subiface = _node("subiface-1")
        # The server-side re-read of the overlay is a different node object from the upserted one.
        refreshed_overlay = _node("vc-1", interface_capabilities=MagicMock(peers=[]))

        gen.client.create = AsyncMock(side_effect=[edge, internet_circuit, overlay_circuit, gateway_subiface])

        edge_template = MagicMock(id="template-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        orchestrator = MagicMock()
        orchestrator.name.value = "EQX-FR2-SDWAN-VCO1"
        managed_devices = MagicMock()
        managed_devices.peers = []
        orchestrator.managed_devices = managed_devices
        orchestrator.save = AsyncMock()
        orchestrator.add_relationships = AsyncMock()
        # Call order: (1) provider org lookup (ISP + overlay operator, one
        # query); (2) existing-edge-by-
        # name lookup -> none, so the edge is new and the template gets
        # resolved; (3) VCE-610_EDGE template lookup; (4) edge uplink
        # interface lookup; (5) re-read the just-created overlay circuit
        # (full interface_capabilities peer list, see
        # _link_overlay_to_interfaces's docstring); (6) orchestrator
        # lookup (managed_devices included).
        gen.client.filters = AsyncMock(
            side_effect=[_providers(), [], [edge_template], [edge_uplink], [refreshed_overlay], [orchestrator]]
        )

        pool = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(return_value=pool)

        self._edge = edge
        self._internet_circuit = internet_circuit
        self._overlay_circuit = overlay_circuit
        self._refreshed_overlay = refreshed_overlay
        self._gateway_subiface = gateway_subiface
        self._orchestrator = orchestrator
        return gen

    @pytest.mark.asyncio
    async def test_full_flow_creates_every_object_in_order(self) -> None:
        """Edge, underlay, overlay, then the gateway sub-interface whose name needs the tunnel id."""
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
    async def test_circuits_join_their_generator_target_groups(self) -> None:
        """The created circuits join physical_circuits/virtual_circuits, so
        add_circuit/add_virtual_circuit run for them like for data circuits."""
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        circuit_calls = gen.client.create.call_args_list[1:3]
        assert [call.kwargs["data"]["member_of_groups"] for call in circuit_calls] == [
            [{"hfid": ["physical_circuits"], "kind": "CoreGeneratorGroup"}],
            [{"hfid": ["virtual_circuits"], "kind": "CoreGeneratorGroup"}],
        ]

    @pytest.mark.asyncio
    async def test_new_edge_sends_object_template(self) -> None:
        """A new edge is created from its template, without an id."""
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
        iface_capabilities.peers = []
        overlay_circuit.interface_capabilities = iface_capabilities
        gateway_subiface = _node("subiface-1")
        gen.client.create = AsyncMock(side_effect=[edge, internet_circuit, overlay_circuit, gateway_subiface])

        existing_edge = MagicMock(id="edge-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        orchestrator = MagicMock()
        managed_devices = MagicMock()
        managed_devices.peers = []
        orchestrator.managed_devices = managed_devices
        orchestrator.save = AsyncMock()
        orchestrator.add_relationships = AsyncMock()
        # Existing-edge lookup now returns a match — no template lookup follows.
        # The 4th entry is the post-create re-fetch of the overlay circuit
        # (real __typename for interface_capabilities peers, see
        # _link_overlay_to_interfaces's docstring) — reusing the
        # same overlay_circuit mock, since only its .interface_capabilities
        # matters here, not a distinct identity.
        gen.client.filters = AsyncMock(
            side_effect=[_providers(), [existing_edge], [edge_uplink], [overlay_circuit], [orchestrator]]
        )
        gen.client.get = AsyncMock(return_value=MagicMock(id="pool-1"))

        await gen.generate(_office_payload())

        edge_call = gen.client.create.call_args_list[0]
        assert edge_call.kwargs["data"]["id"] == "edge-1"
        assert "object_template" not in edge_call.kwargs["data"]

    @pytest.mark.asyncio
    async def test_gateway_subinterface_named_from_resolved_tunnel_id(self) -> None:
        """The gateway sub-interface is <trunk>.<pooled tunnel id>."""
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
    async def test_underlay_is_bought_from_the_isp_and_overlay_from_the_sdwan_operator(self) -> None:
        """The internet circuit's provider is the ISP; the tunnel's is the SD-WAN operator, never the ISP."""
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        assert gen.client.filters.call_args_list[0].kwargs == {
            "kind": "OrganizationProvider",
            "org_id__values": ["P019", "P035"],
        }
        internet_call = gen.client.create.call_args_list[1]
        assert internet_call.kwargs["data"]["provider"] == {"id": "provider-inet"}
        overlay_call = gen.client.create.call_args_list[2]
        assert overlay_call.kwargs["data"]["provider"] == {"id": "provider-overlay"}

    @pytest.mark.asyncio
    async def test_rerun_sends_the_same_payloads(self) -> None:
        """Idempotent: a second run upserts the same names, HFIDs and pool identifier (no new tunnel id key)."""
        first = self._wire_full_success()
        await first.generate(_office_payload())
        second = self._wire_full_success()
        await second.generate(_office_payload())

        def payloads(gen: SdwanEdgeGenerator) -> list[dict[str, Any]]:
            return [call.kwargs["data"] for call in gen.client.create.call_args_list]

        assert payloads(first) == payloads(second)
        assert [p.get("name") for p in payloads(first)] == [
            "C002-P-EDGE1",
            "INET-C002-P",
            "C002-P-SDWAN-FR2",
            "eth0.2000",
        ]

    @pytest.mark.asyncio
    async def test_overlay_circuit_allocates_tunnel_id_from_pool(self) -> None:
        """The tunnel id comes from GLOBAL-SDWAN-TUNNEL-ID, keyed per office."""
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        overlay_call = gen.client.create.call_args_list[2]
        assert overlay_call.kwargs["data"]["tunnel_id"] == {
            "from_pool": {"id": "pool-1"},
            "identifier": "C002-P-sdwan-tunnel_id",
        }

    @pytest.mark.asyncio
    async def test_first_run_links_the_overlay_to_edge_uplink_and_gateway_subinterface(self) -> None:
        """The overlay terminates on both ends, added in one save once the sub-interface exists."""
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        caps = self._refreshed_overlay.interface_capabilities
        assert [c.args[0] for c in caps.add.call_args_list] == [{"id": "edge-uplink-1"}, {"id": "subiface-1"}]
        caps.remove.assert_not_called()
        self._refreshed_overlay.save.assert_awaited()

    @pytest.mark.asyncio
    async def test_overlay_upsert_never_carries_interface_capabilities(self) -> None:
        """An upsert replaces a many-relationship it carries: sending the edge side alone would
        drop the gateway link on every rerun. The link step owns the whole list."""
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        overlay_data = gen.client.create.call_args_list[2].kwargs["data"]
        assert "interface_capabilities" not in overlay_data

    @pytest.mark.asyncio
    async def test_rerun_over_unchanged_data_removes_and_adds_nothing(self) -> None:
        """Rerun: both ends already linked, so the overlay is neither modified nor saved again."""
        gen = self._wire_full_success()
        caps = self._refreshed_overlay.interface_capabilities
        caps.peers = [MagicMock(id="edge-uplink-1"), MagicMock(id="subiface-1")]

        await gen.generate(_office_payload())

        caps.add.assert_not_called()
        caps.remove.assert_not_called()
        self._refreshed_overlay.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_the_missing_end_is_added(self) -> None:
        """Gateway side present, edge side missing: just the edge uplink is added, nothing removed."""
        gen = self._wire_full_success()
        caps = self._refreshed_overlay.interface_capabilities
        caps.peers = [MagicMock(id="subiface-1")]

        await gen.generate(_office_payload())

        caps.add.assert_called_once_with({"id": "edge-uplink-1"})
        caps.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_end_is_removed(self) -> None:
        """A replaced edge uplink leaves its old interface behind: the link step prunes it."""
        gen = self._wire_full_success()
        caps = self._refreshed_overlay.interface_capabilities
        caps.peers = [MagicMock(id="old-uplink"), MagicMock(id="edge-uplink-1"), MagicMock(id="subiface-1")]

        await gen.generate(_office_payload())

        caps.remove.assert_called_once_with("old-uplink")
        caps.add.assert_not_called()
        self._refreshed_overlay.save.assert_awaited()

    @pytest.mark.asyncio
    async def test_edge_attached_to_orchestrator_managed_devices(self) -> None:
        """The new edge joins the gateway's orchestrator by a RelationshipAdd of that one edge.

        Never a save of the whole peer list: the offices' runs attach to the same
        orchestrator concurrently and would overwrite each other's edge, and a
        tracked save would claim the hand-authored orchestrator for this run's group.
        """
        gen = self._wire_full_success()

        await gen.generate(_office_payload())

        self._orchestrator.add_relationships.assert_awaited_once_with(
            relation_to_update="managed_devices", related_nodes=["edge-1"]
        )
        self._orchestrator.managed_devices.add.assert_not_called()
        self._orchestrator.save.assert_not_awaited()
        # The peer ids come with the lookup (include=), not from a per-relationship fetch.
        assert gen.client.filters.await_args_list[-1].kwargs["include"] == ["managed_devices"]

    @pytest.mark.asyncio
    async def test_edge_already_managed_is_not_re_added(self) -> None:
        """Rerun: an edge the orchestrator already manages is not re-added or saved."""
        gen = self._wire_full_success()
        self._orchestrator.managed_devices.peers = [MagicMock(id="edge-1")]

        await gen.generate(_office_payload())

        self._orchestrator.add_relationships.assert_not_awaited()
        self._orchestrator.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_orchestrator_manages_gateway_logs_warning_but_does_not_fail(self) -> None:
        """A gateway without an orchestrator is a warning: the edge is still provisioned."""
        gen = self._wire_full_success()
        edge_template = MagicMock(id="template-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        gen.client.filters = AsyncMock(
            side_effect=[_providers(), [], [edge_template], [edge_uplink], [self._refreshed_overlay], []]
        )

        await gen.generate(_office_payload())

        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()


class TestGenerateFailurePaths:
    """Every failure fails the run: no swallowed exception, no half-provisioned green task."""

    @pytest.mark.asyncio
    async def test_missing_provider_org_raises_before_any_create(self) -> None:
        """Without the SD-WAN operator's provider org there is no overlay provider: fail, create nothing."""
        gen = _make_gen()
        gen.client.create = AsyncMock()
        gen.client.filters = AsyncMock(side_effect=[_providers()[:1]])

        with pytest.raises(GeneratorError, match="P035"):
            await gen.generate(_office_payload())

        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_edge_uplink_interface_raises_before_circuits(self) -> None:
        """An edge without a role=uplink port has nothing to ride the underlay: fail after the edge only."""
        gen = _make_gen()
        gen.client.create = AsyncMock(return_value=_node("edge-1"))
        edge_template = MagicMock(id="template-1")
        # Call order: provider lookup, existing-edge lookup (none), template
        # lookup (found), edge uplink lookup (none) — stops here.
        gen.client.filters = AsyncMock(side_effect=[_providers(), [], [edge_template], []])

        with pytest.raises(GeneratorError, match="no role=uplink interface"):
            await gen.generate(_office_payload())

        assert gen.client.create.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_edge_template_raises_before_uplink_lookup(self) -> None:
        """A new edge needs its template: fail before creating anything."""
        gen = _make_gen()
        gen.client.create = AsyncMock()
        # provider found, no existing edge, no template found
        gen.client.filters = AsyncMock(side_effect=[_providers(), [], []])

        with pytest.raises(GeneratorError, match="VCE-610_EDGE"):
            await gen.generate(_office_payload())

        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_sdk_error_propagates_instead_of_a_green_run(self) -> None:
        """An SDK failure (here the tunnel-id pool lookup) is not caught and turned into a silent return."""
        gen = _make_gen()
        edge = _node("edge-1")
        internet_circuit = _node("inet-1")
        gen.client.create = AsyncMock(side_effect=[edge, internet_circuit])
        edge_template = MagicMock(id="template-1")
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        gen.client.filters = AsyncMock(side_effect=[_providers(), [], [edge_template], [edge_uplink]])
        gen.client.get = AsyncMock(side_effect=RuntimeError("pool not found"))

        with pytest.raises(RuntimeError, match="pool not found"):
            await gen.generate(_office_payload())

        assert gen.client.create.await_count == 2

    @pytest.mark.asyncio
    async def test_failed_save_propagates(self) -> None:
        """A save the server rejects fails the run rather than being logged and skipped."""
        gen = _make_gen()
        edge = _node("edge-1")
        edge.save = AsyncMock(side_effect=RuntimeError("server rejected the edge"))
        gen.client.create = AsyncMock(return_value=edge)
        gen.client.filters = AsyncMock(side_effect=[_providers(), [], [MagicMock(id="template-1")]])

        with pytest.raises(RuntimeError, match="server rejected the edge"):
            await gen.generate(_office_payload())

    @pytest.mark.asyncio
    async def test_unresolved_tunnel_id_raises_before_the_gateway_subinterface(self) -> None:
        """No tunnel id read back means no sub-interface name: fail instead of skipping the gateway side."""
        wiring = TestGenerateHappyPath()
        gen = wiring._wire_full_success()
        wiring._overlay_circuit.tunnel_id = MagicMock(value=None)

        with pytest.raises(GeneratorError, match="no resolved tunnel_id"):
            await gen.generate(_office_payload())

        assert gen.client.create.await_count == 3

    @pytest.mark.asyncio
    async def test_overlay_vanished_before_linking_raises(self) -> None:
        """The overlay circuit's re-fetch finding nothing is a failure, not a skipped link."""
        gen = _make_gen()
        overlay_circuit = _node("vc-1")
        overlay_circuit.tunnel_id = MagicMock(value=2000)
        gen.client.create = AsyncMock(side_effect=[_node("edge-1"), _node("inet-1"), overlay_circuit, _node("sub-1")])
        edge_uplink = MagicMock(id="edge-uplink-1")
        edge_uplink.name.value = "GE1"
        gen.client.filters = AsyncMock(side_effect=[_providers(), [], [MagicMock(id="template-1")], [edge_uplink], []])
        gen.client.get = AsyncMock(return_value=MagicMock(id="pool-1"))

        with pytest.raises(GeneratorError, match="vanished"):
            await gen.generate(_office_payload())
