"""The reconciler of the exchange routes: owned by one context, manual routes kept, writes before deletes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.exchange_routes import ExchangeRoutesMixin, route_name

TENANT = {"namespace": "PROD", "l3_vni": 10, "ns_type": "prod", "vip": "10.9.0.5"}
INTERNET = {"namespace": "INTERNET", "l3_vni": 99, "ns_type": "internet", "vip": "198.51.100.5"}
CTX = "shared-x-prod"
CONTEXT = MagicMock(id="ctx-1")
CONTEXT.name.value = CTX


def _device(name: str, role: str) -> MagicMock:
    device = MagicMock(id=f"id-{name}")
    device.name.value = name
    device.role.value = role
    return device


def _route(name: str, origin: str, maintained_by: str = "ctx-1") -> MagicMock:
    route = MagicMock()
    route.maintained_by.id = maintained_by
    route.name.value, route.origin.value = name, origin
    route.next_hop.value, route.exported.value = "0.0.0.0", True
    route.delete = AsyncMock()
    return route


class _Writer(ExchangeRoutesMixin):
    """The mixin over a fake client holding the routes that exist and recording the ones created."""

    def __init__(self, devices: list[Any], existing: list[Any] | None = None) -> None:
        self.log = MagicMock()
        self.logger = self.log
        self.client = MagicMock()
        self.devices, self.existing = devices, existing or []
        self.created: list[dict[str, Any]] = []
        self.fail = False
        self.vrf_namespaces = ["PROD", "INTERNET"]

        async def filters(kind: str, **kw: Any) -> list[Any]:
            if getattr(kind, "__name__", kind) == "DcimDevice":
                return self.devices
            if getattr(kind, "__name__", kind) == "ManagedVRF":  # every device is in an instance of each namespace
                return [
                    MagicMock(
                        id=f"vrf-{d.id}-{ns}",
                        shared_devices=MagicMock(peers=[MagicMock(id=d.id)]),
                        namespace=SimpleNamespace(peer=SimpleNamespace(name=SimpleNamespace(value=ns))),
                    )
                    for d in self.devices
                    for ns in self.vrf_namespaces
                ]
            if "name__value" in kw:
                return [r for r in self.existing if r.name.value == kw["name__value"]]
            if "maintained_by__ids" in kw:
                return [r for r in self.existing if r.maintained_by.id in kw["maintained_by__ids"]]
            return self.existing

        async def create(kind: str, data: dict[str, Any]) -> MagicMock:
            if self.fail:
                raise RuntimeError("boom")
            self.created.append(data)
            node = MagicMock()
            node.save = AsyncMock()
            return node

        self.client.filters, self.client.create = filters, create

    def specs(self, *, shared: bool = True, snat: str | None = "203.0.113.7") -> dict[bool, list[dict[str, Any]]]:
        kw = {"tenant_leg": TENANT, "peer_legs": [INTERNET], "shared": shared, "snat": snat}
        return {False: self.exchange_specs(edge=False, **kw), True: self.exchange_specs(edge=True, **kw)}

    async def run(self, **kw: Any) -> bool:
        return await self.reconcile_routes(
            context=CONTEXT, device_ids=[d.id for d in self.devices], specs_for=kw.pop("specs_for")
        )


async def test_a_complete_context_without_routes_gets_them() -> None:
    """The reconcile writes the default and the SNAT return route whatever the legs look like."""
    writer = _Writer([_device("bl1", "border-leaf")])
    assert await writer.run(specs_for=writer.specs())
    by_prefix = {d["prefix"]: d for d in writer.created}
    assert by_prefix["0.0.0.0/0"]["next_hop"] == "10.9.0.5" and by_prefix["0.0.0.0/0"]["exported"] is True
    assert by_prefix["203.0.113.7/32"]["next_hop"] == "198.51.100.5"
    assert by_prefix["203.0.113.7/32"]["vrf"] == {"id": "vrf-id-bl1-INTERNET"}
    assert all(d["capabilities"] == [{"id": "id-bl1"}] and d["origin"] == "generated" for d in writer.created)


async def test_an_edge_keeps_the_tenant_default_local() -> None:
    """On a colocation edge the default stays inside the VRF."""
    writer = _Writer([_device("edge1", "edge")])
    await writer.run(specs_for=writer.specs(snat=None))
    assert [d["exported"] for d in writer.created] == [False]


async def test_idempotent_names() -> None:
    """Two runs write the same names, so the upsert converges instead of duplicating."""
    writer = _Writer([_device("bl1", "border-leaf")])
    await writer.run(specs_for=writer.specs())
    await writer.run(specs_for=writer.specs())
    names = [d["name"] for d in writer.created]
    assert len(names) == 4 and len(set(names)) == 2


async def test_a_changed_next_hop_or_snat_is_rewritten_and_the_old_route_removed() -> None:
    """A new SNAT address is a new prefix: it is written, then the old one is deleted."""
    old = _route(route_name(CTX, "bl1", "INTERNET", "203.0.113.6/32"), "generated")
    writer = _Writer([_device("bl1", "border-leaf")], [old])
    await writer.run(specs_for=writer.specs(snat="203.0.113.7"))
    assert "203.0.113.7/32" in {d["prefix"] for d in writer.created}
    assert old.delete.await_count == 1


async def test_a_failed_write_keeps_the_previous_routes() -> None:
    """When a write fails nothing is deleted and the run reports it."""
    old = _route(route_name(CTX, "bl1", "INTERNET", "203.0.113.6/32"), "generated")
    writer = _Writer([_device("bl1", "border-leaf")], [old])
    writer.fail = True
    assert not await writer.run(specs_for=writer.specs())
    assert old.delete.await_count == 0 and writer.log.error.called


async def test_manual_and_foreign_routes_are_never_touched() -> None:
    """A manual route of the same name is kept; a generated route of another context is not ours to delete."""
    mine = route_name(CTX, "bl1", "PROD", "0.0.0.0/0")
    manual = _route(mine, "manual")
    foreign = _route(route_name("other-ctx", "bl1", "PROD", "9.9.9.0/24"), "generated", maintained_by="ctx-2")
    person = _route("my-route", "manual")
    writer = _Writer([_device("bl1", "border-leaf")], [manual, foreign, person])
    await writer.run(specs_for=writer.specs(snat=None))
    assert mine not in {d["name"] for d in writer.created} and writer.log.warning.called
    assert all(r.delete.await_count == 0 for r in (manual, foreign, person))


async def test_vip_route_goes_to_the_leg_vip() -> None:
    """A load-balancer context routes its VIP range to the floating address, always exported."""
    writer = _Writer([_device("bl1", "border-leaf")])
    spec = writer.vip_spec(TENANT, "10.8.0.0/26")
    await writer.run(specs_for={False: [spec], True: [spec]})
    assert writer.created[0]["next_hop"] == "10.9.0.5" and writer.created[0]["exported"] is True


async def test_a_route_left_on_the_previous_device_is_removed() -> None:
    """The attachment moved from bl1 to bl2: bl1's route of this context goes, by ownership and not by device."""
    left = _route(route_name(CTX, "bl1", "PROD", "0.0.0.0/0"), "generated")
    writer = _Writer([_device("bl2", "border-leaf")], [left])
    assert await writer.run(specs_for=writer.specs(snat=None))
    assert left.delete.await_count == 1
    assert [d["capabilities"] for d in writer.created] == [[{"id": "id-bl2"}]]


async def test_a_renamed_context_keeps_ownership_of_its_routes() -> None:
    """Routes named after the old context name are still the context's (maintained_by is the context itself) and are replaced."""
    old = _route(route_name("old-name", "bl1", "PROD", "0.0.0.0/0"), "generated")
    writer = _Writer([_device("bl1", "border-leaf")], [old])
    assert await writer.run(specs_for=writer.specs(snat=None))
    assert old.delete.await_count == 1 and writer.created[0]["maintained_by"] == {"id": "ctx-1"}


async def test_a_device_in_no_vrf_instance_fails_the_reconcile_and_deletes_nothing() -> None:
    """The instance follows the legs; without it the route has no VRF to sit in, which is reported, not skipped."""
    old = _route(route_name(CTX, "bl1", "INTERNET", "203.0.113.6/32"), "generated")
    writer = _Writer([_device("bl1", "border-leaf")], [old])
    writer.vrf_namespaces = []
    assert not await writer.run(specs_for=writer.specs())
    assert old.delete.await_count == 0 and writer.log.error.called


async def test_routes_are_placed_in_the_vrf_instance_of_the_device() -> None:
    """The route points at the ManagedVRF of its device and namespace."""
    writer = _Writer([_device("bl1", "border-leaf")])
    await writer.run(specs_for=writer.specs())
    assert {d["vrf"]["id"] for d in writer.created} == {"vrf-id-bl1-PROD", "vrf-id-bl1-INTERNET"}
