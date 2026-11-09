"""Addressed service legs retain their explicit pool-compatible prefixes without fresh allocation."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk import InfrahubClient
from infrahub_sdk.config import Config
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.node import InfrahubNode

from generators.firewall_context_spec import FIREWALL_CONTEXT
from generators.firewall_transit import TransitLegsMixin


def _fixture() -> tuple[TransitLegsMixin, dict[str, Any], SimpleNamespace]:
    """A real-shaped addressed pair with an explicit prefix parent and IPv4 pool resource."""
    prefix = SimpleNamespace(
        id="prefix",
        prefix=SimpleNamespace(value="100.66.0.40/29"),
        ip_namespace=SimpleNamespace(id="default", peer=SimpleNamespace(name=SimpleNamespace(value="default"))),
        member_type=SimpleNamespace(value="address"),
    )
    members = [
        SimpleNamespace(device_id=f"fw-{i}", offset=i + 4, trunk=SimpleNamespace(name=SimpleNamespace(value="eth1/1")))
        for i in (1, 2)
    ]
    interfaces = [
        SimpleNamespace(
            device=SimpleNamespace(id=m.device_id),
            ip_address=SimpleNamespace(id=f"ip-{m.offset}"),
            vrf=SimpleNamespace(id="vrf-prod", peer=SimpleNamespace(security_zone=SimpleNamespace(id="prod"))),
        )
        for m in members
    ]
    addresses = [
        SimpleNamespace(
            id=f"ip-{m.offset}",
            address=SimpleNamespace(value=f"100.66.0.{40 + m.offset}/29"),
            ip_prefix=SimpleNamespace(id="prefix"),
        )
        for m in members
    ]
    resource = SimpleNamespace(prefix=SimpleNamespace(value="100.66.0.0/20"))
    client = MagicMock()
    client.filters = AsyncMock(side_effect=[interfaces, addresses, [resource]])
    client.get = AsyncMock(return_value=prefix)
    client.allocate_next_ip_prefix = AsyncMock()
    gen = TransitLegsMixin()
    gen.client = client
    gen.logger = MagicMock()
    gen.upsert_prefix_addresses = AsyncMock(return_value={5: addresses[0], 6: addresses[1]})
    args = {
        "context_obj": SimpleNamespace(id="ctx", name=SimpleNamespace(value="dedicated")),
        "namespace": SimpleNamespace(id="prod", name=SimpleNamespace(value="PROD")),
        "pool": SimpleNamespace(resources=SimpleNamespace(peers=[SimpleNamespace(id="resource")])),
        "members": members,
        "vlan_id": 3001,
        "leg_type": "prod",
        "track": True,
    }
    return (
        gen,
        args,
        SimpleNamespace(
            prefix=prefix,
            addresses=addresses,
            interfaces=interfaces,
            resource=resource,
            upsert=gen.upsert_prefix_addresses,
            logger=gen.logger,
        ),
    )


@pytest.mark.asyncio
async def test_authored_leg_retains_its_prefix_without_allocator_identifier() -> None:
    """No allocator mutation or prefix rewrite is needed; addresses still receive the normal tracked upsert."""
    gen, args, model = _fixture()
    result = await gen._allocate_transit_addresses(FIREWALL_CONTEXT, **args)
    assert result is not None
    gen.client.allocate_next_ip_prefix.assert_not_awaited()
    assert model.upsert.await_args.args[0] is model.prefix
    assert model.upsert.await_args.kwargs["track"] is True


@pytest.mark.parametrize(
    "fault", ["length", "namespace", "zone", "missing_vrf", "member_type", "pool", "offset", "split", "missing_parent"]
)
@pytest.mark.asyncio
async def test_inconsistent_addressed_leg_never_allocates_around_the_error(fault: str) -> None:
    """An existing incompatible leg is rejected, not silently moved into the next free range."""
    gen, args, model = _fixture()
    if fault == "length":
        model.prefix.prefix.value = "100.66.0.32/28"
    elif fault == "namespace":
        model.prefix.ip_namespace.peer.name.value = "foreign"
    elif fault == "zone":
        model.interfaces[0].vrf.peer.security_zone.id = "internet"
    elif fault == "missing_vrf":
        model.interfaces[0].vrf.id = None
    elif fault == "member_type":
        model.prefix.member_type.value = "prefix"
    elif fault == "pool":
        model.resource.prefix.value = "100.67.0.0/20"
    elif fault == "offset":
        model.addresses[0].address.value = "100.66.0.44/29"
    elif fault == "split":
        model.addresses[1].ip_prefix.id = "other"
    else:
        model.addresses[0].ip_prefix.id = None
    with pytest.raises(ValueError, match="Existing transit"):
        await gen._allocate_transit_addresses(FIREWALL_CONTEXT, **args)
    gen.client.allocate_next_ip_prefix.assert_not_awaited()
    model.upsert.assert_not_awaited()
    model.logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_existing_prefix_read_failure_propagates_before_any_allocation() -> None:
    """A failed new preflight must abort tracking, not become a successful empty leg."""
    gen, args, model = _fixture()
    gen.client.filters = AsyncMock(side_effect=RuntimeError("prefix read failed"))
    with pytest.raises(RuntimeError, match="prefix read failed"):
        await gen._allocate_transit_addresses(FIREWALL_CONTEXT, **args)
    gen.client.allocate_next_ip_prefix.assert_not_awaited()
    model.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_unaddressed_leg_uses_the_existing_identifier_based_allocator() -> None:
    """A new context still takes the production pool allocation path."""
    gen, args, model = _fixture()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.allocate_next_ip_prefix.return_value = model.prefix
    assert await gen._allocate_transit_addresses(FIREWALL_CONTEXT, **args) is not None
    assert gen.client.allocate_next_ip_prefix.await_args.kwargs["identifier"] == "ctx-PROD-transit"
    assert gen.client.allocate_next_ip_prefix.await_args.kwargs["data"] == {"role": "technical"}


class _GuardGenerator(TransitLegsMixin, InfrahubGenerator):
    """Exercise the actual installed SDK run/exit path with the production prefix guard."""

    args: dict[str, Any]

    async def generate(self, data: dict[str, Any]) -> None:
        """An invalid existing leg must raise before a successful tracking exit."""
        await self._allocate_transit_addresses(FIREWALL_CONTEXT, **self.args)


@pytest.mark.asyncio
async def test_sdk_full_run_propagates_invalid_prefix_without_cleaning_prior_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary SDK logger does not enforce failure; exceptions, not logging, protect cleanup."""
    fixture, args, model = _fixture()
    model.prefix.member_type.value = "prefix"
    client = InfrahubClient(config=Config(address="http://localhost:8000"))
    gen = _GuardGenerator(query="unused", client=client, infrahub_node=InfrahubNode, branch="isolated")
    client = gen._init_client
    client.filters = fixture.client.filters
    client.get = fixture.client.get
    prior_outputs = {"vm-1", "vm-2", "ha", "context", "address"}
    cleanup = AsyncMock(side_effect=prior_outputs.clear)
    monkeypatch.setattr(type(client.group_context), "update_group", cleanup)
    gen.args = args
    gen.upsert_prefix_addresses = model.upsert
    with pytest.raises(ValueError, match="Existing transit prefix"):
        await gen.run("failure-proof", data={"unused": {}})
    cleanup.assert_not_awaited()
    assert prior_outputs == {"vm-1", "vm-2", "ha", "context", "address"}
