"""add_provider_connection (generators/topology/provider_connection.py) on a recording fake client.

The run writes the per-link session circuits and their end interfaces, tracks what it owns, and is idempotent by
edges: a rerun over what it wrote saves nothing. Every refusal happens before the first write.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.logger import GeneratorError
from tests.unit import provider_connection_helpers as h
from tests.unit.provider_connection_helpers import dual_link_profile  # noqa: F401


async def _run(payload: dict[str, Any], client: h.FakeClient | None = None) -> h.FakeClient:
    """Run the generator once over a payload; return the client for assertions."""
    client = client or h.FakeClient()
    await h.make_generator(client).generate(payload)
    return client


pytestmark = pytest.mark.usefixtures("dual_link_profile")


def _circuits(client: h.FakeClient) -> list[dict[str, Any]]:
    """The data of every TopologySessionCircuit saved."""
    return [data["data"] for kind, data in client.of("save", "TopologySessionCircuit")]


class TestDedicatedCreation:
    @pytest.mark.asyncio
    async def test_a_new_connection_gets_its_sub_interfaces_and_one_circuit_with_both_ends(self) -> None:
        client = await _run(h.connection())

        subs = client.of("create", "DcimVirtualInterface")
        assert [data["name"] for _, data in subs] == ["Ethernet1/1.2", "et-0/0/0.2"]
        (circuit,) = _circuits(client)
        assert circuit["name"] == "AWS-FR2-L1"
        assert circuit["circuit_type"] == "handoff"
        assert circuit["connection"] == {"id": h.CONNECTION_ID}
        assert circuit["provider"] == {"id": h.PROVIDER}
        assert circuit["ip_namespace"] == {"id": h.NAMESPACE}
        assert circuit["locations"] == [{"id": h.NEAR}, {"id": h.FAR}]
        assert len(circuit["capabilities"]) == 2
        assert circuit["member_of_groups"] == [{"hfid": ["virtual_circuits"], "kind": "CoreStandardGroup"}]
        assert "link_prefix" not in circuit and "id" not in circuit

    @pytest.mark.asyncio
    async def test_the_connections_password_is_put_on_every_circuit(self) -> None:
        client = await _run(h.connection(password="key-1"))

        (circuit,) = _circuits(client)
        assert circuit["password"] == {"id": "key-1"}

    @pytest.mark.asyncio
    async def test_a_connection_without_a_password_sets_none(self) -> None:
        client = await _run(h.connection())

        assert "password" not in _circuits(client)[0]

    @pytest.mark.asyncio
    async def test_the_run_tracks_the_connection_its_interfaces_and_its_circuit(self) -> None:
        client = await _run(h.connection())

        claimed = client.group_context.related_node_ids
        assert claimed[0] == h.CONNECTION_ID
        assert len(claimed) == 4  # connection, 2 sub-interfaces, circuit

    @pytest.mark.asyncio
    async def test_the_chosen_vlan_is_written_back_untracked_and_last(self) -> None:
        client = await _run(h.connection())

        writes = client.of("save")
        kind, data = writes[-1]
        assert kind == "TopologyProviderConnection"
        assert data["data"] == {"id": h.CONNECTION_ID, "vlan_id": 2}
        assert data["update_group_context"] is False

    @pytest.mark.asyncio
    async def test_a_requested_vlan_is_used_and_not_written_back(self) -> None:
        client = await _run(h.connection(vlan_id=1100))

        names = [data["name"] for _, data in client.of("create", "DcimVirtualInterface")]
        assert names == ["Ethernet1/1.1100", "et-0/0/0.1100"]
        assert client.of("save", "TopologyProviderConnection") == []

    @pytest.mark.asyncio
    async def test_the_connection_status_is_the_circuits(self) -> None:
        client = await _run(h.connection(status="decommissioned", vlan_id=100))

        assert _circuits(client)[0]["status"] == "decommissioned"

    @pytest.mark.asyncio
    async def test_two_links_get_two_circuits_on_distinct_ports(self) -> None:
        payload = h.connection(
            profile="dual",
            vlan_id=300,
            carriers=[h.aws_cross_connect(1), h.aws_cross_connect(2)],
            link_prefixes=[h.prefix("p-2", "10.9.0.4/30"), h.prefix("p-1", "10.9.0.0/30")],
        )

        client = await _run(payload)

        circuits = _circuits(client)
        assert [c["name"] for c in circuits] == ["AWS-FR2-L1", "AWS-FR2-L2"]
        assert [c["link_prefix"] for c in circuits] == [{"id": "p-1"}, {"id": "p-2"}]
        assert len({end["id"] for c in circuits for end in c["capabilities"]}) == 4


class TestVlanAllocation:
    @pytest.mark.asyncio
    async def test_the_lowest_free_vlan_of_every_parent_port_is_taken_under_the_port_locks(self) -> None:
        subs = [h.sub("s-3", "Ethernet1/1.2", "near-port-1"), h.sub("s-4", "et-0/0/0.3", "far-port-1")]
        client = h.FakeClient(subs=subs)

        await _run(h.connection(), client)

        names = [data["name"] for _, data in client.of("create", "DcimVirtualInterface")]
        assert names == ["Ethernet1/1.4", "et-0/0/0.4"]
        events = client.events
        first_read = events.index("filters:DcimInterface")
        locks = [e for e in events[:first_read] if e.startswith("lock:")]
        assert locks == ["lock:pc-conn-1", "lock:pc-port-far-port-1", "lock:pc-port-near-port-1"]
        assert events[-1] == "unlock:pc-conn-1"

    @pytest.mark.asyncio
    async def test_no_lock_of_add_virtual_circuit_is_taken(self) -> None:
        client = await _run(h.connection())

        assert not [e for e in client.events if e.startswith("lock:vc-")]

    @pytest.mark.asyncio
    async def test_a_vlan_that_is_an_end_of_another_session_is_refused_before_any_write(self) -> None:
        subs = [h.sub("s-other", "Ethernet1/1.1100", "near-port-1")]
        client = h.FakeClient(subs=subs)
        client.foreign = [SimpleNamespace(id="other", name=SimpleNamespace(value="OTHER-L1"))]

        with pytest.raises(GeneratorError, match="OTHER-L1"):
            await _run(h.connection(vlan_id=1100), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_an_existing_interface_of_its_own_circuit_is_reused(self) -> None:
        subs = [h.sub("s-near", "Ethernet1/1.1100", "near-port-1"), h.sub("s-far", "et-0/0/0.1100", "far-port-1")]
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "eg-1"), h.end("s-far", "et-0/0/0.1100", "EXT-STUB-1")],
        )
        client = h.FakeClient(subs=subs)

        await _run(h.connection(vlan_id=1100, sessions=[existing]), client)

        assert client.of("create", "DcimVirtualInterface") == []


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_a_rerun_over_what_it_wrote_saves_nothing_and_tracks_everything(self) -> None:
        subs = [h.sub("s-near", "Ethernet1/1.1100", "near-port-1"), h.sub("s-far", "et-0/0/0.1100", "far-port-1")]
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "eg-1"), h.end("s-far", "et-0/0/0.1100", "EXT-STUB-1")],
            session_id="sess-1",
        )
        client = h.FakeClient(subs=subs)

        await _run(h.connection(vlan_id=1100, sessions=[existing]), client)

        assert client.log == []
        assert set(client.group_context.related_node_ids) == {h.CONNECTION_ID, "s-near", "s-far", "sess-1"}

    @pytest.mark.asyncio
    async def test_the_vlan_of_the_sessions_is_kept_when_the_connection_has_none(self) -> None:
        """A rerun with vlan_id still empty (the write-back not yet seen) does not renumber."""
        subs = [h.sub("s-near", "Ethernet1/1.77", "near-port-1"), h.sub("s-far", "et-0/0/0.77", "far-port-1")]
        existing = h.session(
            "AWS-FR2-L1", ends=[h.end("s-near", "Ethernet1/1.77", "eg-1"), h.end("s-far", "et-0/0/0.77", "EXT-STUB-1")]
        )
        client = h.FakeClient(subs=subs)

        await _run(h.connection(sessions=[existing]), client)

        assert client.of("create", "DcimVirtualInterface") == []
        assert client.of("save", "TopologySessionCircuit") == []
        assert client.of("save", "TopologyProviderConnection")[0][1]["data"]["vlan_id"] == 77

    @pytest.mark.asyncio
    async def test_a_password_added_to_the_connection_rewrites_the_circuit_by_id(self) -> None:
        subs = [h.sub("s-near", "Ethernet1/1.1100", "near-port-1"), h.sub("s-far", "et-0/0/0.1100", "far-port-1")]
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "eg-1"), h.end("s-far", "et-0/0/0.1100", "EXT-STUB-1")],
            session_id="sess-1",
        )
        client = h.FakeClient(subs=subs)

        await _run(h.connection(vlan_id=1100, password="key-1", sessions=[existing]), client)

        (circuit,) = _circuits(client)
        assert circuit["id"] == "sess-1" and circuit["password"] == {"id": "key-1"}

    @pytest.mark.asyncio
    async def test_a_rerun_with_the_password_already_on_the_circuit_saves_nothing(self) -> None:
        subs = [h.sub("s-near", "Ethernet1/1.1100", "near-port-1"), h.sub("s-far", "et-0/0/0.1100", "far-port-1")]
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "eg-1"), h.end("s-far", "et-0/0/0.1100", "EXT-STUB-1")],
            password="key-1",
            session_id="sess-1",
        )
        client = h.FakeClient(subs=subs)

        await _run(h.connection(vlan_id=1100, password="key-1", sessions=[existing]), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_changed_status_rewrites_the_circuit_by_id_only(self) -> None:
        subs = [h.sub("s-near", "Ethernet1/1.1100", "near-port-1"), h.sub("s-far", "et-0/0/0.1100", "far-port-1")]
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "eg-1"), h.end("s-far", "et-0/0/0.1100", "EXT-STUB-1")],
            session_id="sess-1",
        )
        client = h.FakeClient(subs=subs)

        await _run(h.connection(vlan_id=1100, status="down", sessions=[existing]), client)

        (circuit,) = _circuits(client)
        assert circuit["id"] == "sess-1" and circuit["status"] == "down"
        assert client.of("create", "DcimVirtualInterface") == []

    @pytest.mark.asyncio
    async def test_a_changed_path_type_leaves_the_old_interfaces_untracked_for_the_cleanup(self) -> None:
        """Dedicated -> VPN: the old circuit is retyped (same name, by id), its sub-interfaces are no longer
        reached, so the SDK's group cleanup deletes them on this run."""
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "edge-1"), h.end("s-far", "et-0/0/0.1100", "EXT-STUB")],
            session_id="sess-1",
        )
        payload = h.connection(
            path_type="internet_vpn",
            internet=[h.internet_carrier("edge-1")],
            stubs=[h.stub_device()],
            link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
            sessions=[existing],
        )

        client = await _run(payload)

        claimed = set(client.group_context.related_node_ids)
        assert {"s-near", "s-far"}.isdisjoint(claimed)
        l1 = next(c for c in _circuits(client) if c["name"] == "AWS-FR2-L1")
        assert l1["id"] == "sess-1" and l1["circuit_type"] == "vpn"


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_stub_without_its_process_writes_and_tracks_nothing(self) -> None:
        client = h.FakeClient()
        payload = h.connection(carriers=[h.aws_cross_connect(stub_processes=[])])

        with pytest.raises(GeneratorError, match="ManagedBGP"):
            await _run(payload, client)

        assert client.log == []
        assert client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_a_link_prefix_count_mismatch_writes_nothing(self) -> None:
        client = h.FakeClient()
        payload = h.connection(profile="dual", carriers=[h.aws_cross_connect(1), h.aws_cross_connect(2)])

        with pytest.raises(GeneratorError, match="link_prefixes expected"):
            await _run(payload, client)

        assert client.log == [] and client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_fewer_carrier_ports_than_the_flavour_links_writes_nothing(self) -> None:
        client = h.FakeClient()
        payload = h.connection(
            profile="dual", link_prefixes=[h.prefix("a", "10.0.0.0/30"), h.prefix("b", "10.0.0.4/30")]
        )

        with pytest.raises(GeneratorError, match="2 link"):
            await _run(payload, client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_missing_provider_writes_nothing(self) -> None:
        client = h.FakeClient()

        with pytest.raises(GeneratorError, match="no provider"):
            await _run(h.connection(with_provider=False), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_payload_without_the_connection_is_logged_and_ignored(self) -> None:
        client = h.FakeClient()
        gen = h.make_generator(client)

        await gen.generate({})

        gen.logger.error.assert_called_once()
        assert client.log == []


class TestInternetVpn:
    def _payload(self, **kwargs: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "path_type": "internet_vpn",
            "internet": [h.internet_carrier("edge-1")],
            "stubs": [h.stub_device()],
            "link_prefixes": [h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        }
        return h.connection(**{**defaults, **kwargs})

    @pytest.mark.asyncio
    async def test_a_tunnel_pair_per_link_and_vpn_circuits_with_their_prefixes(self) -> None:
        client = await _run(self._payload())

        tunnels = client.of("create", "DcimVirtualInterface")
        assert [(data["name"], data["device"]["id"]) for _, data in tunnels] == [
            ("Tunnel1", "dev-edge-1"),
            ("Tunnel1", "dev-stub"),
            ("Tunnel2", "dev-edge-1"),
            ("Tunnel2", "dev-stub"),
        ]
        # the near tunnel rides on the uplink of the internet carrier; the stub's tunnel has no parent
        assert [data.get("parent_interface") for _, data in tunnels] == [
            {"id": "uplink-edge-1"},
            None,
            {"id": "uplink-edge-1"},
            None,
        ]
        circuits = _circuits(client)
        assert [c["name"] for c in circuits] == ["AWS-FR2-L1", "AWS-FR2-L2"]
        assert {c["circuit_type"] for c in circuits} == {"vpn"}
        assert [c["link_prefix"] for c in circuits] == [{"id": "p-1"}, {"id": "p-2"}]
        assert client.of("save", "TopologyProviderConnection") == []

    @pytest.mark.asyncio
    async def test_taken_tunnel_names_are_skipped_under_the_device_locks(self) -> None:
        client = h.FakeClient(taken_tunnels={"dev-edge-1": ["Tunnel1"], "dev-stub": ["Tunnel1", "Tunnel2"]})

        await _run(self._payload(), client)

        names = [(data["device"]["id"], data["name"]) for _, data in client.of("create", "DcimVirtualInterface")]
        assert names == [
            ("dev-edge-1", "Tunnel2"),
            ("dev-stub", "Tunnel3"),
            ("dev-edge-1", "Tunnel3"),
            ("dev-stub", "Tunnel4"),
        ]
        locks = [e for e in client.events if e.startswith("lock:")]
        assert locks == ["lock:pc-conn-1", "lock:pc-device-dev-edge-1", "lock:pc-device-dev-stub"]

    @pytest.mark.asyncio
    async def test_a_rerun_keeps_its_tunnels_and_saves_nothing(self) -> None:
        sessions = [
            h.session(
                f"AWS-FR2-L{n}",
                circuit_type="vpn",
                ends=[h.end(f"t-near-{n}", f"Tunnel{n}", "edge-1"), h.end(f"t-far-{n}", f"Tunnel{n}", "stub")],
                link_prefix=f"p-{n}",
                session_id=f"sess-{n}",
            )
            for n in (1, 2)
        ]
        client = h.FakeClient(taken_tunnels={"dev-edge-1": ["Tunnel1", "Tunnel2"], "dev-stub": ["Tunnel1", "Tunnel2"]})

        await _run(self._payload(sessions=sessions), client)

        assert client.log == []
        assert {"t-near-1", "t-far-2", "sess-1", "sess-2"} <= set(client.group_context.related_node_ids)

    @pytest.mark.asyncio
    async def test_two_near_devices_write_nothing(self) -> None:
        client = h.FakeClient()
        payload = self._payload(internet=[h.internet_carrier("edge-1"), h.internet_carrier("edge-2")])

        with pytest.raises(GeneratorError, match="exactly one device"):
            await _run(payload, client)

        assert client.log == []
