"""add_provider_connection (generators/topology/provider_connection.py) on a recording fake client.

The run writes the per-link session circuits and their end interfaces (and, from the intent alone, the carrier: see
test_provider_connection_carrier.py), tracks what it owns, and is idempotent by edges: a rerun over what it wrote saves
nothing. Every refusal happens before the first write.
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


NEAR_PORT = h.pid("eg-1", "Ethernet1/1")
FAR_PORT = h.pid("AWS-SE", "vnic-AWS-FR2")


def _existing(vlan: int = 1100, *, client: h.FakeClient | None = None, **session: Any) -> tuple[h.FakeClient, Any]:
    """What an earlier run left: the sub-interfaces on the near port and the NIC, and the session circuit."""
    subs = [h.sub("s-near", f"Ethernet1/1.{vlan}", NEAR_PORT), h.sub("s-far", f"vnic-AWS-FR2.{vlan}", FAR_PORT)]
    ends = [h.end("s-near", f"Ethernet1/1.{vlan}", "eg-1"), h.end("s-far", f"vnic-AWS-FR2.{vlan}", "AWS-SE")]
    client = client or h.FakeClient()
    client.subs = subs
    client.add_far_end()
    return client, h.session("AWS-FR2-L1", ends=ends, session_id="sess-1", **session)


def _rerun_payload(vlan: int = 1100, session: Any = None, **kwargs: Any) -> dict[str, Any]:
    """The connection after a run: it lists its own carrier and has its session."""
    return h.connection(vlan_id=vlan, carriers=[h.own_carrier()], sessions=[session] if session else [], **kwargs)


class TestDedicatedCreation:
    @pytest.mark.asyncio
    async def test_a_new_connection_gets_its_sub_interfaces_and_one_circuit_with_both_ends(self) -> None:
        client = await _run(h.connection())

        subs = client.of("create", "DcimVirtualInterface")
        # the first node created is the service-edge NIC, then the two sub-interfaces
        assert [data["name"] for _, data in subs] == ["vnic-AWS-FR2", "Ethernet1/1.2", "vnic-AWS-FR2.2"]
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
        assert len(claimed) == 4  # connection, 2 sub-interfaces, circuit (the carrier and the NIC are shared underlay)

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
        assert names == ["vnic-AWS-FR2", "Ethernet1/1.1100", "vnic-AWS-FR2.1100"]
        assert client.of("save", "TopologyProviderConnection") == []

    @pytest.mark.asyncio
    async def test_the_connection_status_is_the_circuits(self) -> None:
        client = await _run(h.connection(status="decommissioned", vlan_id=100))

        assert _circuits(client)[0]["status"] == "decommissioned"


class TestVlanAllocation:
    @pytest.mark.asyncio
    async def test_the_lowest_free_vlan_of_every_parent_port_is_taken_under_the_port_locks(self) -> None:
        client = h.FakeClient(subs=[h.sub("s-3", "Ethernet1/1.2", NEAR_PORT), h.sub("s-4", "vnic-AWS-FR2.3", FAR_PORT)])
        client.add_far_end()

        await _run(h.connection(carriers=[h.own_carrier()]), client)

        names = [data["name"] for _, data in client.of("create", "DcimVirtualInterface")]
        assert names == ["Ethernet1/1.4", "vnic-AWS-FR2.4"]
        events = client.events
        first_read = events.index("filters:DcimInterface")
        locks = [e for e in events[:first_read] if e.startswith("lock:")]
        assert locks == [
            f"lock:pc-near-{h.NEAR}",
            "lock:pc-conn-1",
            f"lock:pc-port-{FAR_PORT}",
            f"lock:pc-port-{NEAR_PORT}",
        ]
        assert events[-2:] == ["unlock:pc-conn-1", f"unlock:pc-near-{h.NEAR}"]

    @pytest.mark.asyncio
    async def test_no_lock_of_add_virtual_circuit_is_taken(self) -> None:
        client = await _run(h.connection())

        assert not [e for e in client.events if e.startswith("lock:vc-")]

    @pytest.mark.asyncio
    async def test_a_vlan_that_is_an_end_of_another_session_is_refused_before_any_write(self) -> None:
        client = h.FakeClient(subs=[h.sub("s-other", "Ethernet1/1.1100", NEAR_PORT)])
        client.add_far_end()
        client.foreign = [SimpleNamespace(id="other", name=SimpleNamespace(value="OTHER-L1"))]

        with pytest.raises(GeneratorError, match="OTHER-L1"):
            await _run(h.connection(vlan_id=1100, carriers=[h.own_carrier()]), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_an_existing_interface_of_its_own_circuit_is_reused(self) -> None:
        client, existing = _existing()

        await _run(_rerun_payload(session=existing), client)

        assert client.of("create", "DcimVirtualInterface") == []


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_a_rerun_over_what_it_wrote_saves_nothing_and_tracks_everything(self) -> None:
        client, existing = _existing()

        await _run(_rerun_payload(session=existing), client)

        assert client.log == []
        assert set(client.group_context.related_node_ids) == {h.CONNECTION_ID, "s-near", "s-far", "sess-1"}

    @pytest.mark.asyncio
    async def test_the_vlan_of_the_sessions_is_kept_when_the_connection_has_none(self) -> None:
        """A rerun with vlan_id still empty (the write-back not yet seen) does not renumber."""
        client, existing = _existing(77)

        await _run(h.connection(carriers=[h.own_carrier()], sessions=[existing]), client)

        assert client.of("create", "DcimVirtualInterface") == []
        assert client.of("save", "TopologySessionCircuit") == []
        assert client.of("save", "TopologyProviderConnection")[0][1]["data"]["vlan_id"] == 77

    @pytest.mark.asyncio
    async def test_a_password_added_to_the_connection_rewrites_the_circuit_by_id(self) -> None:
        client, existing = _existing()

        await _run(_rerun_payload(session=existing, password="key-1"), client)

        (circuit,) = _circuits(client)
        assert circuit["id"] == "sess-1" and circuit["password"] == {"id": "key-1"}

    @pytest.mark.asyncio
    async def test_a_rerun_with_the_password_already_on_the_circuit_saves_nothing(self) -> None:
        client, existing = _existing(password="key-1")

        await _run(_rerun_payload(session=existing, password="key-1"), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_changed_status_rewrites_the_circuit_by_id_only(self) -> None:
        client, existing = _existing()

        await _run(_rerun_payload(session=existing, status="down"), client)

        (circuit,) = _circuits(client)
        assert circuit["id"] == "sess-1" and circuit["status"] == "down"
        assert client.of("create", "DcimVirtualInterface") == []

    @pytest.mark.asyncio
    async def test_a_changed_path_type_leaves_the_old_interfaces_untracked_for_the_cleanup(self) -> None:
        """Dedicated -> VPN: the old circuit is retyped (same name, by id), its sub-interfaces are no longer
        reached, so the SDK's group cleanup deletes them on this run."""
        existing = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "edge-1"), h.end("s-far", "vnic-AWS-FR2.1100", "AWS-SE")],
            session_id="sess-1",
        )
        payload = h.connection(
            path_type="internet_vpn",
            internet=[h.internet_carrier("edge-1")],
            link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
            sessions=[existing],
        )

        client = await _run(payload)

        claimed = set(client.group_context.related_node_ids)
        assert {"s-near", "s-far"}.isdisjoint(claimed)
        l1 = next(c for c in _circuits(client) if c["name"] == "AWS-FR2-L1")
        assert l1["id"] == "sess-1" and l1["circuit_type"] == "vpn"

    @pytest.mark.asyncio
    async def test_the_reverse_change_vpn_to_dedicated_creates_the_carrier_and_retypes_the_circuit(self) -> None:
        existing = h.session(
            "AWS-FR2-L1",
            circuit_type="vpn",
            ends=[h.end("t-near", "Tunnel1", "edge-1"), h.end("t-far", "Tunnel1", "AWS-SE")],
            session_id="sess-1",
        )

        client = await _run(h.connection(vlan_id=100, sessions=[existing]))

        assert len(client.of("create", "TopologyCarrierCircuit")) == 1
        l1 = next(c for c in _circuits(client) if c["name"] == "AWS-FR2-L1")
        assert l1["id"] == "sess-1" and l1["circuit_type"] == "handoff"
        assert {"t-near", "t-far"}.isdisjoint(set(client.group_context.related_node_ids))


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_stub_without_its_process_writes_and_tracks_nothing(self) -> None:
        client = h.FakeClient()
        payload = h.connection(stubs=[h.stub_device(processes=[])])

        with pytest.raises(GeneratorError, match="ManagedBGP"):
            await _run(payload, client)

        assert client.log == []
        assert client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_a_link_prefix_count_mismatch_writes_nothing(self) -> None:
        client = h.FakeClient()
        payload = h.connection(profile="dual", path_type="internet_vpn", internet=[h.internet_carrier("edge-1")])

        with pytest.raises(GeneratorError, match="link_prefixes expected"):
            await _run(payload, client)

        assert client.log == [] and client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_a_two_link_dedicated_flavour_writes_nothing(self) -> None:
        client = h.FakeClient()

        with pytest.raises(GeneratorError, match="one leg"):
            await _run(h.connection(profile="dual", link_prefixes=[h.prefix("a", "10.0.0.0/30")]), client)

        assert client.log == [] and client.events == []

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
            "link_prefixes": [h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        }
        return h.connection(**{**defaults, **kwargs})

    @pytest.mark.asyncio
    async def test_a_tunnel_pair_per_link_and_vpn_circuits_with_their_prefixes(self) -> None:
        client = await _run(self._payload())

        tunnels = client.of("create", "DcimVirtualInterface")
        assert [(data["name"], data["device"]["id"]) for _, data in tunnels] == [
            ("Tunnel1", "dev-edge-1"),
            ("Tunnel1", "dev-AWS-SE"),
            ("Tunnel2", "dev-edge-1"),
            ("Tunnel2", "dev-AWS-SE"),
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
    async def test_a_vpn_creates_no_carrier_takes_no_near_lock_and_selects_no_port(self) -> None:
        client = await _run(self._payload())

        assert client.of("create", "TopologyCarrierCircuit") == []
        assert not [e for e in client.events if e.startswith(("lock:pc-near", "filters:DcimPhysicalInterface"))]

    @pytest.mark.asyncio
    async def test_taken_tunnel_names_are_skipped_under_the_device_locks(self) -> None:
        client = h.FakeClient(taken_tunnels={"dev-edge-1": ["Tunnel1"], "dev-AWS-SE": ["Tunnel1", "Tunnel2"]})

        await _run(self._payload(), client)

        names = [(data["device"]["id"], data["name"]) for _, data in client.of("create", "DcimVirtualInterface")]
        assert names == [
            ("dev-edge-1", "Tunnel2"),
            ("dev-AWS-SE", "Tunnel3"),
            ("dev-edge-1", "Tunnel3"),
            ("dev-AWS-SE", "Tunnel4"),
        ]
        locks = [e for e in client.events if e.startswith("lock:")]
        assert locks == ["lock:pc-conn-1", "lock:pc-device-dev-AWS-SE", "lock:pc-device-dev-edge-1"]

    @pytest.mark.asyncio
    async def test_a_rerun_keeps_its_tunnels_and_saves_nothing(self) -> None:
        sessions = [
            h.session(
                f"AWS-FR2-L{n}",
                circuit_type="vpn",
                ends=[h.end(f"t-near-{n}", f"Tunnel{n}", "edge-1"), h.end(f"t-far-{n}", f"Tunnel{n}", "AWS-SE")],
                link_prefix=f"p-{n}",
                session_id=f"sess-{n}",
            )
            for n in (1, 2)
        ]
        client = h.FakeClient(
            taken_tunnels={"dev-edge-1": ["Tunnel1", "Tunnel2"], "dev-AWS-SE": ["Tunnel1", "Tunnel2"]}
        )

        await _run(self._payload(sessions=sessions), client)

        assert client.log == []
        assert {"t-near-1", "t-far-2", "sess-1", "sess-2"} <= set(client.group_context.related_node_ids)

    @pytest.mark.asyncio
    async def test_a_carrier_the_connection_still_lists_is_cleared_and_not_deleted(self) -> None:
        client = await _run(self._payload(carriers=[h.own_carrier()]))

        ((_, data),) = client.of("save", "TopologyProviderConnection")
        assert data["data"] == {"id": h.CONNECTION_ID, "carrier_circuits": []}
        assert data["update_group_context"] is False
        assert client.of("create", "TopologyCarrierCircuit") == []

    @pytest.mark.asyncio
    async def test_two_near_devices_write_nothing(self) -> None:
        client = h.FakeClient()
        payload = self._payload(internet=[h.internet_carrier("edge-1"), h.internet_carrier("edge-2")])

        with pytest.raises(GeneratorError, match="exactly one device"):
            await _run(payload, client)

        assert client.log == []
