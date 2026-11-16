"""The WAN exit of an office to its SD-WAN edge (generators/office/office_fabric.py and wan_exit.py): the cables from the
edge's LAN ports to the distribution pair, then per link a /31 of the PROD transit pool, a dot1q sub-interface per end in the
tenant VRF and the eBGP session; written by the reconciler on the in-memory client, after the edge of the same run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.capabilities.bgp import IPV4_UNICAST_AF
from generators.dcim.cabling_plan import PortFacts
from generators.model.errors import GeneratorError
from generators.office.wan_exit import ExitLink, plan_exit_links
from generators.plan import DeviceSpec, TopologyPlan
from tests.unit.office_fabric_generator_helpers import (
    _DISTRIBUTION_TEMPLATE,
    _EDGE,
    OfficeClient,
    _office,
    office_env,
)
from tests.unit.plan_graph_helpers import HfidNode
from utils.exchange_transit import wan_exit_vlan

_SWITCHES = ("ds-c005-office01", "ds-c005-office02")


async def run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: OfficeClient | None = None,
    edge: bool = False,
    templates: list[dict[str, Any]] | None = None,
) -> tuple[Any, OfficeClient, list[TopologyPlan]]:
    """The office generated on the in-memory client (an edge entry in its fabric_templates; ``edge``: a device of an
    earlier run exists); the plans the run applied are returned with the generator and the client.
    """
    gen, client = office_env(monkeypatch, client=client, edge=edge)
    plans: list[TopologyPlan] = []
    apply_plan = gen.apply_plan

    async def spy(plan: TopologyPlan) -> Any:
        plans.append(plan)
        return await apply_plan(plan)

    gen.apply_plan = spy
    office = _office()
    if templates is not None:
        office["fabric_templates"] = templates
    client.store.setdefault(
        "office-1", HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})
    )
    await gen.generate({"TopologyCustomerOffice": [office]})
    return gen, client, plans


def of_kind(client: OfficeClient, kind: str) -> dict[str, dict[str, Any]]:
    return {node.data["name"]: node.data for node in client.store.values() if node.kind == kind}


def snapshot(client: OfficeClient) -> dict[str, dict[str, Any]]:
    return {node_id: {k: v for k, v in node.data.items() if k != "id"} for node_id, node in client.store.items()}


def ids_by_name(client: OfficeClient) -> dict[str, str]:
    return {node.data["name"]: node_id for node_id, node in client.store.items() if "name" in node.data}


def edge_cables(client: OfficeClient) -> list[str]:
    return sorted(name for name in of_kind(client, "DcimCable") if name.startswith(f"{_EDGE}-"))


@pytest.mark.asyncio
class TestWanExit:
    async def test_cables_the_edge_to_the_switches_uplink_ports_one_each(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client, _ = await run(monkeypatch)

        assert edge_cables(client) == [
            f"{_EDGE}-GE3__ds-c005-office01-GigabitEthernet1/0/26",
            f"{_EDGE}-GE4__ds-c005-office02-GigabitEthernet1/0/26",
        ]
        # The cable is the office's; its links carry no technical-pool prefix: the addresses are the tenant-VRF ones
        cable = of_kind(client, "DcimCable")[edge_cables(client)[0]]
        assert cable["deployment"] == {"id": "office-1"}

    async def test_the_edge_is_built_by_the_run_before_the_exit_and_nothing_waits_for_another_generator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client, plans = await run(monkeypatch)

        fabric, edge, cabling, links = plans
        assert [spec.name for spec in edge.of(DeviceSpec)] == [_EDGE] and edge.root_id == "office-1"
        assert fabric.producers == cabling.producers == links.producers == []
        assert of_kind(client, "DcimPhysicalDevice")[_EDGE]["owner"] == {"id": "org-c005"}
        gen.wait_for_parent_generators.assert_not_awaited()

    async def test_each_link_gets_its_own_31_from_the_vrf_transit_pool_tracked_by_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = await run(monkeypatch)

        port_ids = {
            (client.store[node.data["device"]["id"]].data["name"], node.data["name"]): node.id
            for node in client.store.values()
            if node.kind == "DcimPhysicalInterface"
        }
        expected = {
            "__".join(sorted([port_ids[(_EDGE, edge_port)], port_ids[(switch, "GigabitEthernet1/0/26")]]))
            for edge_port, switch in (("GE3", _SWITCHES[0]), ("GE4", _SWITCHES[1]))
        }
        wan = {identifier: pool_id for pool_id, identifier in client.allocations if identifier.startswith("wan-exit__")}
        assert {identifier.removeprefix("wan-exit__") for identifier in wan} == expected
        assert set(wan.values()) == {"pool-FW-Transit-PROD-IPv4"}
        prefixes = [
            node
            for node in client.store.values()
            if node.kind == "IpamPrefix" and node.data.get("identifier", "").startswith("wan-exit__")
        ]
        assert len(prefixes) == 2 and all(str(n.prefix.value).endswith("/31") for n in prefixes)
        assert all(n.data["role"] == "technical" and n.data["is_pool"] is True for n in prefixes)
        # a pool allocation is no save: the run claims the prefix itself
        assert {n.id for n in prefixes} <= set(client.group_context.related_node_ids)

    async def test_both_ends_get_a_dot1q_sub_interface_with_the_vrf_tag_and_their_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = await run(monkeypatch)

        subs = {
            (client.store[node.data["device"]["id"]].data["name"], node.data["name"]): node.data
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and node.data["name"].endswith(".3800")
        }
        assert sorted(subs) == [
            (_EDGE, "GE3.3800"),
            (_EDGE, "GE4.3800"),
            (_SWITCHES[0], "GigabitEthernet1/0/26.3800"),
            (_SWITCHES[1], "GigabitEthernet1/0/26.3800"),
        ]
        assert wan_exit_vlan("prod") == 3800
        assert {(device, data["role"]) for (device, _), data in subs.items()} == {
            (_EDGE, "downlink"),
            (_SWITCHES[0], "uplink"),
            (_SWITCHES[1], "uplink"),
        }
        # the switch takes the lower address of the /31, the edge the upper
        for switch, edge_port in zip(_SWITCHES, ("GE3", "GE4"), strict=True):
            low = client.store[subs[(switch, "GigabitEthernet1/0/26.3800")]["ip_address"]].data["address"]
            high = client.store[subs[(_EDGE, f"{edge_port}.3800")]["ip_address"]].data["address"]
            assert low.endswith(".0.0/31") and high.endswith(".0.1/31") and low.split(".")[1] == high.split(".")[1]

    async def test_each_sub_interface_rides_its_trunk_and_sits_in_the_prod_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = await run(monkeypatch)

        subs = {
            f"{client.store[node.data['device']['id']].data['name']}:{node.data['name']}": node.data
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and node.data["name"].endswith(".3800")
        }
        assert sorted(subs) == [
            f"{_EDGE}:GE3.3800",
            f"{_EDGE}:GE4.3800",
            f"{_SWITCHES[0]}:GigabitEthernet1/0/26.3800",
            f"{_SWITCHES[1]}:GigabitEthernet1/0/26.3800",
        ]
        assert {data["vlan_id"] for data in subs.values()} == {3800}
        trunk = client.store[subs[f"{_SWITCHES[0]}:GigabitEthernet1/0/26.3800"]["parent"]["id"]].data
        assert trunk["name"] == "GigabitEthernet1/0/26"
        devices = {name: node_id for name, node_id in ids_by_name(client).items() if name in (_EDGE, *_SWITCHES)}
        added = {(node_id, relation): peers for node_id, relation, peers in client.edge_adds}
        by_device = {
            client.store[node_id].data["device"]["id"]: peers
            for (node_id, relation), peers in added.items()
            if relation == "capabilities" and client.store[node_id].kind == "DcimVirtualInterface"
        }
        assert {device_id: peers for device_id, peers in by_device.items()} == {
            devices[_SWITCHES[0]]: [f"vrf-{devices[_SWITCHES[0]]}-PROD"],
            devices[_SWITCHES[1]]: [f"vrf-{devices[_SWITCHES[1]]}-PROD"],
            devices[_EDGE]: [f"vrf-{devices[_EDGE]}-PROD"],
        } or len(by_device) == 3

    async def test_every_end_keeps_its_device_lock_over_the_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client, _ = await run(monkeypatch)

        devices = [node.id for node in client.store.values() if node.data.get("name") in (_EDGE, *_SWITCHES)]
        assert sorted(key for key in gen.locked_keys if key.startswith("vrf-device-")) == sorted(
            f"vrf-device-{device}" for device in devices
        )

    async def test_the_exit_writes_under_a_per_office_lock_with_its_shared_objects_locked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, _, _ = await run(monkeypatch)

        assert [key for key in gen.locked_keys if not key.startswith("vrf-device-")][2:] == [
            "bgp-af-ipv4-unicast",
            "office-wan-exit-office-1",
            "routing-password-c005-office-wan-exit-key",
            "shared-as-c005-office SD-WAN edge ASN",
            "shared-as-c005-office WAN exit ASN",
        ]

    async def test_one_process_per_switch_and_one_on_the_edge_all_regular_and_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = await run(monkeypatch)

        processes = {
            name: data for name, data in of_kind(client, "ManagedBGP").items() if name.endswith("-bgp-wan-exit")
        }
        assert sorted(processes) == [f"{_EDGE}-bgp-wan-exit", *(f"{s}-bgp-wan-exit" for s in _SWITCHES)]
        assert {data["process_role"] for data in processes.values()} == {"regular"}
        as_of = {name: data["local_as"]["id"] for name, data in processes.items()}
        # the distribution pair shares one AS, the edge has its own
        assert (
            as_of[f"{_SWITCHES[0]}-bgp-wan-exit"]
            == as_of[f"{_SWITCHES[1]}-bgp-wan-exit"]
            != as_of[f"{_EDGE}-bgp-wan-exit"]
        )
        loopbacks = {
            client.store[n.data["device"]["id"]].data["name"]: n.data["ip_address"]
            for n in client.store.values()
            if n.kind == "DcimVirtualInterface" and n.data["role"] == "loopback"
        }
        for switch in _SWITCHES:
            assert processes[f"{switch}-bgp-wan-exit"]["router_id"] == {"id": loopbacks[switch]}
        # the edge has no loopback: its router-id is its first handoff address, as the gateway's is
        first_edge_address = client.store[
            next(
                n.data["ip_address"]
                for n in client.store.values()
                if n.kind == "DcimVirtualInterface" and n.data["name"] == "GE3.3800"
            )
        ].data["address"]
        router = client.store[processes[f"{_EDGE}-bgp-wan-exit"]["router_id"]["id"]].data["address"]
        assert router == first_edge_address
        process_ids = {n.id for n in client.store.values() if n.kind == "ManagedBGP"}
        assert all(kwargs == {"allow_upsert": True} for _, node_id, _, kwargs in client.saves if node_id in process_ids)

    async def test_the_two_as_numbers_are_found_by_description_before_one_is_drawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = OfficeClient()
        switch_as = client.seed("RoutingAutonomousSystem", description="c005-office WAN exit ASN", asn=64600)

        _, client, _ = await run(monkeypatch, client=client)

        drawn = [n for n in client.store.values() if n.kind == "RoutingAutonomousSystem" and n.id != switch_as.id]
        assert [n.data["description"] for n in drawn] == ["c005-office SD-WAN edge ASN"]
        assert drawn[0].data["asn"] == {"from_pool": {"id": "pool-PRIVATE-ASN32"}}
        assert of_kind(client, "ManagedBGP")[f"{_SWITCHES[0]}-bgp-wan-exit"]["local_as"]["id"] == switch_as.id

    async def test_one_ebgp_regular_peering_per_link_on_both_processes_and_both_sub_interfaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = await run(monkeypatch)

        peerings = of_kind(client, "ManagedBGPPeering")
        assert sorted(peerings) == [f"WAN-EXIT-{switch}-{_EDGE}" for switch in _SWITCHES]
        names = ids_by_name(client)
        first = peerings[f"WAN-EXIT-{_SWITCHES[0]}-{_EDGE}"]
        assert (first["session_type"], first["peering_role"], first["status"]) == ("EBGP", "regular", "active")
        assert {p["id"] for p in first["bgp_processes"]} == {
            names[f"{_SWITCHES[0]}-bgp-wan-exit"],
            names[f"{_EDGE}-bgp-wan-exit"],
        }
        interfaces = {client.store[i["id"]].data["name"] for i in first["capabilities"]}
        assert interfaces == {"GigabitEthernet1/0/26.3800", "GE3.3800"}
        assert first["password"] == {"id": names["c005-office-wan-exit-key"]}
        family = client.store[first["address_families"][0]["id"]].data
        assert (family["afi"], family["safi"]) == IPV4_UNICAST_AF
        assert (first["ttl"], first["bfd_enabled"]) == (1, True)

    async def test_the_key_is_a_tracked_find_or_create_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client, _ = await run(monkeypatch)
        key = next(n for n in client.store.values() if n.data.get("name") == "c005-office-wan-exit-key")
        secret = key.data["password"]
        assert secret and [k for _, i, _, k in client.saves if i == key.id] == [{"allow_upsert": True}]

        await run(monkeypatch, client=client)

        assert client.store[key.id].data["password"] == secret

    async def test_a_rerun_writes_the_same_objects_under_the_same_identifiers_and_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = await run(monkeypatch)
        # the fake stores a created address by its text: the second run is the first to carry its description
        await run(monkeypatch, client=client)
        first = snapshot(client)
        allocations = set(client.allocations)

        await run(monkeypatch, client=client)

        assert snapshot(client) == first
        assert set(client.allocations) == allocations


@pytest.mark.asyncio
class TestWanExitSkips:
    async def test_an_office_without_an_edge_entry_skips_the_exit_and_builds_no_edge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = office_env(monkeypatch)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})

        await gen.generate({"TopologyCustomerOffice": [_office(edge=False)]})

        assert "office-wan-exit-office-1" not in gen.locked_keys and edge_cables(client) == []
        assert _EDGE not in of_kind(client, "DcimPhysicalDevice")

    async def test_an_edge_of_an_earlier_run_is_adopted_not_recreated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client, _ = await run(monkeypatch, edge=True)

        assert len(edge_cables(client)) == 2
        assert [n for n in client.store.values() if n.data.get("name") == _EDGE and n.kind == "DcimPhysicalDevice"][
            0
        ].id.startswith("DcimPhysicalDevice-")
        assert not any(data.get("object_template") for kind, _, data, _ in client.saves if data.get("name") == _EDGE)

    async def test_a_template_without_an_uplink_port_warns_and_cables_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template = {
            **_DISTRIBUTION_TEMPLATE,
            "interfaces": [i for i in _DISTRIBUTION_TEMPLATE["interfaces"] if i["role"] != "uplink"],
        }
        gen, client = office_env(monkeypatch)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})
        office = _office()
        office["fabric_templates"][0] = {"role": "distribution-switch", "quantity": 2, "template": template}

        await gen.generate({"TopologyCustomerOffice": [office]})

        gen.logger.warning.assert_called_once()
        assert "uplink-role port" in gen.logger.warning.call_args.args[0]
        assert edge_cables(client) == []

    @pytest.mark.parametrize("missing", ["SecurityZone", "pool"])
    async def test_a_missing_namespace_or_transit_pool_fails_the_exit_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        gen, client = office_env(monkeypatch)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})
        for node_id, node in list(client.store.items()):
            if node.kind == "SecurityZone" and missing == "SecurityZone":
                del client.store[node_id]
            if node.kind == "CoreIPPrefixPool" and node.data["name"] == "FW-Transit-PROD-IPv4" and missing == "pool":
                del client.store[node_id]

        with pytest.raises(GeneratorError, match="load data/bootstrap"):
            await gen.generate({"TopologyCustomerOffice": [_office()]})

        assert edge_cables(client) == []
        assert not [node for node in client.store.values() if node.kind == "ManagedBGPPeering"]

    async def test_an_edge_template_that_does_not_exist_fails_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = office_env(monkeypatch)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})
        for node_id, node in list(client.store.items()):
            if node.kind == "TemplateDcimPhysicalDevice":
                del client.store[node_id]

        with pytest.raises(GeneratorError, match="Template 'VCE-610_EDGE' not found"):
            await gen.generate({"TopologyCustomerOffice": [_office()]})

        assert edge_cables(client) == []


class TestExitLinksWithoutALoopback:
    def test_a_switch_without_an_addressed_loopback_gets_no_session_and_the_others_are_peered(self) -> None:
        def link(switch: str, edge_port: str) -> ExitLink:
            return ExitLink(
                switch=PortFacts(switch, "GigabitEthernet1/0/26", "1000base-t"),
                switch_port_id=f"sp-{switch}",
                edge=PortFacts(_EDGE, edge_port, "1000base-t"),
                edge_port_id=f"ep-{edge_port}",
            )

        plan = TopologyPlan(root_id=None)
        logger = MagicMock()
        plan_exit_links(
            plan,
            office_name="C005-OFFICE",
            fabric_name="c005-office",
            edge_name=_EDGE,
            links=[link(_SWITCHES[0], "GE3"), link(_SWITCHES[1], "GE4")],
            transit_pool="FW-Transit-PROD-IPv4",
            vlan=3800,
            vrf_ids={(f"id-{n}", "PROD"): f"vrf-{n}" for n in (*_SWITCHES, _EDGE)},
            device_ids={n: f"id-{n}" for n in (*_SWITCHES, _EDGE)},
            zone="PROD",
            asn_pool_id="asn-pool",
            as_ids={},
            families={IPV4_UNICAST_AF: "af-ipv4"},
            router_ids={_SWITCHES[1]: "lo-2"},
            unicast=IPV4_UNICAST_AF,
            logger=logger,
        )

        keys = [getattr(spec, "key", "") for spec in plan.specs]
        assert f"bgp-peering:WAN-EXIT-{_SWITCHES[1]}-{_EDGE}" in keys
        assert f"bgp-peering:WAN-EXIT-{_SWITCHES[0]}-{_EDGE}" not in keys
        assert f"bgp:{_SWITCHES[0]}-bgp-wan-exit" not in keys
        logger.warning.assert_called_once()
