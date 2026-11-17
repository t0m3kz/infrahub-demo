"""The SD-WAN side of an office (generators/office/edge.py, run by add_office_fabric): its edge, one tunnel per gateway,
the controller attachment and the LAN of an office with no switches; from the raw GraphQL shape of the query, on the
in-memory client. The internet access of the edge is authored data: the generator builds no carrier circuit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from generators.model.errors import GeneratorError
from tests.unit.colocation_zone_generator_helpers import ColoClient
from tests.unit.office_fabric_generator_helpers import (
    EDGE_TEMPLATE,
    OfficeClient,
    office_env,
)
from tests.unit.plan_graph_helpers import HfidNode
from utils.limits import GROUP_VIRTUAL_CIRCUITS
from utils.segment_addressing import tenant_pool_name

_EDGE = "C002-OFFICE-EDGE1"
_OVERLAY = "provider-overlay"


def _gateway(
    name: str, *, owner: str | None = _OVERLAY, zone: str | None = "FR2", ports: tuple[str, ...] = ("eth1",)
) -> dict[str, Any]:
    """A gateway as the query returns it: owner (a provider unless ``owner`` is None), zone and its public ports."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "owner": {"node": {"id": owner, "__typename": "OrganizationProvider"}}
        if owner
        else {"node": {"id": "cust-1", "__typename": "OrganizationCustomer"}},
        "deployment": {"node": {"id": f"zone-{zone}", "name": {"value": zone}}} if zone else None,
        "interfaces": {
            "edges": [
                {
                    "node": {
                        "__typename": "DcimPhysicalInterface",
                        "id": f"{name}-eth0",
                        "name": {"value": "eth0"},
                        "role": {"value": "uplink"},
                    }
                },
                *(
                    {
                        "node": {
                            "__typename": "DcimPhysicalInterface",
                            "id": f"{name}-{port}",
                            "name": {"value": port},
                            "role": {"value": "customer"},
                        }
                    }
                    for port in ports
                ),
            ]
        },
    }


def _raw_office(
    *,
    gateways: tuple[dict[str, Any], ...] = (_gateway("GW1"), _gateway("GW2", zone="PA4")),
    entries: tuple[tuple[str, int], ...] = (("sdwan-edge", 1),),
    lan: bool = False,
) -> dict[str, Any]:
    """The query response of an office named C002-OFFICE, as the server delivers it (before clean_data)."""
    template = {
        "id": EDGE_TEMPLATE["id"],
        "template_name": {"value": EDGE_TEMPLATE["template_name"]},
        "template_kind": "TemplateDcimPhysicalDevice",
        "interfaces": {"edges": []},
    }
    node = {
        "id": "office-2",
        "name": {"value": "C002-OFFICE"},
        "naming_convention": {"value": "standard"},
        "owner": {"node": {"id": "org-c002", "name": {"value": "C002"}}},
        "lan_prefixes": {"edges": [{"node": {"id": "lan-1"}}] if lan else []},
        "sdwan_gateways": {"edges": [{"node": gateway} for gateway in gateways]},
        "fabric_templates": {
            "edges": [
                {"node": {"role": {"value": role}, "quantity": {"value": quantity}, "template": {"node": template}}}
                for role, quantity in entries
            ]
        },
    }
    return {"TopologyCustomerOffice": {"edges": [{"node": node}]}}


class EdgeClient(OfficeClient):
    """The office client plus the controllers that manage the gateways: ``managed`` is controller name -> gateway ids."""

    def __init__(self, managed: dict[str, list[str]]) -> None:
        super().__init__()
        self.controllers = [
            SimpleNamespace(
                id=f"ctl-{name}",
                name=SimpleNamespace(value=name),
                managed_devices=SimpleNamespace(peers=[SimpleNamespace(id=gateway) for gateway in gateways]),
                add_relationships=AsyncMock(),
            )
            for name, gateways in managed.items()
        ]

    async def allocate_next_ip_prefix(self, resource_pool: Any, identifier: str, **kwargs: Any) -> Any:
        """The pool's next prefix (an office LAN asks for the pool's default length: none)."""
        node = await super(ColoClient, self).allocate_next_ip_prefix(resource_pool, identifier, **kwargs)
        node.data.setdefault("prefix", "10.210.9.0/24")
        return node

    async def filters(self, kind: Any, **flt: Any) -> list[Any]:
        if getattr(kind, "__name__", kind) == "ManagedController":
            return [
                c
                for c in self.controllers
                if {p.id for p in c.managed_devices.peers} & set(flt["managed_devices__ids"])
            ]
        return await super().filters(kind, **flt)


def _env(monkeypatch: pytest.MonkeyPatch, managed: dict[str, list[str]] | None = None) -> tuple[Any, EdgeClient]:
    managed = {"VCO1": ["id-GW1", "id-GW2"]} if managed is None else managed
    gen, client = office_env(monkeypatch, client=EdgeClient(managed))
    client.seed("CoreIPPrefixPool", name=tenant_pool_name("PROD"))
    client.seed("CoreStandardGroup", name=GROUP_VIRTUAL_CIRCUITS)
    client.store["office-2"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-2", "name": "C002-OFFICE"})
    return gen, cast(EdgeClient, client)


def _of_kind(client: OfficeClient, kind: str) -> dict[str, dict[str, Any]]:
    return {node.data["name"]: node.data for node in client.store.values() if node.kind == kind and "name" in node.data}


def _tunnel_saves(client: OfficeClient) -> list[str]:
    return [data["name"] for kind, _, data, _ in client.saves if kind == "TopologySessionCircuit"]


@pytest.mark.asyncio
class TestEdgeAndTunnels:
    async def test_the_edge_and_one_tunnel_per_gateway_are_built_and_no_internet_carrier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch)

        await gen.generate(_raw_office())

        edge = _of_kind(client, "DcimPhysicalDevice")[_EDGE]
        assert edge["owner"] == {"id": "org-c002"} and edge["deployment"] == {"id": "office-2"}
        tunnels = _of_kind(client, "TopologySessionCircuit")
        assert sorted(tunnels) == ["C002-OFFICE-SDWAN-GW1", "C002-OFFICE-SDWAN-GW2"]
        ports = {
            node.id: node.data["name"]
            for node in client.store.values()
            if node.kind == "DcimPhysicalInterface" and node.data["device"]["id"] != "x"
        }
        first = tunnels["C002-OFFICE-SDWAN-GW1"]
        assert first["circuit_type"] == "tunnel" and first["provider"] == {"id": _OVERLAY}
        assert first["locations"] == [{"id": "office-2"}, {"id": "zone-FR2"}]
        gateway_end, edge_end = (end["id"] for end in first["capabilities"])
        assert gateway_end == "GW1-eth1" and ports[edge_end] == "GE1"
        assert not any(kind == "TopologyCarrierCircuit" for kind, *_ in client.saves)

    async def test_the_tunnel_provider_is_the_owner_of_its_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _env(monkeypatch)
        gateways = (_gateway("GW1", owner="provider-a"), _gateway("GW2", owner="provider-b", zone="PA4"))

        await gen.generate(_raw_office(gateways=gateways))

        tunnels = _of_kind(client, "TopologySessionCircuit")
        assert [tunnels[f"C002-OFFICE-SDWAN-{name}"]["provider"]["id"] for name in ("GW1", "GW2")] == [
            "provider-a",
            "provider-b",
        ]

    async def test_the_edge_joins_the_controller_that_manages_the_gateways(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch)

        await gen.generate(_raw_office())

        edge_id = next(n.id for n in client.store.values() if n.data.get("name") == _EDGE)
        client.controllers[0].add_relationships.assert_awaited_once_with(
            relation_to_update="managed_devices", related_nodes=[edge_id]
        )

    async def test_an_edge_the_controller_already_manages_is_not_added_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch)
        await gen.generate(_raw_office())
        edge_id = next(n.id for n in client.store.values() if n.data.get("name") == _EDGE)
        client.controllers[0].managed_devices.peers.append(SimpleNamespace(id=edge_id))
        client.controllers[0].add_relationships.reset_mock()

        await gen.generate(_raw_office())

        client.controllers[0].add_relationships.assert_not_awaited()

    async def test_gateways_of_two_controllers_fail_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _env(monkeypatch, {"VCO1": ["id-GW1"], "VCO2": ["id-GW2"]})

        with pytest.raises(GeneratorError, match=r"GW1 by \['VCO1'\].*GW2 by \['VCO2'\]"):
            await gen.generate(_raw_office())

        assert client.saves == []

    async def test_no_controller_is_a_warning_and_the_edge_is_still_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch, {})

        await gen.generate(_raw_office())

        assert _EDGE in _of_kind(client, "DcimPhysicalDevice")
        assert "no controller manages its gateways" in gen.logger.warning.call_args.args[0]

    @pytest.mark.parametrize(
        ("gateway", "message"),
        [
            pytest.param(
                _gateway("GW1", owner=None), "no owner that is an OrganizationProvider", id="owner-is-a-customer"
            ),
            pytest.param(_gateway("GW1", zone=None), "has no deployment", id="no-zone"),
            pytest.param(_gateway("GW1", ports=()), "no role=customer public interface", id="no-public-port"),
            pytest.param(
                _gateway("GW1", ports=("eth1", "eth2")),
                r"several role=customer public interfaces \['eth1', 'eth2'\]",
                id="two-public-ports",
            ),
        ],
    )
    async def test_a_gateway_with_a_gap_fails_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch, gateway: dict[str, Any], message: str
    ) -> None:
        gen, client = _env(monkeypatch)

        with pytest.raises(GeneratorError, match=message):
            await gen.generate(_raw_office(gateways=(gateway,)))

        assert client.saves == []

    async def test_two_edges_fail_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _env(monkeypatch)

        with pytest.raises(GeneratorError, match="one SD-WAN edge"):
            await gen.generate(_raw_office(entries=(("sdwan-edge", 2),)))

        assert client.saves == []

    async def test_gateways_without_an_edge_entry_build_nothing_and_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _env(monkeypatch)

        await gen.generate(_raw_office(entries=()))

        assert client.saves == []
        assert "without a sdwan-edge" in gen.logger.warning.call_args.args[0]

    async def test_an_edge_without_gateways_is_built_with_no_tunnel_and_no_controller_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch)

        await gen.generate(_raw_office(gateways=()))

        assert _EDGE in _of_kind(client, "DcimPhysicalDevice") and _tunnel_saves(client) == []
        client.controllers[0].add_relationships.assert_not_awaited()


@pytest.mark.asyncio
class TestEdgeOnlyOffice:
    async def test_it_builds_the_edge_the_tunnels_and_the_lan_and_no_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch)

        await gen.generate(_raw_office())

        assert list(_of_kind(client, "DcimPhysicalDevice")) == [_EDGE]
        assert not _of_kind(client, "DcimCable") and not _of_kind(client, "CoreNumberPool")
        assert len(_tunnel_saves(client)) == 2
        assert client.allocations and next(iter(client.allocations))[1] == "office-lan__C002-OFFICE"
        assert client.store["office-2"].data["lan_prefixes"]

    async def test_an_office_with_its_own_lan_prefix_gets_none_allocated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _env(monkeypatch)

        await gen.generate(_raw_office(lan=True))

        assert client.allocations == {}

    async def test_a_missing_tenant_pool_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _env(monkeypatch)
        for node_id, node in list(client.store.items()):
            if node.data.get("name") == tenant_pool_name("PROD"):
                del client.store[node_id]

        with pytest.raises(GeneratorError, match="tenant pool"):
            await gen.generate(_raw_office())

    async def test_a_rerun_writes_the_same_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fake keeps a tunnel as a new node per save (the server upserts it by name), so the tunnels are compared by
        what each run saved."""
        gen, client = _env(monkeypatch)
        # the fake stores a created address by its text: the second run is the first to carry its description
        for _ in range(3):
            await gen.generate(_raw_office())

        def state() -> dict[str, dict[str, Any]]:
            return {
                node_id: dict(node.data)
                for node_id, node in client.store.items()
                if node.kind != "TopologySessionCircuit"
            }

        first, count = state(), len(client.saves)

        await gen.generate(_raw_office())

        assert state() == first
        tunnels = [(kind, data) for kind, _, data, _ in client.saves[count:] if kind == "TopologySessionCircuit"]
        assert [data["name"] for _, data in tunnels] == ["C002-OFFICE-SDWAN-GW1", "C002-OFFICE-SDWAN-GW2"]

    async def test_a_gateway_taken_out_of_the_list_is_no_longer_saved_so_the_cleanup_reclaims_its_tunnel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _env(monkeypatch)
        await gen.generate(_raw_office())
        before = len(client.saves)

        await gen.generate(_raw_office(gateways=(_gateway("GW1"),)))

        assert _tunnel_saves(client)[2:] == ["C002-OFFICE-SDWAN-GW1"]
        # tracked: a plain upsert, never the untracked save of a shared object
        assert all(
            kwargs == {"allow_upsert": True}
            for kind, _, _, kwargs in client.saves[before:]
            if kind == "TopologySessionCircuit"
        )
