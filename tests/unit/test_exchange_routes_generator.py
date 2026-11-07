"""add_context_routes: the routes of one service context, written tracked on its border devices in their VRF instances."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.exchange_routes import route_name
from generators.topology.context_routes import ContextRoutesGenerator


def _value(value: Any) -> dict[str, Any]:
    return {"value": value}


def _namespace(name: str, ns_type: str, vni: int) -> dict[str, Any]:
    return {
        "node": {"id": f"ns-{name}", "name": _value(name), "namespace_type": _value(ns_type), "l3_vni": _value(vni)}
    }


def _leg(address: str, namespace: dict[str, Any]) -> dict[str, Any]:
    return {
        "node": {
            "__typename": "DcimVirtualInterface",
            "id": f"leg-{address}",
            "device": {"node": {"id": "fw-1", "name": _value("fw-1"), "role": _value("internal-firewall")}},
            "ip_address": {"node": {"address": _value(address), "ip_namespace": namespace}},
        }
    }


def _port(device_id: str) -> dict[str, Any]:
    return {
        "node": {
            "__typename": "DcimPhysicalInterface",
            "id": f"port-{device_id}",
            "device": {"node": {"id": device_id, "name": _value(device_id), "role": _value("border-leaf")}},
        }
    }


PROD = _namespace("PROD", "prod", 50001)
INTERNET = _namespace("INTERNET", "internet", 50003)


def _firewall(
    *, tenant: str | None = None, snat: str | None = "192.0.2.5/32", ports: tuple[str, ...] = ("bl-1",)
) -> dict:
    return {
        "ManagedFirewallContext": {
            "edges": [
                {
                    "node": {
                        "id": "ctx-1",
                        "name": _value("shared-fw-prod"),
                        "tenant": {"node": {"id": tenant} if tenant else None},
                        "snat_address": {"node": {"address": _value(snat)} if snat else None},
                        "capabilities": {
                            "edges": [_leg("100.66.0.5/29", PROD), _leg("100.66.32.5/29", INTERNET)]
                            + [_port(p) for p in ports]
                        },
                    }
                }
            ]
        },
        "ManagedLoadbalancerContext": {"edges": []},
    }


def _loadbalancer(*, vip_prefix: str | None = "100.68.0.0/26") -> dict:
    return {
        "ManagedFirewallContext": {"edges": []},
        "ManagedLoadbalancerContext": {
            "edges": [
                {
                    "node": {
                        "id": "lb-1",
                        "name": _value("shared-lb-prod"),
                        "vip_prefix": {"node": {"prefix": _value(vip_prefix)} if vip_prefix else None},
                        "capabilities": {"edges": [_leg("100.66.64.5/29", PROD), _port("bl-1")]},
                    }
                }
            ]
        },
    }


def test_the_generator_has_the_lock_and_the_failure_helper_it_calls() -> None:
    """A real class, not a mock: resource_lock comes from PoolMixin and fail from CommonGenerator (a run on the
    server failed with no resource_lock while every test used a mocked one)."""
    for attribute in ("resource_lock", "fail", "reconcile_vrfs", "write_routes"):
        assert callable(getattr(ContextRoutesGenerator, attribute)), attribute


class _Gen(ContextRoutesGenerator):
    """The generator over a fake client; records the routes saved and their save kwargs."""

    def __init__(self, devices: list[Any], vrf_namespaces: tuple[str, ...] = ("PROD", "INTERNET")) -> None:
        self.log = MagicMock()
        self.logger = self.log
        self.client = MagicMock()
        self.saved: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.devices, self.vrf_namespaces = devices, vrf_namespaces
        self.vrfs = AsyncMock()
        self.reconcile_vrfs = self.vrfs
        self.resource_lock = MagicMock()
        self.resource_lock.return_value.__aenter__ = AsyncMock()
        self.resource_lock.return_value.__aexit__ = AsyncMock(return_value=False)

        async def filters(kind: Any, **_: Any) -> list[Any]:
            if kind.__name__ == "DcimDevice":
                return self.devices
            return [
                SimpleNamespace(
                    id=f"vrf-{d.id}-{ns}",
                    shared_devices=SimpleNamespace(peers=[SimpleNamespace(id=d.id)]),
                    namespace=SimpleNamespace(peer=SimpleNamespace(name=SimpleNamespace(value=ns))),
                )
                for d in self.devices
                for ns in self.vrf_namespaces
            ]

        async def create(kind: Any, data: dict[str, Any]) -> Any:
            node = MagicMock()

            async def save(**kwargs: Any) -> None:
                self.saved.append((data, kwargs))

            node.save = save
            return node

        self.client.filters, self.client.create = filters, create


def _device(device_id: str, role: str = "border-leaf") -> SimpleNamespace:
    return SimpleNamespace(id=device_id, name=SimpleNamespace(value=device_id), role=SimpleNamespace(value=role))


async def test_a_shared_firewall_context_routes_its_default_and_snat_return_tracked() -> None:
    """The default route to the tenant leg's VIP and the SNAT /32 to the INTERNET leg's, each in the device's VRF."""
    gen = _Gen([_device("bl-1")])
    await gen.generate(_firewall())
    by_prefix = {data["prefix"]: (data, kwargs) for data, kwargs in gen.saved}
    default, kwargs = by_prefix["0.0.0.0/0"]
    assert default["next_hop"] == "100.66.0.4" and default["exported"] is True
    assert default["vrf"] == {"id": "vrf-bl-1-PROD"} and default["capabilities"] == [{"id": "bl-1"}]
    assert by_prefix["192.0.2.5/32"][0]["next_hop"] == "100.66.32.4"
    assert by_prefix["192.0.2.5/32"][0]["vrf"] == {"id": "vrf-bl-1-INTERNET"}
    # tracked: the run's group owns the route, so it deletes the ones a later run no longer saves
    assert all(k == {"allow_upsert": True} for _, k in gen.saved)
    assert default["name"] == route_name("shared-fw-prod", "bl-1", "PROD", "0.0.0.0/0")
    gen.vrfs.assert_awaited_once_with(["bl-1"])


async def test_the_edge_keeps_the_tenant_default_local() -> None:
    """On a colocation edge the default stays inside the VRF."""
    gen = _Gen([_device("edge-1", "edge")])
    await gen.generate(_firewall(snat=None, ports=("edge-1",)))
    assert [(d["prefix"], d["exported"]) for d, _ in gen.saved] == [("0.0.0.0/0", False)]


async def test_a_dedicated_context_without_snat_writes_nothing() -> None:
    """A dedicated context steers its tenant by PBR and has no public address of its own here."""
    gen = _Gen([_device("bl-1")])
    await gen.generate(_firewall(tenant="cust-1", snat=None))
    assert gen.saved == []


async def test_every_border_device_gets_its_routes() -> None:
    """Two border leaves facing the pair carry the same routes, each under its own name."""
    gen = _Gen([_device("bl-1"), _device("bl-2")])
    await gen.generate(_firewall(ports=("bl-1", "bl-2")))
    assert len({d["name"] for d, _ in gen.saved}) == 4
    assert {d["capabilities"][0]["id"] for d, _ in gen.saved} == {"bl-1", "bl-2"}


async def test_a_load_balancer_context_routes_its_vip_range() -> None:
    """The VIP range goes to the leg's floating address, always exported."""
    gen = _Gen([_device("bl-1")])
    await gen.generate(_loadbalancer())
    assert [(d["prefix"], d["next_hop"], d["exported"]) for d, _ in gen.saved] == [
        ("100.68.0.0/26", "100.66.64.4", True)
    ]


async def test_a_context_without_legs_or_ports_writes_nothing() -> None:
    """A context built in steps writes nothing until it has a leg and a tagged border port."""
    gen = _Gen([_device("bl-1")])
    await gen.generate(_firewall(ports=()))
    assert gen.saved == [] and not gen.vrfs.await_count


async def test_a_load_balancer_context_without_a_vip_prefix_writes_nothing() -> None:
    """The VIP prefix is allocated before the legs; until then there is nothing to route."""
    gen = _Gen([_device("bl-1")])
    await gen.generate(_loadbalancer(vip_prefix=None))
    assert gen.saved == []


async def test_a_device_in_no_vrf_instance_fails_the_run() -> None:
    """The instance follows the legs; a route with no VRF to sit in is reported, not skipped."""
    gen = _Gen([_device("bl-1")], vrf_namespaces=())
    await gen.generate(_firewall())
    assert gen.saved == [] and gen.log.error.called


async def test_a_failed_save_is_reported_with_the_route_name() -> None:
    """The error names the route and the device."""
    gen = _Gen([_device("bl-1")])
    gen.client.create = AsyncMock(side_effect=RuntimeError("boom"))
    await gen.generate(_firewall())
    assert "bl-1" in gen.log.error.call_args.args[0] and "rt:shared-fw-prod:bl-1" in gen.log.error.call_args.args[0]


async def test_a_response_without_a_context_fails() -> None:
    """The query matched nothing."""
    gen = _Gen([])
    gen.fail = MagicMock(side_effect=RuntimeError("no context"))
    with pytest.raises(RuntimeError):
        await gen.generate({"ManagedFirewallContext": {"edges": []}, "ManagedLoadbalancerContext": {"edges": []}})
