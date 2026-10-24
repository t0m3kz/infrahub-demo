"""Unit tests for the handoff circuit session of the session generator (add_virtual_circuit)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.logger import GeneratorError
from generators.topology.virtual_circuit import handoff_pool_name
from tests.integration.demo_data import BOOTSTRAP, declared
from tests.unit.simulators.circuit_fakes import (
    HANDOFF_POOL_NETWORK,
    VC_ID,
    FakeClient,
    deployment,
    handoff_edge_port,
    handoff_party_port,
    handoff_payload,
    session_port,
)
from tests.unit.virtual_circuit_session_helpers import (
    HANDOFF,
    UNTRACKED,
    _assert_refused,
    _interface_saves,
    _peering,
    _run,
)


class TestHandoffSession:
    @pytest.mark.asyncio
    async def test_allocates_the_namespace_pool_by_circuit_identifier(self) -> None:
        """A /31 (point-to-point IPv4 link) comes from the namespace's Handoff pool, keyed by circuit id for reruns."""
        client = FakeClient()

        await _run(client, handoff_payload())

        (allocation,) = client.allocations
        assert allocation["resource_pool"].pool_name == "Handoff-PROD-IPv4"
        assert allocation["identifier"] == f"vc-p2p__{VC_ID}"
        assert allocation["prefix_length"] == 31
        assert allocation["member_type"] == "address"
        assert allocation["data"] == {"role": "technical", "is_pool": True, "description": f"Handoff {HANDOFF}"}

    @pytest.mark.asyncio
    async def test_the_prefix_joins_the_tracking_group_before_anything_else_is_written(self) -> None:
        """The prefix is tracked right after the allocation, so a crash later still leaves it owned by the cleanup."""
        client = FakeClient()

        await _run(client, handoff_payload())

        assert [action for action, _, _ in client.writes[:2]] == ["allocate", "track"]
        assert f"prefix-vc-p2p__{VC_ID}" in client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_both_ends_are_addressed_ours_first_untracked_with_no_mtu_change(self) -> None:
        """Our end (the device with an overlay process) takes the lower address; interfaces are never claimed."""
        client = FakeClient()

        await _run(client, handoff_payload())

        base = HANDOFF_POOL_NETWORK.split("/")[0]
        assert client.interfaces["if-hs"].ip_address.id == f"IpamIPAddress:{base}/31"
        assert client.interfaces["if-aws"].ip_address.id == "IpamIPAddress:10.255.128.1/31"
        assert client.interfaces["if-hs"].description.value == f"Handoff {HANDOFF} to EXT-AWS-DX-FR2 Ethernet1.1900"
        assert client.interfaces["if-aws"].description.value == f"Handoff {HANDOFF} to eg-fr201 Ethernet1/10.1900"
        assert client.interfaces["if-hs"].status.value == "active"
        assert client.interfaces["if-hs"].mtu.value == 9000
        saves = client.saves()
        assert saves["if-hs"] == UNTRACKED and saves["if-aws"] == UNTRACKED

    @pytest.mark.asyncio
    async def test_one_peering_with_these_exact_attributes(self) -> None:
        """Handoff eBGP: ttl 1 (directly connected /31), BFD on, no communities sent, maximum_routes 100 from the circuit."""
        client = FakeClient()

        await _run(client, handoff_payload(password="pw-1"))

        (peering,) = [data for data, _ in client.created_of("ManagedBGPPeering")]
        assert peering == {
            "name": f"{HANDOFF}-bgp",
            "description": (
                f"Handoff eBGP eg-fr201/Ethernet1/10.1900 <-> EXT-AWS-DX-FR2/Ethernet1.1900 over {HANDOFF}"
            ),
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "regular",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": False,
            "send_extended_community": False,
            "maximum_routes": 100,
            "address_families": [{"id": "af-ipv4-unicast"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "EXT-AWS-DX-FR2-bgp-regular"}],
            "password": {"id": "pw-1"},
            "capabilities": [{"id": "if-hs"}, {"id": "if-aws"}],
        }

    @pytest.mark.asyncio
    async def test_no_password_and_no_maximum_routes_are_not_written(self) -> None:
        """A handoff that states neither a key nor a route limit gets a session without those two fields."""
        client = FakeClient()

        await _run(client, handoff_payload(maximum_routes=None))

        peering = _peering(client, "bgp")
        assert "password" not in peering and "maximum_routes" not in peering
        assert client.created_of("RoutingPassword") == []

    @pytest.mark.asyncio
    async def test_a_circuit_that_is_not_active_is_provisioning(self) -> None:
        """Any status other than active gives the session and its interfaces the provisioning status."""
        client = FakeClient()

        await _run(client, handoff_payload(status="provisioning"))

        assert _peering(client, "bgp")["status"] == "provisioning"
        assert client.interfaces["if-hs"].status.value == "provisioning"

    @pytest.mark.asyncio
    async def test_the_session_runs_on_the_overlay_process_else_the_only_one(self) -> None:
        """Both ends with a single process and none overlay: ordered by device name."""
        client = FakeClient()
        ends = [handoff_edge_port(roles=("regular",)), handoff_party_port()]

        await _run(client, handoff_payload(ends=ends))

        assert _peering(client, "bgp")["bgp_processes"] == [
            {"id": "EXT-AWS-DX-FR2-bgp-regular"},
            {"id": "eg-fr201-bgp-regular"},
        ]

    @pytest.mark.asyncio
    async def test_the_writes_run_under_one_lock_per_circuit(self) -> None:
        """All writes of a circuit sit under a single lock keyed by the circuit id, so two runs cannot interleave."""
        gen = await _run(FakeClient(), handoff_payload())

        assert gen.lock_keys.count(f"vc-session-{VC_ID}") == 1

    @pytest.mark.asyncio
    async def test_claims_prefix_addresses_and_peering_only(self) -> None:
        """Interfaces are written untracked; the global address family is only referenced."""
        client = FakeClient()

        await _run(client, handoff_payload())

        assert client.claimed() == {
            f"prefix-vc-p2p__{VC_ID}",
            "IpamIPAddress:10.255.128.0/31",
            "IpamIPAddress:10.255.128.1/31",
            f"ManagedBGPPeering:{HANDOFF}-bgp",
        }

    @pytest.mark.asyncio
    async def test_a_second_run_allocates_the_same_prefix_and_writes_no_interface_again(self) -> None:
        """Idempotency: the rerun gets the same /31 and the interfaces, already correct, are not saved again."""
        client = FakeClient()
        await _run(client, handoff_payload())
        first_labels = [label for _, label, _ in client.writes]
        first_interface_saves = _interface_saves(client)

        await _run(client, handoff_payload())

        assert [a["identifier"] for a in client.allocations] == [f"vc-p2p__{VC_ID}"] * 2
        assert len(client.prefixes) == 1
        assert _interface_saves(client) == first_interface_saves
        assert {label for _, label, _ in client.writes} == set(first_labels)
        assert len(client.addresses) == 2

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(handoff_payload(namespace=None), r"needs its ip_namespace", id="no-namespace"),
            pytest.param(
                handoff_payload(namespace="NON-PROD"),
                r"pool 'Handoff-NON-PROD-IPv4' not found",
                id="namespace-without-pool",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_namespace_problems_are_refused_before_any_write(self, payload: dict[str, Any], message: str) -> None:
        """A handoff with no namespace, or a namespace that has no Handoff pool, is refused with nothing written."""
        await _assert_refused(payload, message)

    @pytest.mark.parametrize(
        ("party", "message"),
        [
            pytest.param(
                session_port("if-aws", "Ethernet1", "EXT-AWS-DX-FR2", where=deployment("TopologyCloudRegion", "x")),
                r"EXT-AWS-DX-FR2/Ethernet1 must be a DcimVirtualInterface named <parent>.<vlan>",
                id="physical-port",
            ),
            pytest.param(
                session_port("if-aws", "lo0", "EXT-AWS-DX-FR2", kind="DcimVirtualInterface"),
                r"EXT-AWS-DX-FR2/lo0 must be a DcimVirtualInterface named <parent>.<vlan>",
                id="no-vlan-in-the-name",
            ),
            pytest.param(
                handoff_party_port(roles=("underlay", "regular")),
                r"EXT-AWS-DX-FR2 needs one overlay ManagedBGP process, or only one ManagedBGP process; has: "
                r"EXT-AWS-DX-FR2-bgp-underlay, EXT-AWS-DX-FR2-bgp-regular",
                id="two-processes-no-overlay",
            ),
            pytest.param(handoff_party_port(roles=()), r"has: none", id="no-process"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_bad_end_is_refused_before_any_write(self, party: dict[str, Any], message: str) -> None:
        """A party end that is no VLAN sub-interface, or a device without a usable BGP process, is refused up front."""
        await _assert_refused(handoff_payload(ends=[handoff_edge_port(), party]), message)

    @pytest.mark.asyncio
    async def test_an_interface_that_cannot_be_fetched_is_refused_before_the_allocation(self) -> None:
        """A missing end interface fails before the pool is touched, so no prefix is left allocated."""
        client = FakeClient()

        async def _only_one(kind: Any, **kwargs: Any) -> list[Any]:
            return [client.interface_node("if-hs")] if "ids" in kwargs else []

        setattr(client, "filters", _only_one)

        with pytest.raises(GeneratorError, match=r"interface\(s\) EXT-AWS-DX-FR2/Ethernet1.1900 not found"):
            await _run(client, handoff_payload())

        assert client.allocations == []

    @pytest.mark.asyncio
    async def test_an_exhausted_pool_fails_before_any_other_write(self) -> None:
        """A pool that returns no prefix aborts the run with nothing else written."""
        client = FakeClient()

        async def _none(**kwargs: Any) -> None:
            return None

        setattr(client, "allocate_next_ip_prefix", _none)

        with pytest.raises(GeneratorError, match="returned no prefix"):
            await _run(client, handoff_payload())

        assert client.writes == []


class TestHandoffPoolData:
    """data/bootstrap/26_handoff_pools.yml: one /31 pool per environment namespace, named as the generator expects."""

    @staticmethod
    def _rows(kind: str, name: str) -> list[dict[str, Any]]:
        """The top-level entries of `kind` a data/bootstrap file declares (nested objects are not rows)."""
        return [obj.item for obj in declared(kind, BOOTSTRAP / name) if obj.enclosing is None]

    def test_every_namespace_has_its_pool_in_that_namespace(self) -> None:
        """Each namespace has one Handoff pool in it, handing out /31 prefixes (point-to-point IPv4 link)."""
        namespaces = [row["name"] for row in self._rows("IpamNamespace", "22_namespaces.yml")]
        pools = {row["name"]: row for row in self._rows("CoreIPPrefixPool", "26_handoff_pools.yml")}

        assert sorted(pools) == sorted(handoff_pool_name(ns) for ns in namespaces)
        for namespace in namespaces:
            pool = pools[handoff_pool_name(namespace)]
            assert pool["ip_namespace"] == namespace
            assert pool["default_prefix_length"] == 31
            assert [resource["ip_namespace"] for resource in pool["resources"]["data"]] == [namespace]
            assert [resource["member_type"] for resource in pool["resources"]["data"]] == ["prefix"]

    def test_the_pool_prefixes_do_not_overlap_each_other_or_the_authored_handoffs(self) -> None:
        """The pool prefixes overlap neither each other nor 10.255.5.0/24, where the hand-authored handoffs sit."""
        import ipaddress

        networks = [
            ipaddress.ip_network(resource["prefix"])
            for row in self._rows("CoreIPPrefixPool", "26_handoff_pools.yml")
            for resource in row["resources"]["data"]
        ]
        authored = [ipaddress.ip_network("10.255.5.0/24")]

        assert len(networks) == 4
        for index, network in enumerate(networks):
            assert not any(network.overlaps(other) for other in [*networks[:index], *networks[index + 1 :], *authored])
