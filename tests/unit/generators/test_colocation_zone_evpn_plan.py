"""The EVPN Multi-Site border gateways of a colocation zone or virtual colocation and the edge pair of a zone
(generators/colocation/colocation_evpn.py, applied by colocation_fabric.py): the site AS the zones of a metro share, a VTEP
loopback and an overlay process per edge, and for exactly two physical edges the interlink and the iBGP FABRIC session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.colocation.colocation_fabric import VirtualColocationGenerator
from generators.model.errors import GeneratorError
from tests.unit.colocation_zone_generator_helpers import (
    _PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
    ColoClient,
    _virtual,
    _zone,
    seed_nodes,
    zone_env,
)
from tests.unit.generators.test_colocation_zone_plan import (
    _BALANCERS,
    _EDGES,
    _FIREWALLS,
    names_of,
    of_kind,
    run,
    snapshot,
)

_SITE_AS = "fr EVPN site ASN"


def _site_as(client: ColoClient) -> list[dict[str, Any]]:
    return [node.data for node in client.store.values() if node.data.get("description") == _SITE_AS]


def _processes(client: ColoClient) -> dict[str, dict[str, Any]]:
    return of_kind(client, "ManagedBGP")


@pytest.mark.asyncio
class TestEvpn:
    async def test_no_edges_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = await run(monkeypatch, _FIREWALLS)

        assert not _site_as(client) and not _processes(client)
        assert not [
            n for n in client.store.values() if n.kind == "DcimVirtualInterface" and n.data["role"] == "loopback-vtep"
        ]
        assert "evpn_rt_as" not in client.store["metro-1"].data
        gen.wait_for_parent_generators.assert_not_awaited()

    async def test_the_site_as_is_drawn_from_the_metro_asn_pool_and_never_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        ((as_id, node),) = [(n.id, n) for n in client.store.values() if n.data.get("description") == _SITE_AS]
        assert node.data["asn"] == {"from_pool": {"id": "asn-pool"}}
        (kwargs,) = [kwargs for kind, node_id, _, kwargs in client.saves if node_id == as_id]
        assert kwargs == {"allow_upsert": True, "update_group_context": False}
        assert as_id not in client.group_context.related_node_ids

    async def test_an_existing_site_as_is_reused_by_the_metro_description_and_attached_to_the_metro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = ColoClient()
        existing = client.seed("RoutingAutonomousSystem", description=_SITE_AS, asn=64999)

        _, client = await run(monkeypatch, _EDGES, client=client)

        assert len(_site_as(client)) == 1
        assert client.store["metro-1"].data["evpn_rt_as"] == existing.id
        assert {process["local_as"]["id"] for process in _processes(client).values()} == {existing.id}

    async def test_a_site_as_already_on_the_metro_costs_no_save(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = ColoClient()
        existing = client.seed("RoutingAutonomousSystem", description=_SITE_AS, asn=64999)

        _, client = await run(
            monkeypatch,
            _EDGES,
            client=client,
            parent={
                "id": "metro-1",
                "name": "FR",
                "naming_convention": "standard",
                "fabric_asn_pool": {"id": "asn-pool"},
                # the query's `evpn_rt_as { node { id } }` is cleaned to the bare id
                "evpn_rt_as": existing.id,
            },
        )

        assert [node_id for _, node_id, _, _ in client.saves if node_id == "metro-1"] == []

    async def test_a_different_site_as_on_the_metro_is_replaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = ColoClient()
        existing = client.seed("RoutingAutonomousSystem", description=_SITE_AS, asn=64999)

        _, client = await run(
            monkeypatch,
            _EDGES,
            client=client,
            parent={
                "id": "metro-1",
                "name": "FR",
                "naming_convention": "standard",
                "fabric_asn_pool": {"id": "asn-pool"},
                "evpn_rt_as": "as-stale",
            },
        )

        assert client.store["metro-1"].data["evpn_rt_as"] == existing.id

    async def test_the_site_as_is_drawn_under_a_lock_keyed_by_the_metro_description(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, _ = await run(monkeypatch, _EDGES)

        assert f"shared-as-{_SITE_AS}" in gen.locked_keys

    async def test_two_zones_of_one_metro_share_the_same_as(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES)
        second = {
            "id": "zone-2",
            "name": "FR3",
            "parent": _zone()["parent"],
            "fabric_templates": [_EDGES],
        }
        gen, _ = zone_env(monkeypatch, client=client)
        seed_nodes(client, second)

        await gen.generate({"TopologyColocationZone": [second]})

        assert len(_site_as(client)) == 1
        assert len({process["local_as"]["id"] for process in _processes(client).values()}) == 1
        assert sorted(_processes(client)) == [f"eg-fr{zone}0{n}-bgp-overlay" for zone in (2, 3) for n in (1, 2)]

    async def test_every_edge_gets_an_ipv6_vtep_loopback_and_an_overlay_process_on_its_router_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        names = {node_id: node.data.get("name") for node_id, node in client.store.items()}
        vteps = {
            names[node.data["device"]["id"]]: node.data
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and node.data["role"] == "loopback-vtep"
        }
        assert sorted(str(edge) for edge in vteps) == ["eg-fr201", "eg-fr202"]
        assert {data["name"] for data in vteps.values()} == {"loopback1"}
        assert {data["description"] for data in vteps.values()} == {"EVPN VTEP source (IPv6)"}
        for edge, data in vteps.items():
            address = client.store[data["ip_address"]].data
            assert (
                address["identifier"] == f"{edge}-vtep" and address["description"] == f"EVPN VTEP loopback for {edge}"
            )
        loopbacks = {
            names[node.data["device"]["id"]]: node.data["ip_address"]
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and node.data["role"] == "loopback"
        }
        for edge, process in _processes(client).items():
            device = edge.removesuffix("-bgp-overlay")
            assert process["process_role"] == "overlay" and process["router_id"] == {"id": loopbacks[device]}
            assert process["capabilities"] == [{"id": next(i for i, n in names.items() if n == device)}]
            assert process["description"] == f"EVPN Multi-Site border-gateway process for {device}"

    async def test_the_vtep_address_comes_from_the_zones_ipv6_loopback_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        pools = {
            client.store[key.split("/")[0]].data["name"]
            for key in client.address_pool_allocations
            if key.endswith("-vtep")
        }
        assert pools == {"fr2-loopback-ipv6-pool"}

    async def test_a_rerun_keeps_the_vtep_address_the_site_as_and_the_processes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)
        first = snapshot(client)

        await run(monkeypatch, _EDGES, client=client)

        assert snapshot(client) == first
        assert len(_site_as(client)) == 1


@pytest.mark.asyncio
class TestEdgePair:
    @pytest.mark.parametrize("quantity", [1, 3])
    async def test_anything_but_two_physical_edges_gets_no_pair(
        self, monkeypatch: pytest.MonkeyPatch, quantity: int
    ) -> None:
        gen, client = await run(monkeypatch, {**_EDGES, "quantity": quantity})

        assert names_of(client, "ManagedBGPPeering") == []
        assert "fr2-fabric-key" not in names_of(client, "RoutingPassword")
        assert not [name for name in names_of(client, "CoreIPPrefixPool") if name.endswith("technical-ipv6-pool")]
        assert not [name for name in names_of(client, "DcimCable") if "eg-fr201-Ethernet1/1__" in name]
        # the border gateways are still there
        assert len(_processes(client)) == quantity
        assert not [key for key in gen.locked_keys if key.startswith("edge-pair")]

    async def test_the_pair_is_cabled_on_the_interlink_role_with_a_127_from_the_zones_ipv6_technical_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        assert [
            name for name in names_of(client, "DcimCable") if "Ethernet1/1__" in name or "Ethernet1/2__" in name
        ] == [
            "eg-fr201-Ethernet1/1__eg-fr202-Ethernet1/1",
            "eg-fr201-Ethernet1/2__eg-fr202-Ethernet1/2",
        ]
        links = {
            identifier: client.store[pool_id].data["name"]
            for pool_id, identifier in client.allocations
            if identifier.startswith("DcimPhysicalInterface")
        }
        assert len(links) == 2 and set(links.values()) == {"fr2-technical-ipv6-pool"}
        link_prefixes = [
            node
            for node in client.store.values()
            if node.kind == "IpamPrefix" and str(node.prefix.value).endswith("/127")
        ]
        assert len(link_prefixes) == 2

    async def test_the_ipv6_technical_pool_is_a_zone_owned_slice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES)

        pool = of_kind(client, "CoreIPPrefixPool")["fr2-technical-ipv6-pool"]
        assert pool["default_prefix_length"] == 124

    async def test_the_session_is_an_ibgp_fabric_one_over_the_two_overlay_processes_with_the_zones_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        (peering,) = of_kind(client, "ManagedBGPPeering").values()
        assert peering["name"] == "FABRIC-eg-fr201-eg-fr202"
        assert (peering["session_type"], peering["peering_role"], peering["status"]) == ("IBGP", "fabric", "active")
        processes = _processes(client)
        assert {p["id"] for p in peering["bgp_processes"]} == {
            node.id for node in client.store.values() if node.data.get("name") in processes
        }
        families = {
            (client.store[family["id"]].data["afi"], client.store[family["id"]].data["safi"])
            for family in peering["address_families"]
        }
        assert families == {("ipv6", "unicast"), ("l2vpn", "evpn")}
        key = of_kind(client, "RoutingPassword")["fr2-fabric-key"]
        assert peering["password"] == {
            "id": next(n.id for n in client.store.values() if n.data.get("name") == "fr2-fabric-key")
        }
        assert key["password"]
        assert len(peering["capabilities"]) == 4  # both ends of both interlinks
        assert "iBGP FABRIC" in key["description"] and "Colocation zone fr2" in peering["description"]

    async def test_the_fabric_key_is_the_runs_own_and_tracked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES)

        key_id = next(n.id for n in client.store.values() if n.data.get("name") == "fr2-fabric-key")
        (kwargs,) = [kwargs for _, node_id, _, kwargs in client.saves if node_id == key_id]
        assert kwargs == {"allow_upsert": True}

    async def test_existing_address_families_are_referenced_not_recreated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = ColoClient()
        ipv6 = client.seed("RoutingBGPAddressFamily", afi="ipv6", safi="unicast")
        evpn = client.seed("RoutingBGPAddressFamily", afi="l2vpn", safi="evpn")

        _, client = await run(monkeypatch, _EDGES, client=client)

        (peering,) = of_kind(client, "ManagedBGPPeering").values()
        assert {family["id"] for family in peering["address_families"]} == {ipv6.id, evpn.id}
        assert not [kind for kind, *_ in client.saves if kind == "RoutingBGPAddressFamily"]

    async def test_a_pair_without_interlink_ports_fails_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        zone = _zone(fabric_templates=[_EDGES])
        gen, client = zone_env(monkeypatch, ports=[port for port in _PORTS if port.role != "interlink"])
        seed_nodes(client, zone)

        with pytest.raises(GeneratorError, match="cannot cable"):
            await gen.generate({"TopologyColocationZone": [zone]})

        assert client.saves == []

    async def test_two_zones_of_one_metro_get_independent_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES)
        second = {"id": "zone-2", "name": "FR3", "parent": _zone()["parent"], "fabric_templates": [_EDGES]}
        gen, _ = zone_env(monkeypatch, client=client)
        seed_nodes(client, second)

        await gen.generate({"TopologyColocationZone": [second]})

        assert names_of(client, "ManagedBGPPeering") == ["FABRIC-eg-fr201-eg-fr202", "FABRIC-eg-fr301-eg-fr302"]
        assert {"fr2-fabric-key", "fr3-fabric-key"} <= set(names_of(client, "RoutingPassword"))

    async def test_the_services_and_the_pair_share_the_edge_ports_without_taking_each_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS)

        cables = [name for name in names_of(client, "DcimCable") if "SYNC" not in name]
        ports = [end for name in cables for end in name.split("__")]
        assert len(ports) == len(set(ports)) == 2 * len(cables)


@pytest.mark.asyncio
class TestMetroAsnPool:
    async def test_the_pool_in_the_query_result_is_used_without_waiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, _ = await run(monkeypatch, _EDGES)

        gen.wait_for_parent_generators.assert_not_awaited()

    async def test_a_pool_not_attached_yet_waits_for_the_metro_generator_then_looks_it_up_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = ColoClient()
        pool = client.seed("CoreNumberPool", name="fr-asn-pool")

        gen, client = await run(
            monkeypatch,
            _EDGES,
            client=client,
            parent={"id": "metro-1", "name": "FR", "naming_convention": "standard"},
        )

        gen.wait_for_parent_generators.assert_any_await(("add_colocation_metro",), "metro-1")
        (site_as,) = _site_as(client)
        assert site_as["asn"] == {"from_pool": {"id": pool.id}}

    async def test_a_pool_that_never_appears_fails_the_run_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.colocation.colocation_fabric.asyncio.sleep", AsyncMock())

        with pytest.raises(GeneratorError, match="metro FR has no ASN pool 'fr-asn-pool'"):
            await run(monkeypatch, _EDGES, parent={"id": "metro-1", "name": "FR", "naming_convention": "standard"})

    async def test_a_zone_without_edges_never_asks_for_the_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, _ = await run(
            monkeypatch, _FIREWALLS, parent={"id": "metro-1", "name": "FR", "naming_convention": "standard"}
        )

        gen.wait_for_parent_generators.assert_not_awaited()


@pytest.mark.asyncio
class TestVirtualColocation:
    @staticmethod
    async def _run(
        monkeypatch: pytest.MonkeyPatch, *entries: dict[str, Any], **overrides: Any
    ) -> tuple[Any, ColoClient]:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        node = _virtual(fabric_templates=list(entries), **overrides)
        gen, client = zone_env(monkeypatch, cls=VirtualColocationGenerator)
        seed_nodes(client, node)
        await gen.generate({"TopologyVirtualColocation": [node]})
        return gen, client

    _KIT = (
        {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        {"role": "internal-firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        {"role": "load-balancer", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
    )

    async def test_every_role_is_an_appliance_ordered_under_the_account_and_deployed_to_the_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await self._run(monkeypatch, *self._KIT, account="acct-1")

        appliances = of_kind(client, "CloudNetworkAppliance")
        assert sorted(appliances) == [
            "eg-am-ne01",
            "eg-am-ne02",
            "fw-am-ne01",
            "fw-am-ne02",
            "lb-am-ne01",
            "lb-am-ne02",
        ]
        assert {data["account"]["id"] for data in appliances.values()} == {"acct-1"}
        assert {data["deployment"]["id"] for data in appliances.values()} == {"vc-1"}
        assert not names_of(client, "DcimPhysicalDevice") and "rack" not in next(iter(appliances.values()))

    async def test_a_presence_without_an_account_orders_its_appliances_under_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await self._run(monkeypatch, *self._KIT, account=None)

        assert {data["account"] for data in of_kind(client, "CloudNetworkAppliance").values()} == {None}

    async def test_the_edge_routers_keep_their_loopback_and_run_the_evpn_border_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await self._run(monkeypatch, *self._KIT)

        assert sorted(_processes(client)) == ["eg-am-ne01-bgp-overlay", "eg-am-ne02-bgp-overlay"]
        loopbacks = [n for n in client.store.values() if n.kind == "DcimVirtualInterface"]
        assert sorted(n.data["role"] for n in loopbacks) == ["loopback", "loopback", "loopback-vtep", "loopback-vtep"]
        assert any(n.data.get("description") == "am EVPN site ASN" for n in client.store.values())

    async def test_the_pairs_are_ha_domains_without_a_sync_port_or_a_cable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await self._run(monkeypatch, *self._KIT)

        assert names_of(client, "ManagedFirewallHA") == ["fw-am-ne01-fw-am-ne02"]
        assert names_of(client, "ManagedLoadbalancerHA") == ["lb-am-ne01-lb-am-ne02"]
        assert "group_id" in of_kind(client, "ManagedFirewallHA")["fw-am-ne01-fw-am-ne02"]
        assert names_of(client, "DcimCable") == []

    async def test_never_a_pair_a_cable_or_a_vlan_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await self._run(monkeypatch, *self._KIT)

        assert names_of(client, "ManagedBGPPeering") == []
        assert names_of(client, "DcimCable") == []
        assert not [name for name in names_of(client, "CoreNumberPool") if "context-vlan" in name]
        assert not [name for name in names_of(client, "CoreIPPrefixPool") if name.endswith("technical-ipv6-pool")]

    async def test_a_rerun_writes_the_same_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await self._run(monkeypatch, *self._KIT, account="acct-1")
        first = snapshot(client)
        node = _virtual(fabric_templates=list(self._KIT), account="acct-1")
        gen, _ = zone_env(monkeypatch, cls=VirtualColocationGenerator, client=client)
        seed_nodes(client, node)

        await gen.generate({"TopologyVirtualColocation": [node]})

        assert snapshot(client) == first
