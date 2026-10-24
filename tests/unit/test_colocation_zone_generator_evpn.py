"""The EVPN Multi-Site objects and the metro ASN pool of ColocationFabricGenerator.

The generator lives in generators/topology/colocation_fabric.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreNumberPool

from generators.logger import GeneratorError
from tests.unit.colocation_zone_generator_helpers import _created, _made_virtual, _made_zone, _zone


def _loopback(device: str, role: str, *, addressed: bool = True, iface_id: str | None = None) -> MagicMock:
    loopback = MagicMock()
    loopback.id = iface_id or f"{device}-{role}"
    loopback.role.value = role
    loopback.device.peer.name.value = device
    loopback.device.peer.id = f"dev-{device}"
    loopback.ip_address.initialized = addressed
    loopback.ip_address.id = f"ip-{device}-{role}"
    return loopback


def _evpn_generator(*, existing_as: bool = True, loopbacks: list | None = None, virtual: bool = False) -> Any:
    """A generator whose EVPN step runs for real; only the SDK edges are mocked.

    client.filters is called twice: the shared-AS lookup by description, then the edges' loopbacks."""
    if virtual:
        gen = _made_virtual()
        gen._edge_devices = {"eg-am-ne01": "cisco_iosxe"}
        edge = "eg-am-ne01"
    else:
        gen = _made_zone()
        gen._edge_devices = {"eg-fr201": "cisco_nxos"}
        edge = "eg-fr201"
    gen._vtep_pool = MagicMock(id="vtep-pool")
    site_as = MagicMock(id="as-site")
    site_as.save = AsyncMock()
    metro_node = MagicMock()
    metro_node.save = AsyncMock()

    gen.client.get = AsyncMock(return_value=metro_node)
    gen.client.filters = AsyncMock(
        side_effect=[
            [site_as] if existing_as else [],
            [_loopback(edge, "loopback")] if loopbacks is None else loopbacks,
        ]
    )
    created = MagicMock()
    created.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created)
    gen.client.allocate_next_ip_address = AsyncMock(return_value=MagicMock(id="ip-vtep-new"))
    gen.client.group_context.related_node_ids = []
    gen._metro_node = metro_node
    return gen


class TestEvpn:
    """_ensure_evpn: the metro's shared site AS, VTEP loopback and overlay BGP per edge."""

    @pytest.mark.asyncio
    async def test_no_edges_is_a_no_op(self) -> None:
        """A zone without edges makes no EVPN objects and touches nothing."""
        gen = _evpn_generator()
        gen._edge_devices = {}

        assert await gen._ensure_evpn() == {}

        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_edge_attribute_at_all_is_a_no_op(self) -> None:
        """A zone whose device step never ran (no kit) has no _edge_devices: nothing to do."""
        gen = _made_zone()

        assert await gen._ensure_evpn() == {}

    @pytest.mark.asyncio
    async def test_existing_site_as_is_reused_by_metro_description_and_attached_to_the_metro(self) -> None:
        """The AS is found by '<metro> EVPN site ASN', never re-drawn; evpn_rt_as lands on the METRO, not the zone."""
        gen = _evpn_generator()

        await gen._ensure_evpn()

        assert gen.client.filters.await_args_list[0].kwargs["description__value"] == "fr EVPN site ASN"
        assert _created(gen, "RoutingAutonomousSystem") == []
        gen.client.get.assert_awaited_once_with(kind="TopologySegmentHosting", id="metro-1")
        assert gen._metro_node.evpn_rt_as == {"id": "as-site"}
        gen._metro_node.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_site_as_already_on_the_metro_costs_no_get_and_no_save(self) -> None:
        """The query read the metro's evpn_rt_as: a re-run finds it in place and does not fetch or save the metro."""
        gen = _evpn_generator()
        gen.data["parent"]["evpn_rt_as"] = {"id": "as-site"}

        await gen._ensure_evpn()

        gen.client.get.assert_not_awaited()
        gen._metro_node.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_different_site_as_on_the_metro_is_replaced(self) -> None:
        """The metro points at another AS than the shared one: it is fetched and re-pointed."""
        gen = _evpn_generator()
        gen.data["parent"]["evpn_rt_as"] = {"id": "as-old"}

        await gen._ensure_evpn()

        assert gen._metro_node.evpn_rt_as == {"id": "as-site"}
        gen._metro_node.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_site_as_is_drawn_under_a_lock_keyed_by_the_metro_description(self) -> None:
        """Zones of one metro running concurrently must not draw two ASNs: the lock key carries the metro's name."""
        gen = _evpn_generator()

        await gen._ensure_evpn()

        gen.acquire_resource_lock.assert_awaited_once_with("shared-as-fr EVPN site ASN")
        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_site_as_is_never_tracked_by_a_zone_run(self) -> None:
        """Every zone and virtual colocation of the metro finds the same AS, so none of their tracking groups may
        own it: a zone that stopped declaring edges would otherwise delete an AS its sibling's overlay BGP uses."""
        for existing_as in (True, False):
            gen = _evpn_generator(existing_as=existing_as)

            await gen._ensure_evpn()

            assert gen.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_new_site_as_is_drawn_from_the_metro_asn_pool(self) -> None:
        gen = _evpn_generator(existing_as=False)

        await gen._ensure_evpn()

        (as_data,) = _created(gen, "RoutingAutonomousSystem")
        assert as_data["asn"] == {"from_pool": {"id": "asn-pool"}}
        assert as_data["description"] == "fr EVPN site ASN"

    @pytest.mark.asyncio
    async def test_two_zones_of_one_metro_share_the_same_as_description(self) -> None:
        """FR2 and FR6 both look up the same description, which is why the metro has exactly one site AS."""
        first = _evpn_generator()
        second = _evpn_generator()
        second.data = _zone(name="FR6", id="zone-6")
        second.fabric_name = "fr6"

        await first._ensure_evpn()
        await second._ensure_evpn()

        descriptions = {g.client.filters.await_args_list[0].kwargs["description__value"] for g in (first, second)}
        assert descriptions == {"fr EVPN site ASN"}
        assert second._metro_node.evpn_rt_as == {"id": "as-site"}

    @pytest.mark.asyncio
    async def test_new_vtep_is_allocated_and_overlay_bgp_created(self) -> None:
        gen = _evpn_generator()

        await gen._ensure_evpn()

        assert gen.client.allocate_next_ip_address.await_args_list[-1].kwargs["identifier"] == "eg-fr201-vtep"
        assert gen.client.allocate_next_ip_address.await_args_list[-1].kwargs["prefix_length"] == 128
        assert gen.client.allocate_next_ip_address.await_args_list[-1].kwargs["resource_pool"].id == "vtep-pool"
        (vtep,) = _created(gen, "DcimVirtualInterface")
        assert vtep["role"] == "loopback-vtep"
        assert vtep["name"] == "loopback1"
        assert "id" not in vtep
        (bgp,) = _created(gen, "ManagedBGP")
        assert bgp["name"] == "eg-fr201-bgp-overlay"
        assert bgp["local_as"] == {"id": "as-site"}
        assert bgp["router_id"] == {"id": "ip-eg-fr201-loopback"}
        assert bgp["process_role"] == "overlay"
        assert bgp["capabilities"] == [{"id": "dev-eg-fr201"}]

    @pytest.mark.asyncio
    async def test_existing_vtep_is_reused_idempotently(self) -> None:
        """A re-run keeps the VTEP's id and address: no new allocation."""
        loopbacks = [_loopback("eg-fr201", "loopback"), _loopback("eg-fr201", "loopback-vtep", iface_id="vtep-1")]
        gen = _evpn_generator(loopbacks=loopbacks)

        await gen._ensure_evpn()

        gen.client.allocate_next_ip_address.assert_not_awaited()
        (vtep,) = _created(gen, "DcimVirtualInterface")
        assert vtep["id"] == "vtep-1"
        assert vtep["ip_address"] == {"id": "ip-eg-fr201-loopback-vtep"}

    @pytest.mark.asyncio
    async def test_edge_without_addressed_loopback_is_skipped(self) -> None:
        gen = _evpn_generator(loopbacks=[_loopback("eg-fr201", "loopback", addressed=False)])

        result = await gen._ensure_evpn()

        gen.logger.error.assert_called_once()
        assert _created(gen, "ManagedBGP") == []
        assert result == {}

    @pytest.mark.asyncio
    async def test_overlay_process_ids_are_returned_by_edge_name(self) -> None:
        gen = _evpn_generator()
        gen.client.create.return_value.id = "bgp-saved"

        assert await gen._ensure_evpn() == {"eg-fr201": "bgp-saved"}

    @pytest.mark.asyncio
    async def test_virtual_colocation_does_evpn_for_its_metro(self) -> None:
        """A virtual edge is a border gateway too: same shared AS description, attached to ITS metro (AM)."""
        gen = _evpn_generator(virtual=True)

        result = await gen._ensure_evpn()

        assert gen.client.filters.await_args_list[0].kwargs["description__value"] == "am EVPN site ASN"
        gen.client.get.assert_awaited_once_with(kind="TopologySegmentHosting", id="metro-am")
        assert gen._metro_node.evpn_rt_as == {"id": "as-site"}
        (bgp,) = _created(gen, "ManagedBGP")
        assert bgp["name"] == "eg-am-ne01-bgp-overlay"
        assert result == {"eg-am-ne01": gen.client.create.return_value.id}


class TestMetroAsnPool:
    """_metro_asn_pool_id: the metro's ASN pool, waiting for add_colocation_metro when it is not visible yet."""

    @pytest.mark.asyncio
    async def test_pool_in_the_query_result_is_used_without_waiting(self) -> None:
        gen = _made_zone()
        gen.wait_for_parent_generators = AsyncMock()
        gen._get_parent_pool_with_retry = AsyncMock()

        assert await gen._metro_asn_pool_id() == "asn-pool"

        gen.wait_for_parent_generators.assert_not_awaited()
        gen._get_parent_pool_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", [None, {}])
    async def test_missing_pool_waits_for_the_metro_generator_then_looks_it_up_by_name(
        self, missing: dict[str, Any] | None
    ) -> None:
        """A zone's created-trigger can fire before add_colocation_metro attached the pool."""
        gen = _made_zone()
        gen.data["parent"]["fabric_asn_pool"] = missing
        gen.wait_for_parent_generators = AsyncMock(return_value=True)
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="late-pool"))

        assert await gen._metro_asn_pool_id() == "late-pool"

        gen.wait_for_parent_generators.assert_awaited_once_with(("add_colocation_metro",), "metro-1")
        gen._get_parent_pool_with_retry.assert_awaited_once_with("fr-asn-pool", kind=CoreNumberPool)

    @pytest.mark.asyncio
    async def test_pool_that_never_appears_fails_the_run(self) -> None:
        gen = _made_zone()
        gen.data["parent"]["fabric_asn_pool"] = None
        gen.wait_for_parent_generators = AsyncMock(return_value=False)
        gen._get_parent_pool_with_retry = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="CoreNumberPool", identifier={})
        )

        with pytest.raises(GeneratorError, match="fr-asn-pool"):
            await gen._metro_asn_pool_id()

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_evpn_waits_then_draws_from_the_late_pool(self) -> None:
        """End to end through _ensure_evpn: the AS is drawn from the pool found after the wait."""
        gen = _evpn_generator(existing_as=False)
        gen.data["parent"]["fabric_asn_pool"] = None
        gen.wait_for_parent_generators = AsyncMock(return_value=True)
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="late-pool"))

        await gen._ensure_evpn()

        (as_data,) = _created(gen, "RoutingAutonomousSystem")
        assert as_data["asn"] == {"from_pool": {"id": "late-pool"}}
