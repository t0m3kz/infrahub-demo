"""add_mlag (generators/capabilities/mlag.py): the wiring of a domain that exists, planned from what it reads and applied
on the HFID fake: the objects it leaves, what it does not track, and a re-run."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.capabilities.mlag import MLAGGenerator
from generators.model.errors import GeneratorError
from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator
from utils.limits import MLAG_CONTROL_IPV4_POOL, MLAG_CONTROL_IPV6_POOL

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TYPE = "100gbase-x-qsfp28"
_NAME = "leaf-01-leaf-02-mlag"


def _estate(*, virtual: bool = False, platform: str = "sonic") -> tuple[HfidClient, HfidNode, dict[str, HfidNode]]:
    """Two SONiC leafs (an IPv4 control session, as the fake allocates) with two peer ports each and the domain that pairs them, all written by the rack's run."""
    client = HfidClient()
    platform_node = client.seed("DcimPlatform", name=platform)
    devices: dict[str, HfidNode] = {}
    for name in ("leaf-01", "leaf-02"):
        device = client.seed("DcimPhysicalDevice", name=name, platform={"id": platform_node.id})
        devices[name] = device
        for port in ("Eth1/49", "Eth1/50"):
            client.seed(
                "DcimPhysicalInterface",
                name=port,
                device={"id": device.id},
                role="mlag-peer",
                interface_type=_TYPE,
                status="free",
            )
    domain = client.seed(
        "ManagedMLAG",
        name=_NAME,
        virtual_peer_link=virtual,
        capabilities=[{"id": node.id} for node in devices.values()],
        interfaces=[],
    )
    client.seed("CoreIPPrefixPool", name=MLAG_CONTROL_IPV4_POOL)
    client.seed("CoreIPPrefixPool", name=MLAG_CONTROL_IPV6_POOL)
    return client, domain, devices


def _gen(client: HfidClient) -> Any:
    return make_hfid_generator(client, cls=MLAGGenerator)


def _of_kind(client: HfidClient, kind: str) -> list[Any]:
    return [node for node in client.store.values() if node.kind == kind]


def _snapshot(client: HfidClient) -> dict[str, dict[str, Any]]:
    """What is stored, without the ``id`` a second run sends to update what it found and without the addresses (the fake
    stores them bare and a second run sets their description)."""
    return {
        node_id: {key: value for key, value in node.data.items() if key != "id"}
        for node_id, node in client.store.items()
        if node.kind != "IpamIPAddress"
    }


def _payload(domain: HfidNode) -> dict[str, Any]:
    return {"ManagedMLAG": [{"id": domain.id, "name": {"value": _NAME}}]}


class TestGenerate:
    @pytest.mark.asyncio
    async def test_no_mlag_data_logs_error_and_returns(self) -> None:
        gen = _gen(HfidClient())

        await gen.generate({"ManagedMLAG": []})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_physical_domain_gets_a_lag_per_member_a_cable_per_port_pair_and_the_control_svis(self) -> None:
        client, domain, _ = _estate()
        gen = _gen(client)

        await gen.generate(_payload(domain))

        assert sorted(node.data["name"] for node in _of_kind(client, "DcimLAGInterface")) == ["PortChannel100"] * 2
        assert sorted(node.data["name"] for node in _of_kind(client, "DcimCable")) == [
            f"CBL-{_NAME}-PL1",
            f"CBL-{_NAME}-PL2",
        ]
        svis = _of_kind(client, "DcimVirtualInterface")
        assert [node.data["name"] for node in svis] == ["Vlan4094"] * 2
        assert all(node.data["ip_address"] for node in svis)
        # the domain lists its ports, its two LAGs and its two control SVIs
        assert len(domain.data["interfaces"]) == 4 + 2 + 2
        # the control prefix is keyed by the domain's id
        assert any(identifier == f"mlag-control__{domain.id}" for _, identifier in client.allocations)

    @pytest.mark.asyncio
    async def test_nothing_of_it_is_tracked_and_the_domain_itself_is_not_written(self) -> None:
        """The wiring belongs to the generator that created the domain: add_mlag writes it untracked."""
        client, domain, _ = _estate()
        gen = _gen(client)

        await gen.generate(_payload(domain))

        by_kind = {kind: kwargs for kind, _, _, kwargs in client.saves if kind != "DcimPhysicalInterface"}
        assert by_kind["DcimLAGInterface"] == _UNTRACKED
        assert by_kind["DcimCable"] == _UNTRACKED
        assert by_kind["DcimVirtualInterface"] == _UNTRACKED
        assert all(kind != "ManagedMLAG" for kind, _, _, _ in client.saves)

    @pytest.mark.asyncio
    async def test_a_rerun_writes_the_same_objects_and_adds_nothing_to_the_domain(self) -> None:
        client, domain, _ = _estate()
        gen = _gen(client)
        await gen.generate(_payload(domain))
        before = _snapshot(client)
        edge_adds = list(client.edge_adds)

        await gen.generate(_payload(domain))

        assert _snapshot(client) == before
        assert client.edge_adds == edge_adds

    @pytest.mark.asyncio
    async def test_a_virtual_domain_gets_loopbacks_and_no_cable_and_the_back_to_back_leftovers_go(self) -> None:
        client, domain, devices = _estate(virtual=True)
        leftovers = [
            client.seed(
                "DcimLAGInterface", name="PortChannel100", device={"id": node.id}, groups=["CoreGeneratorGroup"]
            )
            for node in devices.values()
        ]
        gen = _gen(client)

        await gen.generate(_payload(domain))

        assert sorted(node.data["name"] for node in _of_kind(client, "DcimVirtualInterface")) == ["Loopback100"] * 2
        assert _of_kind(client, "DcimCable") == []
        assert all(node.id not in client.store for node in leftovers)

    @pytest.mark.asyncio
    async def test_a_leftover_no_generator_tracks_fails_the_run_before_anything_is_written(self) -> None:
        client, domain, devices = _estate(virtual=True)
        client.seed("DcimLAGInterface", name="PortChannel100", device={"id": devices["leaf-01"].id})
        gen = _gen(client)

        with pytest.raises(GeneratorError, match="no generator tracks it"):
            await gen.generate(_payload(domain))

        assert [kind for kind, _, _, _ in client.saves if kind != "DcimPhysicalInterface"] == []

    @pytest.mark.asyncio
    async def test_the_domain_is_wired_under_its_lock(self) -> None:
        client, domain, _ = _estate()
        gen = _gen(client)

        await gen.generate(_payload(domain))

        assert gen.locked_keys == [f"mlag-domain-{_NAME}"]

    @pytest.mark.asyncio
    async def test_a_domain_that_does_not_have_two_members_logs_the_error(self) -> None:
        client, domain, devices = _estate()
        domain.data["capabilities"] = [{"id": devices["leaf-01"].id}]
        gen = _gen(client)

        await gen.generate(_payload(domain))

        assert "Expected exactly 2 peer devices" in gen.logger.error.call_args.args[0]
        assert client.saves == []

    @pytest.mark.asyncio
    async def test_a_member_without_peer_ports_fails_the_run_naming_it_and_writes_nothing(self) -> None:
        client, domain, devices = _estate()
        for node in list(client.store.values()):
            if node.kind == "DcimPhysicalInterface" and node.data["device"]["id"] == devices["leaf-02"].id:
                del client.store[node.id]
        gen = _gen(client)
        gen.apply_plan = AsyncMock()

        with pytest.raises(GeneratorError, match=r"\[leaf-02\] No physical interfaces with role=mlag-peer"):
            await gen.generate(_payload(domain))

        gen.apply_plan.assert_not_awaited()
