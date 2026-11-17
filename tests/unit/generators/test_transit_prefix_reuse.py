"""Addressed service legs retain their explicit pool-compatible prefixes without fresh allocation
(generators/customers/firewall_transit.py ``existing_transit_prefix``, the plan of generators/customers/context_plan.py)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk import InfrahubClient
from infrahub_sdk.config import Config
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.node import InfrahubNode

from generators.customers.firewall_transit import existing_transit_prefix


def _fixture() -> tuple[Any, dict[str, Any], SimpleNamespace]:
    """A real-shaped addressed pair with an explicit prefix parent and IPv4 pool resource."""
    prefix = SimpleNamespace(
        id="prefix",
        prefix=SimpleNamespace(value="100.66.0.40/29"),
        ip_namespace=SimpleNamespace(id="default", peer=SimpleNamespace(name=SimpleNamespace(value="default"))),
        member_type=SimpleNamespace(value="address"),
    )
    members = [
        SimpleNamespace(
            device_id=f"fw-{i}", offset=i + 4, trunk=SimpleNamespace(id="trunk", name=SimpleNamespace(value="eth1/1"))
        )
        for i in (1, 2)
    ]
    interfaces = [
        SimpleNamespace(
            id=f"leg-{m.device_id}",
            name=SimpleNamespace(value=f"eth1/1.{3001}"),
            device=SimpleNamespace(id=m.device_id),
            ip_address=SimpleNamespace(id=f"ip-{m.offset}"),
            parent=SimpleNamespace(id="trunk"),  # each leg rides on the trunk with the transit tag
            vlan_id=SimpleNamespace(value=3001),
        )
        for m in members
    ]
    # The legs' VRF instance with its zone: it lists the legs in its capabilities (the interface edge).
    vrfs = {
        "vrf-prod": SimpleNamespace(
            id="vrf-prod",
            security_zone=SimpleNamespace(id="prod"),
            capabilities=SimpleNamespace(peers=[SimpleNamespace(id=leg.id) for leg in interfaces]),
        )
    }
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

    async def filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        """Answer each read by its kind, so the order of the reads is free."""
        name = getattr(kind, "__name__", str(kind))
        if name == "DcimVirtualInterface":
            return interfaces
        if name == "ManagedVRF":
            wanted = set(kwargs["capabilities__ids"])
            return [vrf for vrf in vrfs.values() if wanted & {peer.id for peer in vrf.capabilities.peers}]
        if name == "IpamIPAddress":
            return addresses
        return [resource]

    client.filters = AsyncMock(side_effect=filters)
    client.get = AsyncMock(return_value=prefix)
    client.allocate_next_ip_prefix = AsyncMock()
    gen = SimpleNamespace(client=client, logger=MagicMock())
    args = {
        "namespace": SimpleNamespace(id="prod", name=SimpleNamespace(value="PROD")),
        "pool": SimpleNamespace(resources=SimpleNamespace(peers=[SimpleNamespace(id="resource")])),
        "members": members,
        "vlan_id": 3001,
        "leg_type": "prod",
    }
    return (
        gen,
        args,
        SimpleNamespace(
            prefix=prefix,
            addresses=addresses,
            interfaces=interfaces,
            vrfs=vrfs,
            resource=resource,
            logger=gen.logger,
        ),
    )


@pytest.mark.asyncio
async def test_authored_leg_retains_its_prefix_without_allocator_identifier() -> None:
    """No allocator mutation or prefix rewrite is needed: the leg's prefix, namespace and the addresses it has are
    what the plan upserts."""
    gen, args, model = _fixture()

    result = await existing_transit_prefix(gen, **args)

    assert result is not None
    assert (result.network, result.namespace_id, result.namespace_name) == ("100.66.0.40/29", "default", "default")
    assert dict(result.address_ids) == {5: "ip-5", 6: "ip-6"}
    gen.client.allocate_next_ip_prefix.assert_not_awaited()


@pytest.mark.parametrize(
    "fault", ["length", "namespace", "zone", "member_type", "pool", "offset", "split", "missing_parent"]
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
        model.vrfs["vrf-prod"].security_zone.id = "internet"
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
        await existing_transit_prefix(gen, **args)
    gen.client.allocate_next_ip_prefix.assert_not_awaited()
    model.logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_a_leg_in_no_vrf_yet_keeps_its_prefix() -> None:
    """A leg's VRF edge is written after its save: one in no instance yet is healed by the run, not rejected."""
    gen, args, model = _fixture()
    model.vrfs["vrf-prod"].capabilities.peers.pop(0)
    result = await existing_transit_prefix(gen, **args)

    assert result is not None and result.network == "100.66.0.40/29"
    gen.client.allocate_next_ip_prefix.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_prefix_read_failure_propagates_before_any_allocation() -> None:
    """A failed new preflight must abort tracking, not become a successful empty leg."""
    gen, args, model = _fixture()
    gen.client.filters = AsyncMock(side_effect=RuntimeError("prefix read failed"))
    with pytest.raises(RuntimeError, match="prefix read failed"):
        await existing_transit_prefix(gen, **args)
    gen.client.allocate_next_ip_prefix.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unaddressed_leg_has_no_prefix_yet() -> None:
    """A new context takes the production pool allocation path: its plan allocates the leg (test_context_plan)."""
    gen, args, _ = _fixture()
    gen.client.filters = AsyncMock(return_value=[])

    assert await existing_transit_prefix(gen, **args) is None
    gen.client.allocate_next_ip_prefix.assert_not_awaited()


class _GuardGenerator(InfrahubGenerator):
    """Exercise the actual installed SDK run/exit path with the production prefix guard."""

    args: dict[str, Any]

    async def generate(self, data: dict[str, Any]) -> None:
        """An invalid existing leg must raise before a successful tracking exit."""
        await existing_transit_prefix(self, **self.args)


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
    with pytest.raises(ValueError, match="Existing transit prefix"):
        await gen.run("failure-proof", data={"unused": {}})
    cleanup.assert_not_awaited()
    assert prior_outputs == {"vm-1", "vm-2", "ha", "context", "address"}
