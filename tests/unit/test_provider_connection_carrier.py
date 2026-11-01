"""add_provider_connection from the intent alone: the near port, the far NIC and the carrier (generators/helpers/near_port*.py).

The request names the facilities; the run picks the near port (explicit, else the one its own carrier ends on, else a
free port of the right role on the least loaded device), reuses a carrier that already ends there or creates one, and
writes nothing at all when it must refuse.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.logger import GeneratorError
from tests.unit import provider_connection_helpers as h

FAR_DEVICE = h.SERVICE_EDGE[0]


async def _run(payload: dict[str, Any], client: h.FakeClient | None = None) -> h.FakeClient:
    client = client or h.FakeClient()
    await h.make_generator(client).generate(payload)
    return client


def _carrier_data(client: h.FakeClient) -> dict[str, Any]:
    ((_, data),) = client.of("save", "TopologyCarrierCircuit")
    return data


def _near_end(client: h.FakeClient) -> str:
    """The id of the near end of the carrier the run created."""
    return _carrier_data(client)["data"]["capabilities"][0]["id"]


class TestCreateFromIntent:
    @pytest.mark.asyncio
    async def test_the_carrier_is_created_with_its_ends_provider_and_group(self) -> None:
        client = await _run(h.connection(bandwidth=1000))

        data = _carrier_data(client)["data"]
        nic = client.of("save", "DcimVirtualInterface")[0][1]["id"]
        assert data == {
            "name": "AWS-FR2-XC",
            "circuit_id": "AWS-FR2-XC",
            "circuit_type": "virtual_connection",
            "status": "provisioning",
            "description": data["description"],
            "provider": {"id": h.PROVIDER},
            "locations": [],
            "capabilities": [{"id": h.pid("eg-1", "Ethernet1/1")}, {"id": nic}],
            "provider_connections": [{"id": h.CONNECTION_ID}],
            "member_of_groups": [{"hfid": ["carrier_circuits"], "kind": "CoreStandardGroup"}],
            "bandwidth": 1000,
        }
        assert "replace circuit_id" in data["description"]

    @pytest.mark.asyncio
    async def test_no_bandwidth_sends_none(self) -> None:
        client = await _run(h.connection())

        assert "bandwidth" not in _carrier_data(client)["data"]

    @pytest.mark.asyncio
    async def test_the_carrier_and_the_nic_are_untracked_and_the_carrier_lists_the_connection_itself(self) -> None:
        client = await _run(h.connection())

        carrier = _carrier_data(client)
        nic = client.of("save", "DcimVirtualInterface")[0][1]
        assert carrier["update_group_context"] is False and nic["update_group_context"] is False
        assert {carrier["id"], nic["id"]}.isdisjoint(client.group_context.related_node_ids)
        # the carrier lists the connection at creation: the claim and carrier_circuits are one write
        assert [d["data"] for _, d in client.of("save", "TopologyProviderConnection")] == [
            {"id": h.CONNECTION_ID, "vlan_id": 2}
        ]

    @pytest.mark.asyncio
    async def test_the_nic_is_a_virtual_uplink_of_the_service_edge_named_after_the_connection(self) -> None:
        client = await _run(h.connection())

        (_, nic) = client.of("create", "DcimVirtualInterface")[0]
        assert nic["name"] == "vnic-AWS-FR2"
        assert nic["device"] == {"id": FAR_DEVICE}
        assert nic["role"] == "uplink" and nic["status"] == "active"
        assert "parent_interface" not in nic

    @pytest.mark.asyncio
    async def test_the_sessions_follow_the_carrier_ends(self) -> None:
        client = await _run(h.connection())

        names = [data["name"] for _, data in client.of("create", "DcimVirtualInterface")]
        assert names == ["vnic-AWS-FR2", "Ethernet1/1.2", "vnic-AWS-FR2.2"]
        (circuit,) = client.of("save", "TopologySessionCircuit")
        assert circuit[1]["data"]["name"] == "AWS-FR2-L1" and circuit[1]["data"]["circuit_type"] == "handoff"

    @pytest.mark.asyncio
    async def test_the_carrier_precedes_the_sessions_and_nothing_is_written_before_the_choice(self) -> None:
        client = await _run(h.connection())

        kinds = [kind for kind, _ in client.of("save")]
        assert kinds[:2] == ["DcimVirtualInterface", "TopologyCarrierCircuit"]
        assert kinds.index("TopologyCarrierCircuit") < kinds.index("TopologySessionCircuit")

    @pytest.mark.asyncio
    async def test_a_physical_provider_device_gets_a_cross_connect_on_its_first_free_port(self) -> None:
        client = h.FakeClient()
        client.add_port("eg-1", "Ethernet1/9", role="partner")
        client.add_port("EXT-R1", "et-0/0/10", role=None)
        client.add_port("EXT-R1", "et-0/0/2", role=None)
        client.add_port("EXT-R1", "et-0/0/1", role=None, cabled=True)
        payload = h.connection(
            far_kind="TopologyExternalProviders", stubs=[h.stub_device("dev-EXT-R1", "EXT-R1")], profile="megaport"
        )

        await _run(payload, client)

        data = _carrier_data(client)["data"]
        assert data["circuit_type"] == "cross_connect"
        assert [end["id"] for end in data["capabilities"]] == [
            h.pid("eg-1", "Ethernet1/9"),
            h.pid("EXT-R1", "et-0/0/2"),
        ]
        assert client.of("create", "DcimVirtualInterface")[0][1]["name"] == "Ethernet1/9.2"  # no NIC created

    @pytest.mark.asyncio
    async def test_two_connections_to_one_region_get_distinct_nics(self) -> None:
        first = await _run(h.connection(name="PC-A", conn_id="c-a", vlan_id=100))
        second = await _run(h.connection(name="PC-B", conn_id="c-b", vlan_id=100))

        nics = [
            data["name"]
            for client in (first, second)
            for _, data in client.of("create", "DcimVirtualInterface")
            if data["device"]["id"] == FAR_DEVICE and "." not in data["name"]
        ]
        assert nics == ["vnic-PC-A", "vnic-PC-B"]

    @pytest.mark.asyncio
    async def test_an_existing_nic_of_the_connection_is_reused_not_created_again(self) -> None:
        client = h.FakeClient()
        nic = client.add_far_end("vnic-AWS-FR2")

        await _run(h.connection(vlan_id=100), client)

        assert nic in [end["id"] for end in _carrier_data(client)["data"]["capabilities"]]
        assert "vnic-AWS-FR2" not in [d["name"] for _, d in client.of("create", "DcimVirtualInterface")]


class TestNearPortSelection:
    @pytest.mark.asyncio
    async def test_the_first_free_port_of_the_role_on_the_first_device_is_taken_by_default(self) -> None:
        client = await _run(h.connection())

        assert _near_end(client) == h.pid("eg-1", "Ethernet1/1")

    @pytest.mark.asyncio
    async def test_the_least_loaded_device_wins(self) -> None:
        client = await _run(
            h.connection(near_load=[h.other_connection("o-1", "eg-1"), h.other_connection("o-2", "eg-1")])
        )

        assert _near_end(client) == h.pid("eg-2", "Ethernet1/1")

    @pytest.mark.asyncio
    async def test_a_port_with_a_carrier_a_cable_or_a_lag_is_skipped_in_natural_order(self) -> None:
        client = h.FakeClient()
        client.add_port("eg-1", "Ethernet1/10", role="cloud")
        client.add_port("eg-1", "Ethernet1/0", role="cloud", lag=True)
        client.physical[0].cable.id, client.physical[0].cable.initialized = "cable-1", True  # eg-1 Ethernet1/1
        client.add_carrier("other-xc", [h.pid("eg-1", "Ethernet1/2")], claimed_by=("other",))

        await _run(h.connection(), client)

        assert _near_end(client) == h.pid("eg-1", "Ethernet1/3")

    @pytest.mark.asyncio
    async def test_a_port_with_sub_interfaces_of_another_use_is_skipped(self) -> None:
        client = h.FakeClient(subs=[h.sub("s-1", "Ethernet1/1.50", h.pid("eg-1", "Ethernet1/1"))])

        await _run(h.connection(), client)

        assert _near_end(client) == h.pid("eg-1", "Ethernet1/2")

    @pytest.mark.asyncio
    async def test_a_port_of_another_connections_session_is_skipped(self) -> None:
        client = h.FakeClient()
        client.port_sessions = [
            h.session_node("other-s", [h.pid("eg-1", "Ethernet1/1")], connection="c-other"),
        ]

        await _run(h.connection(), client)

        assert _near_end(client) == h.pid("eg-1", "Ethernet1/2")

    @pytest.mark.asyncio
    async def test_the_role_follows_the_far_kind(self) -> None:
        client = h.FakeClient()
        client.add_port("eg-2", "Ethernet1/6", role="saas")

        await _run(h.connection(far_kind="TopologySaasRegion", profile="aws"), client)

        assert _near_end(client) == h.pid("eg-2", "Ethernet1/6")

    @pytest.mark.asyncio
    async def test_no_port_with_the_role_fails_before_any_write(self) -> None:
        client = h.FakeClient()

        with pytest.raises(GeneratorError, match="no free port with the required role"):
            await _run(h.connection(far_kind="TopologySaasRegion"), client)

        assert client.log == []
        assert client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_every_port_busy_fails_with_the_reasons_before_any_write(self) -> None:
        client = h.FakeClient()
        for port in client.physical:
            port.lag.id, port.lag.initialized = "lag-1", True

        with pytest.raises(GeneratorError, match=r"eg-1/Ethernet1/1: member of a LAG"):
            await _run(h.connection(), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_the_choice_is_made_under_both_locks_and_before_any_port_lock(self) -> None:
        client = await _run(h.connection())

        events = client.events
        near, conn = events.index(f"lock:pc-near-{h.NEAR}"), events.index("lock:pc-conn-1")
        first_port_lock = next(i for i, e in enumerate(events) if e.startswith("lock:pc-port-"))
        assert near < conn < events.index("filters:DcimPhysicalInterface") < first_port_lock
        port_locks = [e for e in events if e.startswith("lock:pc-port-")]
        assert port_locks == sorted(port_locks)
        assert events[-2:] == ["unlock:pc-conn-1", f"unlock:pc-near-{h.NEAR}"]


class TestExplicitNearPort:
    def _client_with_carrier(self, **carrier: Any) -> tuple[h.FakeClient, str, str]:
        client = h.FakeClient()
        near = h.pid("eg-2", "Ethernet1/2")
        far = client.add_far_end("vnic-from-the-data")
        client.add_carrier("xc-data", [near, far], **carrier)
        return client, near, far

    @pytest.mark.asyncio
    async def test_a_carrier_that_ends_on_the_port_is_reused_and_nothing_is_created(self) -> None:
        client, near, far = self._client_with_carrier()

        await _run(h.connection(near_port=near, vlan_id=100), client)

        assert client.of("create", "TopologyCarrierCircuit") == []
        assert "vnic-AWS-FR2" not in [d["name"] for _, d in client.of("create", "DcimVirtualInterface")]
        ((_, saved),) = client.of("save", "TopologyProviderConnection")
        assert saved["data"] == {"id": h.CONNECTION_ID, "carrier_circuits": [{"id": "xc-data"}]}
        subs = [d for _, d in client.of("create", "DcimVirtualInterface")]
        assert [(d["name"], d["parent_interface"]) for d in subs] == [
            ("Ethernet1/2.100", {"id": near}),
            ("vnic-from-the-data.100", {"id": far}),
        ]

    @pytest.mark.asyncio
    async def test_the_explicit_port_wins_over_a_less_loaded_free_one(self) -> None:
        client, near, _ = self._client_with_carrier()

        await _run(h.connection(near_port=near, near_load=[h.other_connection("o", "eg-2")], vlan_id=100), client)

        assert client.of("create", "TopologyCarrierCircuit") == []
        assert client.of("create", "DcimVirtualInterface")[0][1]["parent_interface"] == {"id": near}

    @pytest.mark.asyncio
    async def test_a_carrier_of_another_connection_is_not_reused_a_new_one_ends_on_the_same_port(self) -> None:
        client, near, _ = self._client_with_carrier(claimed_by=("other-connection",))

        await _run(h.connection(near_port=near, vlan_id=100), client)

        assert _near_end(client) == near
        assert _carrier_data(client)["data"]["name"] == "AWS-FR2-XC"

    @pytest.mark.asyncio
    async def test_a_decommissioned_carrier_is_not_reused(self) -> None:
        client, near, _ = self._client_with_carrier(status="decommissioned")

        await _run(h.connection(near_port=near, vlan_id=100), client)

        assert len(client.of("create", "TopologyCarrierCircuit")) == 1

    @pytest.mark.asyncio
    async def test_no_carrier_on_the_port_creates_one_there_even_when_the_port_is_cabled(self) -> None:
        client = h.FakeClient()
        client.physical[0].cable.id, client.physical[0].cable.initialized = "cable-1", True

        await _run(h.connection(near_port=h.pid("eg-1", "Ethernet1/1"), vlan_id=100), client)

        assert _near_end(client) == h.pid("eg-1", "Ethernet1/1")

    @pytest.mark.asyncio
    async def test_two_carriers_that_reach_the_far_facility_are_ambiguous_and_nothing_is_written(self) -> None:
        client, near, far = self._client_with_carrier()
        client.add_carrier("xc-second", [near, far])

        with pytest.raises(GeneratorError, match="several carrier circuits"):
            await _run(h.connection(near_port=near), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_port_in_conflict_is_refused_never_replaced(self) -> None:
        client = h.FakeClient()
        port = h.pid("eg-1", "Ethernet1/1")
        client.physical[0].lag.id, client.physical[0].lag.initialized = "lag-1", True

        with pytest.raises(GeneratorError, match="near_port eg-1/Ethernet1/1 cannot be used: member of a LAG"):
            await _run(h.connection(near_port=port), client)

        assert client.log == [] and client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_a_sub_interface_of_another_use_is_a_conflict(self) -> None:
        port = h.pid("eg-1", "Ethernet1/1")
        client = h.FakeClient(subs=[h.sub("s-1", "Ethernet1/1.7", port)])

        with pytest.raises(GeneratorError, match="sub-interfaces of another use"):
            await _run(h.connection(near_port=port), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_port_that_does_not_exist_is_refused(self) -> None:
        client = h.FakeClient()
        client.interfaces.clear()

        with pytest.raises(GeneratorError, match="not found"):
            await _run(h.connection(near_port="nope"), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_port_outside_the_near_facility_is_refused(self) -> None:
        client = h.FakeClient()
        port = client.add_port("elsewhere", "Ethernet1/1")

        with pytest.raises(GeneratorError, match="not on a device of the near facility"):
            await _run(h.connection(near_port=port), client)

        assert client.log == []


class TestVirtualColocation:
    """A far facility we order ourselves (a provider-hosted edge): no carrier is created for it."""

    def _payload(self, **kwargs: Any) -> dict[str, Any]:
        return h.connection(far_kind="TopologyVirtualColocation", profile="equinix", **kwargs)

    @pytest.mark.asyncio
    async def test_without_a_port_and_without_a_carrier_the_run_fails_before_any_write(self) -> None:
        client = h.FakeClient()

        with pytest.raises(GeneratorError, match="provisioned outside this model"):
            await _run(self._payload(), client)

        assert client.log == []
        assert [e for e in client.events if e.startswith("filters")] == []

    @pytest.mark.asyncio
    async def test_an_explicit_port_with_no_carrier_on_it_fails_before_any_write(self) -> None:
        client = h.FakeClient()

        with pytest.raises(GeneratorError, match="no carrier circuit on near_port"):
            await _run(self._payload(near_port=h.pid("eg-1", "Ethernet1/1")), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_carrier_authored_on_the_explicit_port_is_adopted(self) -> None:
        client = h.FakeClient()
        near = h.pid("eg-1", "Ethernet1/1")
        far = client.add_far_end("ne-uplink")
        client.add_carrier("xc-ne", [near, far])

        await _run(self._payload(near_port=near, vlan_id=100), client)

        assert client.of("create", "TopologyCarrierCircuit") == []
        assert client.of("save", "TopologyProviderConnection")[0][1]["data"]["carrier_circuits"] == [{"id": "xc-ne"}]


class TestRerun:
    def _state(self) -> tuple[h.FakeClient, dict[str, Any]]:
        client = h.FakeClient()
        client.add_far_end()
        client.subs = [
            h.sub("s-near", "Ethernet1/1.1100", h.pid("eg-1", "Ethernet1/1")),
            h.sub("s-far", "vnic-AWS-FR2.1100", h.pid("AWS-SE", "vnic-AWS-FR2")),
        ]
        session = h.session(
            "AWS-FR2-L1",
            ends=[h.end("s-near", "Ethernet1/1.1100", "eg-1"), h.end("s-far", "vnic-AWS-FR2.1100", "AWS-SE")],
            session_id="sess-1",
        )
        return client, session

    @pytest.mark.asyncio
    async def test_a_rerun_writes_nothing_and_never_selects_again(self) -> None:
        client, session = self._state()
        payload = h.connection(vlan_id=1100, carriers=[h.own_carrier()], sessions=[session])

        await _run(payload, client)

        assert client.log == []
        assert "filters:DcimPhysicalInterface" not in client.events

    @pytest.mark.asyncio
    async def test_the_own_carrier_decides_the_port_even_when_another_device_is_less_loaded(self) -> None:
        client, session = self._state()
        load = [h.other_connection("o-1", "eg-1"), h.other_connection("o-2", "eg-1")]
        payload = h.connection(vlan_id=1100, carriers=[h.own_carrier()], sessions=[session], near_load=load)

        await _run(payload, client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_two_live_own_carriers_without_an_explicit_port_are_ambiguous(self) -> None:
        client = h.FakeClient()
        carriers = [h.own_carrier(), h.own_carrier("eg-2", carrier_id="xc-2")]

        with pytest.raises(GeneratorError, match="several live carrier circuits"):
            await _run(h.connection(carriers=carriers), client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_a_new_explicit_port_replaces_the_listed_carrier_and_drops_the_old_one_from_the_list(self) -> None:
        client = h.FakeClient()
        near = h.pid("eg-2", "Ethernet1/2")
        far = client.add_far_end("vnic-from-the-data")
        client.add_carrier("xc-data", [near, far])

        await _run(h.connection(near_port=near, vlan_id=100, carriers=[h.own_carrier()]), client)

        ((_, saved),) = client.of("save", "TopologyProviderConnection")
        assert saved["data"]["carrier_circuits"] == [{"id": "xc-data"}]
        assert client.of("create", "TopologyCarrierCircuit") == []

    @pytest.mark.asyncio
    async def test_a_decommissioned_own_carrier_is_replaced_by_a_new_one(self) -> None:
        client = h.FakeClient()
        dead = h.own_carrier(status="decommissioned")

        await _run(h.connection(vlan_id=100, carriers=[dead]), client)

        assert len(client.of("create", "TopologyCarrierCircuit")) == 1
        ((_, saved),) = client.of("save", "TopologyProviderConnection")
        assert saved["data"]["carrier_circuits"] == [{"id": client.of("save", "TopologyCarrierCircuit")[0][1]["id"]}]
