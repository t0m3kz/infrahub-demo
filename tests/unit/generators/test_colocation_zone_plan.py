"""The plan of a colocation zone (generators/colocation/colocation_fabric.py): its pools, devices, HA domains and cabling,
built from what exists before the run and written by the reconciler on the in-memory client; and what a re-run repeats.
The EVPN border gateways and the edge pair are in test_colocation_zone_evpn_plan.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.colocation.colocation_fabric import ColocationZoneGenerator, VirtualColocationGenerator
from generators.colocation.colocation_pools import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_TECHNICAL_PREFIX_LENGTH,
)
from generators.dcim.device_plan import planned_device_names
from generators.dcim.template_interfaces import TemplatePort
from generators.helpers.naming import NamingConvention
from generators.model.errors import GeneratorError
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
    ColoClient,
    _virtual,
    _zone,
    seed_nodes,
    zone_env,
)

_EDGES = {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS}
_FIREWALLS = {"role": "internal-firewall", "quantity": 2, "template": _EDGE_TEMPLATE}
_BALANCERS = {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE}


async def run(
    monkeypatch: pytest.MonkeyPatch, *entries: dict[str, Any], client: ColoClient | None = None, **overrides: Any
) -> tuple[Any, ColoClient]:
    """A zone with ``entries`` as its kit, generated on the in-memory client; returns the generator and the client."""
    monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
    zone = _zone(fabric_templates=list(entries), **overrides)
    gen, client = zone_env(monkeypatch, client=client)
    seed_nodes(client, zone)
    await gen.generate({"TopologyColocationZone": [zone]})
    return gen, client


def of_kind(client: ColoClient, kind: str) -> dict[str, dict[str, Any]]:
    """The stored objects of a kind by name."""
    return {node.data["name"]: node.data for node in client.store.values() if node.kind == kind}


def names_of(client: ColoClient, kind: str) -> list[str]:
    return sorted(of_kind(client, kind))


def snapshot(client: ColoClient) -> dict[str, dict[str, Any]]:
    return {node_id: {k: v for k, v in node.data.items() if k != "id"} for node_id, node in client.store.items()}


@pytest.mark.asyncio
class TestGenerateEntry:
    async def test_missing_root_key_logs_error_and_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = zone_env(monkeypatch)

        await gen.generate({})

        gen.logger.error.assert_called_once()
        assert client.saves == []

    async def test_virtual_colocation_reads_its_own_root_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zone payload handed to the virtual colocation generator is 'no data', not silently accepted."""
        gen, client = zone_env(monkeypatch, cls=VirtualColocationGenerator)

        await gen.generate({"TopologyColocationZone": [_zone()]})

        gen.logger.error.assert_called_once()
        assert client.saves == []

    async def test_raw_graphql_response_is_cleaned_and_names_derive_from_the_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """generate() runs clean_data() first; fabric_name is the lower-cased zone name, deployment_id the zone id."""
        gen, client = zone_env(monkeypatch)
        seed_nodes(client, _zone())
        raw = {
            "TopologyColocationZone": {
                "edges": [
                    {
                        "node": {
                            "id": "zone-1",
                            "name": {"value": "FR2"},
                            "parent": {
                                "node": {
                                    "id": "metro-1",
                                    "name": {"value": "FR"},
                                    "naming_convention": {"value": "standard"},
                                    "fabric_asn_pool": {"node": {"id": "asn-pool", "name": {"value": "fr-asn-pool"}}},
                                }
                            },
                            "fabric_templates": {
                                "edges": [
                                    {
                                        "node": {
                                            "role": {"value": "edge"},
                                            "quantity": {"value": 2},
                                            "template": {
                                                "node": {
                                                    "id": "tpl-edge",
                                                    "template_kind": "TemplateDcimPhysicalDevice",
                                                }
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }

        await gen.generate(raw)

        assert (gen.fabric_name, gen.pod_name, gen.deployment_id) == ("fr2", None, "zone-1")
        assert names_of(client, "DcimPhysicalDevice") == ["eg-fr201", "eg-fr202"]
        assert client.related_at_first_write == ["zone-1"]

    async def test_zone_without_kit_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Most zones declare nothing (FR6): no pools, no devices, no EVPN, no error."""
        gen, client = await run(monkeypatch)

        assert client.saves == [] and gen.locked_keys == []
        gen.logger.error.assert_not_called()

    async def test_zone_whose_only_entries_are_unusable_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zone declaring only a virtual template builds nothing: warn per entry, then write nothing."""
        gen, client = await run(monkeypatch, {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE})

        assert client.saves == []
        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()

    async def test_the_numbering_of_a_role_continues_across_its_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two entries of a role (two models): the second never names a device of the first."""
        _, client = await run(
            monkeypatch,
            {**_EDGES, "quantity": 1},
            {**_EDGES, "quantity": 2, "template": {**_EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS, "id": "tpl-other"}},
        )

        devices = of_kind(client, "DcimPhysicalDevice")
        assert sorted(devices) == ["eg-fr201", "eg-fr202", "eg-fr203"]
        assert {name: data["object_template"]["id"] for name, data in devices.items()} == {
            "eg-fr201": _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS["id"],
            "eg-fr202": "tpl-other",
            "eg-fr203": "tpl-other",
        }

    async def test_two_firewall_entries_form_two_ha_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _FIREWALLS, {**_FIREWALLS, "template": {**_EDGE_TEMPLATE, "id": "t2"}})

        assert names_of(client, "DcimPhysicalDevice") == ["fw-fr201", "fw-fr202", "fw-fr203", "fw-fr204"]
        assert names_of(client, "ManagedFirewallHA") == ["fw-fr201-fw-fr202", "fw-fr203-fw-fr204"]


@pytest.mark.asyncio
class TestPools:
    async def test_pool_names_kinds_and_lengths_without_a_design(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES)

        addresses = of_kind(client, "CoreIPAddressPool")
        prefixes = of_kind(client, "CoreIPPrefixPool")
        assert {name: pool["default_prefix_length"] for name, pool in addresses.items()} == {
            "fr2-loopback-pool": _COLO_LOOPBACK_PREFIX_LENGTH,
            "fr2-management-pool": _COLO_MANAGEMENT_PREFIX_LENGTH,
            "fr2-loopback-ipv6-pool": 120,
        }
        zone_prefixes = {name: pool for name, pool in prefixes.items() if name.startswith("fr2-")}
        assert {name: pool["default_prefix_length"] for name, pool in zone_prefixes.items()} == {
            "fr2-technical-pool": _COLO_TECHNICAL_PREFIX_LENGTH,
            "fr2-technical-ipv6-pool": 124,
        }

    async def test_each_pool_is_cut_from_its_bootstrap_pool_and_holds_its_slice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        parents = {
            identifier: client.store[pool_id].data["name"]
            for pool_id, identifier in client.allocations
            if identifier.startswith("fr2-")
        }
        assert parents == {
            "fr2-loopback-pool": "Loopback-IPv4",
            "fr2-management-pool": "Management-IPv4",
            "fr2-technical-pool": "Technical-IPv4",
            "fr2-loopback-ipv6-pool": "Loopback-IPv6",
            "fr2-technical-ipv6-pool": "Technical-IPv6",
        }

    async def test_the_zone_points_at_its_three_ipv4_pools_in_one_untracked_save(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        names = {node_id: node.data.get("name") for node_id, node in client.store.items()}
        zone = client.store["zone-1"].data
        assert {key: names[zone[key]] for key in ("loopback_pool", "management_pool", "technical_pool")} == {
            "loopback_pool": "fr2-loopback-pool",
            "management_pool": "fr2-management-pool",
            "technical_pool": "fr2-technical-pool",
        }
        (kwargs,) = [kwargs for _, node_id, _, kwargs in client.saves if node_id == "zone-1"]
        assert kwargs == {"allow_upsert": True, "update_group_context": False}

    async def test_a_virtual_colocation_names_its_pools_after_itself_and_points_at_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        node = _virtual()
        gen, client = zone_env(monkeypatch, cls=VirtualColocationGenerator)
        seed_nodes(client, node)

        await gen.generate({"TopologyVirtualColocation": [node]})

        assert {"am-ne-loopback-pool", "am-ne-management-pool", "am-ne-technical-pool"} <= set(
            names_of(client, "CoreIPAddressPool") + names_of(client, "CoreIPPrefixPool")
        )
        assert {"loopback_pool", "management_pool", "technical_pool"} <= set(client.store["vc-1"].data)


@pytest.mark.asyncio
class TestDevices:
    async def test_edge_devices_deploy_to_the_zone_with_flat_naming_and_a_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES)

        devices = of_kind(client, "DcimPhysicalDevice")
        assert sorted(devices) == ["eg-fr201", "eg-fr202"]
        # The kit is the zone's: deployment is the zone id, never the metro.
        assert {data["deployment"]["id"] for data in devices.values()} == {"zone-1"}
        assert {data["role"] for data in devices.values()} == {"edge"}
        loopbacks = {
            client.store[data["device"]["id"]].data["name"]: data
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and (data := node.data)["role"] == "loopback"
        }
        assert sorted(loopbacks) == ["eg-fr201", "eg-fr202"]
        addresses = {
            node.data["identifier"]: node.data
            for node in client.store.values()
            if node.kind == "IpamIPAddress" and "identifier" in node.data
        }
        assert addresses["eg-fr201"]["description"] in {"Management IP for eg-fr201", "Loopback IP for eg-fr201"}

    async def test_every_device_gets_a_management_address_and_an_edge_a_loopback_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS)

        descriptions = sorted(
            node.data["description"]
            for node in client.store.values()
            if node.kind == "IpamIPAddress" and "identifier" in node.data
        )
        assert descriptions == [
            "EVPN VTEP loopback for eg-fr201",
            "EVPN VTEP loopback for eg-fr202",
            "Loopback IP for eg-fr201",
            "Loopback IP for eg-fr202",
            "Management IP for eg-fr201",
            "Management IP for eg-fr202",
            "Management IP for fw-fr201",
            "Management IP for fw-fr202",
        ]

    async def test_service_devices_pair_into_ha_domains_without_a_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS)

        assert names_of(client, "ManagedFirewallHA") == ["fw-fr201-fw-fr202"]
        assert names_of(client, "ManagedLoadbalancerHA") == ["lb-fr201-lb-fr202"]
        firewall = of_kind(client, "ManagedFirewallHA")["fw-fr201-fw-fr202"]
        assert firewall["group_id"]["from_pool"] and len(firewall["capabilities"]) == 2
        assert "group_id" not in of_kind(client, "ManagedLoadbalancerHA")["lb-fr201-lb-fr202"]
        looped = {
            client.store[node.data["device"]["id"]].data["name"]
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and node.data["role"] == "loopback"
        }
        assert looped == {"eg-fr201", "eg-fr202"}

    async def test_each_role_joins_its_role_group_and_a_missing_group_is_created_untracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS)

        assert {"edges", "firewalls"} <= set(names_of(client, "CoreStandardGroup"))
        created = [
            kwargs
            for kind, _, data, kwargs in client.saves
            if kind == "CoreStandardGroup" and data["name"] in ("edges", "firewalls")
        ]
        assert created and all(kwargs["update_group_context"] is False for kwargs in created)

    @pytest.mark.parametrize("convention", ["standard", "Standard", None, "hierarchical"])
    async def test_naming_convention_is_read_from_the_parent_metro_lowercased_and_defaulted(
        self, monkeypatch: pytest.MonkeyPatch, convention: str | None
    ) -> None:
        zone = _zone()
        zone["parent"]["naming_convention"] = convention
        zone["fabric_templates"] = [_EDGES]
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        gen, client = zone_env(monkeypatch)
        seed_nodes(client, zone)

        await gen.generate({"TopologyColocationZone": [zone]})

        applied: NamingConvention = "hierarchical" if convention == "hierarchical" else "standard"
        assert names_of(client, "DcimPhysicalDevice") == planned_device_names(
            fabric_name="fr2", pod_name=None, device_role="edge", quantity=2, naming_convention=applied, options={}
        )

    async def test_a_physical_template_is_never_a_virtual_device_or_an_appliance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS)

        assert not [
            node for node in client.store.values() if node.kind in ("CloudNetworkAppliance", "DcimVirtualDevice")
        ]


@pytest.mark.asyncio
class TestServiceCabling:
    async def test_the_firewall_pair_is_cabled_to_the_edge_pair_index_paired_and_without_addresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS)

        cables = sorted(name for name in names_of(client, "DcimCable") if "SYNC" not in name and "fw" in name)
        assert cables == [
            "eg-fr201-Ethernet1/15__fw-fr201-Ethernet1/31",
            "eg-fr201-Ethernet1/16__fw-fr201-Ethernet1/32",
            "eg-fr202-Ethernet1/15__fw-fr202-Ethernet1/31",
            "eg-fr202-Ethernet1/16__fw-fr202-Ethernet1/32",
        ]
        # A zone has no connectivity_mode and addresses no service link: always pbr, no P2P prefix
        service_ports = [
            node.data
            for node in client.store.values()
            if node.kind == "DcimPhysicalInterface" and "fw-fr2" in node.data.get("description", "")
        ]
        assert len(service_ports) == 8 and not [port for port in service_ports if "ip_address" in port]

    async def test_the_load_balancer_pair_is_cabled_on_its_own_ports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES, _BALANCERS)

        assert sorted(name for name in names_of(client, "DcimCable") if "lb" in name and "SYNC" not in name) == [
            "eg-fr201-Ethernet1/17__lb-fr201-Ethernet1/31",
            "eg-fr201-Ethernet1/18__lb-fr201-Ethernet1/32",
            "eg-fr202-Ethernet1/17__lb-fr202-Ethernet1/31",
            "eg-fr202-Ethernet1/18__lb-fr202-Ethernet1/32",
        ]

    async def test_the_ha_pairs_get_their_sync_cable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS)

        assert "CBL-fw-fr201-fw-fr202-SYNC" in names_of(client, "DcimCable")

    async def test_services_with_no_edge_are_created_and_cabled_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch, _FIREWALLS)

        assert names_of(client, "DcimPhysicalDevice") == ["fw-fr201", "fw-fr202"]
        assert names_of(client, "DcimCable") == ["CBL-fw-fr201-fw-fr202-SYNC"]

    async def test_a_template_without_the_ports_fails_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_firewall_ports = [port for port in _PORTS if port.role != "firewall"]
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        zone = _zone(fabric_templates=[_EDGES, _FIREWALLS])
        gen, client = zone_env(monkeypatch, ports=no_firewall_ports)
        seed_nodes(client, zone)

        with pytest.raises(GeneratorError, match="cannot cable"):
            await gen.generate({"TopologyColocationZone": [zone]})

        assert client.saves == []

    async def test_a_port_the_template_lacks_for_the_ha_sync_fails_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_sync = [port for port in _PORTS if port.role != "ha"]
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        zone = _zone(fabric_templates=[_EDGES, _FIREWALLS])
        gen, client = zone_env(monkeypatch, ports=[TemplatePort(p.name, p.role, p.interface_type) for p in no_sync])
        seed_nodes(client, zone)

        with pytest.raises(GeneratorError, match="No HA sync interface"):
            await gen.generate({"TopologyColocationZone": [zone]})

        assert client.saves == []


@pytest.mark.asyncio
class TestContextVlanPool:
    async def test_the_pool_is_keyed_by_the_zone_and_only_a_zone_with_physical_firewalls_gets_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, with_firewalls = await run(monkeypatch, _EDGES, _FIREWALLS)
        _, without = await run(monkeypatch, _EDGES, _BALANCERS)

        assert "fr2-fw-context-vlan-pool" in names_of(with_firewalls, "CoreNumberPool")
        assert not [name for name in names_of(without, "CoreNumberPool") if "context-vlan" in name]

    async def test_the_pool_is_created_under_its_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, _ = await run(monkeypatch, _EDGES, _FIREWALLS)

        assert "fw-context-pools-fr2" in gen.locked_keys

    async def test_a_virtual_colocation_has_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        node = _virtual(
            fabric_templates=[
                {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
                {"role": "internal-firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            ]
        )
        gen, client = zone_env(monkeypatch, cls=VirtualColocationGenerator)
        seed_nodes(client, node)

        await gen.generate({"TopologyVirtualColocation": [node]})

        assert not [name for name in names_of(client, "CoreNumberPool") if "context-vlan" in name]


@pytest.mark.asyncio
class TestLocks:
    async def test_the_run_holds_the_locks_of_what_it_decides_from_a_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, _ = await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS)

        assert sorted(gen.locked_keys) == sorted(
            [
                "colocation-pools-fr2",
                "ha-domain-fw-fr201-fw-fr202",
                "ha-domain-lb-fr201-lb-fr202",
                "firewall-ha-group-pool",
                "fw-context-pools-fr2",
                "shared-as-fr EVPN site ASN",
                "edge-pair-fr2",
                "routing-password-fr2-fabric-key",
                "bgp-af-ipv6-unicast",
                "bgp-af-l2vpn-evpn",
            ]
        )


@pytest.mark.asyncio
class TestRerun:
    async def test_a_rerun_writes_the_same_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS)
        first = snapshot(client)
        written = len(client.saves)

        await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS, client=client)

        assert snapshot(client) == first
        # nothing new was created: a rerun only upserts what it found
        assert len(client.created_ids) == len(set(client.created_ids)) and len(client.saves) > written

    async def test_a_rerun_creates_no_device_pool_cable_or_session_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS)
        before = {
            kind: len([n for n in client.store.values() if n.kind == kind])
            for kind in {n.kind for n in client.store.values()}
        }

        await run(monkeypatch, _EDGES, _FIREWALLS, _BALANCERS, client=client)

        after = {
            kind: len([n for n in client.store.values() if n.kind == kind])
            for kind in {n.kind for n in client.store.values()}
        }
        assert after == before

    async def test_the_edge_pair_key_keeps_its_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _EDGES)
        secret = of_kind(client, "RoutingPassword")["fr2-fabric-key"]["password"]

        await run(monkeypatch, _EDGES, client=client)

        assert of_kind(client, "RoutingPassword")["fr2-fabric-key"]["password"] == secret


def test_the_zone_class_flow_is_the_virtual_ones() -> None:
    """One base for both: they differ by class attributes only."""
    assert ColocationZoneGenerator.build_plan is VirtualColocationGenerator.build_plan
