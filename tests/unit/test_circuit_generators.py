"""Unit tests for the circuit generators (generators/topology/circuit.py).

PhysicalCircuitGenerator builds the DCI session a peering_role=dci circuit
asks for (P2P from the DCI pool, both interfaces addressed, one eBGP peering)
and fans out add_virtual_circuit; VirtualCircuitGenerator builds the overlay
eBGP peering a peering_role=overlay tunnel circuit asks for. Every refusal
must happen before the first write. Fakes: tests/unit/simulators/circuit_fakes.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.logger import GeneratorError
from generators.topology.circuit import (
    CircuitEndpoint,
    endpoint_problem,
    order_endpoints,
    physical_circuit_endpoints,
)
from tests.unit.simulators.circuit_fakes import (
    DC_KEY_ID,
    PREFIX_ID,
    V4_POOL_ID,
    V6_POOL_ID,
    FakeClient,
    cage_edge,
    dc_border_leaf,
    interface,
    physical_generator,
    physical_payload,
    sdwan_edge,
    sdwan_gateway,
    underlay_circuit,
    virtual_generator,
    virtual_payload,
)
from utils.data_cleaning import clean_data

TRACKED = {"allow_upsert": True}


def _peerings(client: FakeClient) -> list[dict[str, Any]]:
    return [data for data, _ in client.created_of("ManagedBGPPeering")]


# ===========================================================================
# Endpoint parsing
# ===========================================================================


class TestEndpoints:
    def test_customer_and_provider_interfaces_are_one_endpoint_list(self) -> None:
        """Data fills customer_interfaces/provider_interfaces inconsistently:
        the endpoints are both lists together, deduplicated."""
        circuit = clean_data(physical_payload(customer=[dc_border_leaf(), cage_edge()], provider=[cage_edge()]))
        endpoints = physical_circuit_endpoints(circuit["TopologyPhysicalCircuit"][0])

        assert [e.label for e in endpoints] == ["bl-dc101101/Ethernet1/35", "eg-fr01/Ethernet1/10"]

    def test_dc_end_carries_its_fabric_and_overlay_process(self) -> None:
        """A device whose deployment is a TopologyDataCenter is a DC fabric end."""
        bl = CircuitEndpoint.parse(clean_data(dc_border_leaf()))
        eg = CircuitEndpoint.parse(clean_data(cage_edge()))

        assert bl is not None and eg is not None
        assert (bl.fabric, bl.underlay_protocol) == ("dc10", "ipv6")
        assert eg.fabric is None
        assert bl.bgp_process("overlay") == "bl-dc101101-bgp-overlay"

    def test_the_facility_end_comes_first(self) -> None:
        """The non-DC end takes the lower P2P address, the DC end the upper one."""
        bl = CircuitEndpoint.parse(clean_data(dc_border_leaf()))
        eg = CircuitEndpoint.parse(clean_data(cage_edge()))
        assert bl is not None and eg is not None

        assert order_endpoints([bl, eg]) == (eg, bl)

    @pytest.mark.parametrize(
        ("interfaces", "problem"),
        [
            ([dc_border_leaf()], "exactly 2 endpoint interfaces, found 1"),
            ([dc_border_leaf(), cage_edge(), sdwan_edge()], "exactly 2 endpoint interfaces, found 3"),
            (
                [dc_border_leaf(), interface("if-bl-2", "Ethernet1/36", "bl-dc101101", dc="DC10")],
                "endpoints on 2 different devices, both are on bl-dc101101",
            ),
        ],
    )
    def test_a_session_needs_two_interfaces_on_two_devices(self, interfaces: list, problem: str) -> None:
        """Anything but exactly two interfaces on two devices carries no session."""
        endpoints = [CircuitEndpoint.parse(clean_data(i)) for i in interfaces]

        assert endpoint_problem([e for e in endpoints if e is not None]) == problem


# ===========================================================================
# PhysicalCircuitGenerator
# ===========================================================================


class TestPhysicalCircuitNoSession:
    @pytest.mark.asyncio
    async def test_role_none_writes_nothing(self) -> None:
        """peering_role none (the default) builds no session."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none"))

        assert client.writes == []
        assert client.created == []

    @pytest.mark.asyncio
    async def test_unset_role_is_none(self) -> None:
        """A circuit loaded before the attribute existed reads as none."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(role=None))

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_role_none_with_one_endpoint_only_logs(self) -> None:
        """An INET-style circuit with one terminating port is fine without a session."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none", provider=[]))

        assert client.writes == []
        assert any("exactly 2 endpoint interfaces" in str(c) for c in gen.logger.info.call_args_list)

    @pytest.mark.parametrize("role", ["underlay", "transit"])
    @pytest.mark.asyncio
    async def test_unimplemented_roles_log_and_write_nothing(self, role: str) -> None:
        """underlay and transit are accepted values, not generated yet — and not a failure."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role=role))

        assert client.writes == []
        assert any("not implemented yet" in str(c) for c in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_decommissioned_dci_circuit_writes_nothing(self) -> None:
        """No session: the run tracks nothing, so cleanup removes what an earlier run built."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(status="decommissioned"))

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_empty_response_fails(self) -> None:
        with pytest.raises(GeneratorError, match="No TopologyPhysicalCircuit"):
            await physical_generator(FakeClient()).generate({"TopologyPhysicalCircuit": {"edges": []}})


class TestPhysicalCircuitDci:
    @pytest.mark.asyncio
    async def test_prefix_comes_from_the_ipv6_dci_pool(self) -> None:
        """One /127 per circuit, idempotent per circuit node id."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        assert len(client.allocations) == 1
        allocation = client.allocations[0]
        assert allocation["resource_pool"].id == V6_POOL_ID
        assert allocation["identifier"] == "dci-p2p__circ-df10"
        assert allocation["prefix_length"] == 127
        assert allocation["member_type"] == "address"

    @pytest.mark.asyncio
    async def test_ipv4_fabric_uses_the_ipv4_dci_pool_and_unicast_family(self) -> None:
        """The DCI follows the DC fabric's underlay_protocol."""
        client = FakeClient()
        payload = physical_payload(customer=[dc_border_leaf(underlay="ipv4")])

        await physical_generator(client).generate(payload)

        assert client.allocations[0]["resource_pool"].id == V4_POOL_ID
        assert client.allocations[0]["prefix_length"] == 31
        assert _peerings(client)[0]["address_families"] == [{"id": "af-ipv4-unicast"}, {"id": "af-l2vpn-evpn"}]

    @pytest.mark.asyncio
    async def test_prefix_and_both_addresses_are_tracked(self) -> None:
        """The prefix is claimed by id (allocation is no save); the addresses are tracked upserts."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        assert client.group_context.related_node_ids == [PREFIX_ID]
        addresses = [data["address"] for data, _ in client.created_of("IpamIPAddress")]
        assert addresses == ["fd00:2200::/127", "fd00:2200::1/127"]
        saves = client.saves()
        assert saves["IpamIPAddress:fd00:2200::/127"] == TRACKED
        assert saves["IpamIPAddress:fd00:2200::1/127"] == TRACKED

    @pytest.mark.asyncio
    async def test_interfaces_are_addressed_untracked(self) -> None:
        """The cage end takes the lower address, the DC end the upper one;
        both template interfaces are saved with update_group_context=False."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        cage, leaf = client.interfaces["if-eg"], client.interfaces["if-bl"]
        assert cage.ip_address == "IpamIPAddress:fd00:2200::/127"
        assert leaf.ip_address == "IpamIPAddress:fd00:2200::1/127"
        assert cage.description.value == "DCI DF-DC10-EQXFR2 to bl-dc101101 Ethernet1/35"
        assert leaf.description.value == "DCI DF-DC10-EQXFR2 to eg-fr01 Ethernet1/10"
        assert cage.status.value == leaf.status.value == "active"
        assert client.saves()["if-eg"] == {"update_group_context": False}
        assert client.saves()["if-bl"] == {"update_group_context": False}

    @pytest.mark.parametrize(
        ("circuit_status", "interface_status"), [("maintenance", "maintenance"), ("down", "outage")]
    )
    @pytest.mark.asyncio
    async def test_circuit_status_reaches_the_interfaces(self, circuit_status: str, interface_status: str) -> None:
        """A circuit in maintenance or down shuts its interfaces (templates render non-active shut)."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(status=circuit_status))

        assert client.interfaces["if-bl"].status.value == interface_status
        assert _peerings(client)[0]["status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_unchanged_interfaces_are_not_resaved(self) -> None:
        """A rerun over already-addressed interfaces writes them not at all."""
        client = FakeClient()
        client.interface_node(
            "if-eg",
            address_id="IpamIPAddress:fd00:2200::/127",
            description="DCI DF-DC10-EQXFR2 to bl-dc101101 Ethernet1/35",
            status="active",
        )

        await physical_generator(client).generate(physical_payload())

        assert "if-eg" not in client.saves()
        assert "if-bl" in client.saves()

    @pytest.mark.asyncio
    async def test_one_tracked_dci_peering(self) -> None:
        """The session the hand-written 05_dci_peerings.yml used to declare."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        peerings = client.created_of("ManagedBGPPeering")
        assert len(peerings) == 1
        data, node = peerings[0]
        assert data == {
            "name": "DCI-DF-DC10-EQXFR2",
            "description": "EVPN Multi-Site DCI eg-fr01 <-> bl-dc101101 over DF-DC10-EQXFR2",
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "dci",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": True,
            "address_families": [{"id": "af-ipv6-unicast"}, {"id": "af-l2vpn-evpn"}],
            "bgp_processes": [{"id": "eg-fr01-bgp-overlay"}, {"id": "bl-dc101101-bgp-overlay"}],
            "password": {"id": DC_KEY_ID},
            "interface_capabilities": [{"id": "if-eg"}, {"id": "if-bl"}],
        }
        assert client.saves()[node.id] == TRACKED

    @pytest.mark.asyncio
    async def test_key_is_the_dc_fabric_overlay_key(self) -> None:
        """Looked up by the DC end's fabric name, never written."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        assert ("RoutingPassword", {"name__value": "dc10-overlay-key"}) in client.get_calls
        assert not client.created_of("RoutingPassword")

    @pytest.mark.asyncio
    async def test_missing_address_families_are_created_untracked_under_a_lock(self) -> None:
        """Global nodes: found or created inside resource_lock, never claimed."""
        client = FakeClient(address_families=False)
        gen = physical_generator(client)

        await gen.generate(physical_payload())

        assert gen.lock_keys == ["bgp-af-ipv6-unicast", "bgp-af-l2vpn-evpn"]
        families = client.created_of("RoutingBGPAddressFamily")
        assert [(d["afi"], d["safi"]) for d, _ in families] == [("ipv6", "unicast"), ("l2vpn", "evpn")]
        assert families[1][0]["advertise_all_vni"] is True
        for _, node in families:
            assert client.saves()[node.id] == {"allow_upsert": True, "update_group_context": False}
            assert node.id not in client.group_context.related_node_ids


class TestPhysicalCircuitDciRefusals:
    """Every precondition fails before the first write (save, allocation, claim)."""

    @pytest.mark.parametrize(
        ("payload", "client_kwargs", "message"),
        [
            pytest.param(physical_payload(provider=[]), {}, "exactly 2 endpoint interfaces, found 1", id="one-end"),
            pytest.param(
                physical_payload(provider=[interface("if-bl-2", "Ethernet1/36", "bl-dc101101", dc="DC10")]),
                {},
                "2 different devices",
                id="same-device",
            ),
            pytest.param(
                physical_payload(customer=[sdwan_edge()]), {}, "needs a DC fabric device at one end", id="no-dc-end"
            ),
            pytest.param(
                physical_payload(provider=[cage_edge(roles=("underlay",))]),
                {},
                "eg-fr01 has no single overlay ManagedBGP process",
                id="no-overlay-process",
            ),
            pytest.param(
                physical_payload(customer=[dc_border_leaf(kind="DcimLAGInterface")]),
                {},
                "takes no address",
                id="lag-endpoint",
            ),
            pytest.param(physical_payload(), {"key": False}, "'dc10-overlay-key' not found", id="no-key"),
            pytest.param(physical_payload(), {"pools": False}, "DCI-Technical-IPv6' not found", id="no-pool"),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusal_writes_nothing(self, payload: dict, client_kwargs: dict, message: str) -> None:
        client = FakeClient(**client_kwargs)

        with pytest.raises(GeneratorError, match=message):
            await physical_generator(client).generate(payload)

        assert client.writes == []
        assert client.created == []


class TestPhysicalCircuitFanOut:
    @pytest.mark.asyncio
    async def test_reruns_the_overlay_circuits_riding_it(self) -> None:
        """Fire-and-forget, so a status change reaches the virtual circuits."""
        client = FakeClient()
        client.virtual_circuits = [MagicMock(id="vc-2"), MagicMock(id="vc-1")]
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none"))

        assert (
            "TopologyVirtualCircuit",
            {"physical_circuits__ids": ["circ-df10"], "peering_role__value": "overlay"},
        ) in client.filter_calls
        gen.run_generator.assert_awaited_once_with("add_virtual_circuit", ["vc-1", "vc-2"], wait=False)

    @pytest.mark.asyncio
    async def test_no_overlay_circuits_no_fan_out(self) -> None:
        gen = physical_generator(FakeClient())

        await gen.generate(physical_payload())

        gen.run_generator.assert_not_awaited()


# ===========================================================================
# VirtualCircuitGenerator
# ===========================================================================


class TestVirtualCircuitOverlay:
    @pytest.mark.asyncio
    async def test_role_none_writes_nothing_and_waits_for_nothing(self) -> None:
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(role="none"))

        assert client.writes == []
        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_managed_link_type_is_skipped(self) -> None:
        """A cloud on-ramp is the provider's session, not ours, even if marked overlay."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(link_type="direct_connect_aws"))

        assert client.writes == []
        assert any("provider-managed" in str(c) for c in gen.logger.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_overlay_session_over_existing_addresses(self) -> None:
        """One tracked eBGP peering; nothing allocated, no interface fetched or written."""
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload())

        assert client.allocations == []
        assert client.interfaces == {}
        assert [name for name, _, _ in client.created] == ["ManagedBGPPeering"]
        data, node = client.created_of("ManagedBGPPeering")[0]
        assert data == {
            "name": "C001-SDWAN-FR2-bgp",
            "description": "Overlay eBGP C001-EDGE1/GE1 <-> EQX-FR2-SDWAN-GW1/eth0.1001 over C001-SDWAN-FR2",
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "overlay",
            "ttl": 1,
            "bgp_processes": [{"id": "C001-EDGE1-bgp-underlay"}, {"id": "EQX-FR2-SDWAN-GW1-bgp-underlay"}],
            "interface_capabilities": [{"id": "if-edge"}, {"id": "if-gw"}],
        }
        assert client.saves() == {node.id: TRACKED}

    @pytest.mark.asyncio
    async def test_waits_for_every_physical_circuit_run(self) -> None:
        gen = virtual_generator(FakeClient())
        payload = virtual_payload(physical=[underlay_circuit("INET-A"), underlay_circuit("INET-B")])

        await gen.generate(payload)

        waited = [c.args for c in gen.wait_for_parent_generator_and_refetch.await_args_list]
        assert waited == [("add_circuit", "pc-INET-A"), ("add_circuit", "pc-INET-B")]

    @pytest.mark.asyncio
    async def test_refetched_data_wins(self) -> None:
        """Data re-collected after the underlay run is what the session is built from."""
        client = FakeClient()
        gen = virtual_generator(client)
        gen.wait_for_parent_generator_and_refetch.return_value = virtual_payload(role="none")

        await gen.generate(virtual_payload())

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_dc_end_keys_the_session_with_its_fabric_key(self) -> None:
        client = FakeClient()
        dc_end = interface("if-bl", "Ethernet1/40", "bl-dc101101", address="10.0.0.1/31", dc="DC10", roles=("regular",))

        await virtual_generator(client).generate(virtual_payload(interfaces=[sdwan_edge(), dc_end]))

        assert _peerings(client)[0]["password"] == {"id": DC_KEY_ID}

    @pytest.mark.asyncio
    async def test_decommissioned_underlay_builds_no_session(self) -> None:
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload(physical=[underlay_circuit(status="decommissioned")]))

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_underlay_down_keeps_the_session_provisioning(self) -> None:
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload(physical=[underlay_circuit(status="down")]))

        assert _peerings(client)[0]["status"] == "provisioning"


class TestVirtualCircuitRefusals:
    @pytest.mark.parametrize(
        ("payload", "client_kwargs", "message"),
        [
            pytest.param(virtual_payload(physical=[]), {}, "needs its physical_circuits", id="no-underlay"),
            pytest.param(virtual_payload(interfaces=[sdwan_edge()]), {}, "exactly 2 endpoint interfaces", id="one-end"),
            pytest.param(
                virtual_payload(interfaces=[sdwan_edge(), sdwan_gateway(address=None)]),
                {},
                "has no IP address",
                id="no-address",
            ),
            pytest.param(
                virtual_payload(
                    interfaces=[
                        sdwan_edge(),
                        interface("if-x", "eth1", "GW2", address="10.0.0.1/31", roles=("underlay", "overlay")),
                    ]
                ),
                {},
                "GW2 needs one ManagedBGP process",
                id="ambiguous-process",
            ),
            pytest.param(
                virtual_payload(
                    interfaces=[
                        sdwan_edge(),
                        interface("if-bl", "Ethernet1/40", "bl-dc101101", address="10.0.0.1/31", dc="DC10"),
                    ]
                ),
                {},
                "bl-dc101101 needs one ManagedBGP process",
                id="dc-end-without-regular-process",
            ),
            pytest.param(
                virtual_payload(
                    interfaces=[
                        sdwan_edge(),
                        interface(
                            "if-bl", "Ethernet1/40", "bl-dc101101", address="10.0.0.1/31", dc="DC10", roles=("regular",)
                        ),
                    ]
                ),
                {"key": False},
                "'dc10-overlay-key' not found",
                id="dc-end-without-key",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusal_writes_nothing(self, payload: dict, client_kwargs: dict, message: str) -> None:
        client = FakeClient(**client_kwargs)

        with pytest.raises(GeneratorError, match=message):
            await virtual_generator(client).generate(payload)

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_empty_response_fails(self) -> None:
        with pytest.raises(GeneratorError, match="No TopologyVirtualCircuit"):
            await virtual_generator(FakeClient()).generate({"TopologyVirtualCircuit": {"edges": []}})
