"""Unit tests for the interconnect circuit session of the session generator (add_virtual_circuit)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.simulators.circuit_fakes import (
    DC_KEY_ID,
    V4_POOL_NETWORK,
    VC_ID,
    FakeClient,
    colo_edge_port,
    dc_border_port,
    deployment,
    interconnect_payload,
    loopback,
    session_port,
    underlay_circuit,
)
from tests.unit.virtual_circuit_session_helpers import (
    DCI,
    DCI_KEY,
    UNTRACKED,
    _assert_refused,
    _interface_saves,
    _peering,
    _run,
)


def _two_dc_ends(protocol: str, address_for: Any) -> list[dict[str, Any]]:
    """Two DC border leafs of one underlay protocol, each with a VTEP loopback of the matching family."""
    return [
        dc_border_port(
            where=deployment("TopologyDataCenter", "DC10", underlay=protocol),
            loopbacks=[loopback("bl-dc101101", "loopback2", "loopback-vtep", address_for("1"))],
        ),
        session_port(
            "if-bl2",
            "Ethernet1/35",
            "bl-dc111101",
            where=deployment("TopologyDataCenter", "DC11", underlay=protocol),
            circuits=[underlay_circuit()],
            loopbacks=[loopback("bl-dc111101", "loopback2", "loopback-vtep", address_for("2"))],
        ),
    ]


class TestInterconnectSession:
    @pytest.mark.asyncio
    async def test_allocates_the_dci_pool_by_the_dc_ends_underlay_protocol(self) -> None:
        """An IPv6 fabric takes a /127 (point-to-point IPv6 link prefix, RFC 6164) from DCI-Technical-IPv6."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        (allocation,) = client.allocations
        assert allocation["resource_pool"].pool_name == "DCI-Technical-IPv6"
        assert allocation["prefix_length"] == 127
        assert allocation["identifier"] == f"vc-p2p__{VC_ID}"

    @pytest.mark.asyncio
    async def test_an_ipv4_fabric_between_two_dcs_uses_the_ipv4_pool_and_family(self) -> None:
        """An IPv4 fabric takes a /31 (point-to-point IPv4 link) from DCI-Technical-IPv4 and the ipv4 unicast family."""
        client = FakeClient()
        ends = _two_dc_ends("ipv4", lambda n: f"10.0.0.{n}/32")

        await _run(client, interconnect_payload(ends=ends))

        (allocation,) = client.allocations
        assert allocation["resource_pool"].pool_name == "DCI-Technical-IPv4" and allocation["prefix_length"] == 31
        assert _peering(client, "underlay")["address_families"] == [{"id": "af-ipv4-unicast"}]
        assert client.interfaces["if-bl"].ip_address.id == f"IpamIPAddress:{V4_POOL_NETWORK.split('/')[0]}/31"

    @pytest.mark.asyncio
    async def test_the_colocation_edge_takes_the_lower_address(self) -> None:
        """The colocation edge takes the lower address of the /127, the DC border gateway the upper one."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert client.interfaces["if-eg"].ip_address.id == "IpamIPAddress:fd00:2200::/127"
        assert client.interfaces["if-bl"].ip_address.id == "IpamIPAddress:fd00:2200::1/127"

    @pytest.mark.asyncio
    async def test_the_underlay_peering_has_these_exact_attributes(self) -> None:
        """DCI underlay eBGP: ttl 1 (directly connected /127), BFD on, 16 routes at most (links, loopbacks), keyed."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert _peering(client, "underlay") == {
            "name": f"{DCI}-underlay",
            "description": f"DCI underlay eBGP eg-fr201/Ethernet1/10 <-> bl-dc101101/Ethernet1/35 over {DCI}",
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "dci-underlay",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": False,
            "maximum_routes": 16,
            "address_families": [{"id": "af-ipv6-unicast"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "bl-dc101101-bgp-overlay"}],
            "password": {"id": DCI_KEY},
            "capabilities": [{"id": "if-eg"}, {"id": "if-bl"}],
        }

    @pytest.mark.asyncio
    async def test_the_evpn_peering_has_these_exact_attributes_on_the_vtep_loopbacks(self) -> None:
        """The VTEP loopback beats the plain one (loopback1) and the multisite VIP (loopback3) is never used."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert _peering(client, "evpn") == {
            "name": f"{DCI}-evpn",
            "description": f"DCI EVPN multihop eg-fr201 <-> bl-dc101101 over {DCI}",
            "status": "active",
            "session_type": "EBGP_MULTIHOP",
            "peering_role": "dci",
            "ttl": 2,
            "bfd_enabled": False,
            "send_community": True,
            "send_extended_community": True,
            "address_families": [{"id": "af-l2vpn-evpn"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "bl-dc101101-bgp-overlay"}],
            "password": {"id": DCI_KEY},
            "capabilities": [{"id": "lo-eg-fr201-loopback1"}, {"id": "lo-bl-dc101101-loopback2"}],
        }
        assert "maximum_routes" not in _peering(client, "evpn")

    @pytest.mark.parametrize(
        ("loopbacks", "expected"),
        [
            pytest.param(
                [
                    loopback("bl-dc101101", "lo1", "loopback", "fd00:10::1/128"),
                    loopback("bl-dc101101", "lo2", "loopback-vtep", "fd00:10::2/128"),
                ],
                "lo-bl-dc101101-lo2",
                id="vtep-before-loopback",
            ),
            pytest.param(
                [loopback("bl-dc101101", "lo1", "loopback", "fd00:10::1/128")],
                "lo-bl-dc101101-lo1",
                id="loopback-when-no-vtep",
            ),
            pytest.param(
                [
                    loopback("bl-dc101101", "lo1", "loopback", "fd00:10::1/128"),
                    loopback("bl-dc101101", "lo2", "loopback-vtep", "10.0.0.2/32"),
                ],
                "lo-bl-dc101101-lo1",
                id="vtep-of-the-wrong-family-is-skipped",
            ),
            pytest.param(
                [
                    loopback("bl-dc101101", "lo3", "loopback-vtep", "fd00:10::3/128"),
                    loopback("bl-dc101101", "lo2", "loopback-vtep", "fd00:10::2/128"),
                ],
                "lo-bl-dc101101-lo2",
                id="first-by-name-among-equals",
            ),
            pytest.param(
                [
                    loopback("bl-dc101101", "lo1", "loopback-vtep", None),
                    loopback("bl-dc101101", "lo2", "loopback", "fd00:10::2/128"),
                ],
                "lo-bl-dc101101-lo2",
                id="vtep-without-an-address-is-skipped",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_loopback_selection(self, loopbacks: list[dict[str, Any]], expected: str) -> None:
        """EVPN source loopback: VTEP before plain, same address family only, with an address, first by name."""
        client = FakeClient()

        await _run(client, interconnect_payload(ends=[dc_border_port(loopbacks=loopbacks), colo_edge_port()]))

        assert _peering(client, "evpn")["capabilities"] == [{"id": "lo-eg-fr201-loopback1"}, {"id": expected}]

    @pytest.mark.asyncio
    async def test_the_key_is_created_once_untracked_and_shared_by_both_peerings(self) -> None:
        """Untracked: the run's cleanup must never reach the key, or the next run would generate a new one."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        (key_data, _) = client.created_of("RoutingPassword")[0]
        assert key_data["name"] == f"{DCI}-dci-key" and key_data["password"]
        assert client.saves()[DCI_KEY] == {"allow_upsert": True, **UNTRACKED}
        assert DCI_KEY not in client.claimed()
        assert {_peering(client, s)["password"]["id"] for s in ("underlay", "evpn")} == {DCI_KEY}

    @pytest.mark.asyncio
    async def test_a_second_run_reuses_the_key_and_creates_no_second_one(self) -> None:
        """The rerun finds the key by name: no second key, the same secret, and still not tracked."""
        client = FakeClient()
        await _run(client, interconnect_payload())
        first_password = client.created_of("RoutingPassword")[0][0]["password"]

        await _run(client, interconnect_payload())

        assert len(client.created_of("RoutingPassword")) == 1
        assert client.keys[f"{DCI}-dci-key"].id == DCI_KEY
        assert first_password == client.created_of("RoutingPassword")[0][0]["password"]
        assert DCI_KEY not in client.claimed()
        assert len(client.prefixes) == 1 and len(client.addresses) == 2

    @pytest.mark.parametrize(
        "second_payload",
        [
            pytest.param(lambda: interconnect_payload(status="decommissioned"), id="decommissioned"),
            pytest.param(lambda: interconnect_payload(ends=[dc_border_port()]), id="one-end"),
            pytest.param(
                lambda: interconnect_payload(
                    ends=[dc_border_port(circuits=[]), colo_edge_port(circuits=[])],
                ),
                id="no-carrier",
            ),
            pytest.param(
                lambda: interconnect_payload(
                    ends=[
                        dc_border_port(circuits=[underlay_circuit(status="decommissioned")]),
                        colo_edge_port(circuits=[underlay_circuit(status="decommissioned")]),
                    ]
                ),
                id="whole-carrier-decommissioned",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_run_that_returns_early_after_a_build_never_touches_the_key(self, second_payload: Any) -> None:
        """Every early return skips the writes, and an untracked key is not in the run's group for the cleanup to
        delete, so a later full run finds the same key (no rotation)."""
        client = FakeClient()
        await _run(client, interconnect_payload())
        key = client.keys[f"{DCI}-dci-key"]
        password = client.created_of("RoutingPassword")[0][0]["password"]
        writes_before = len(client.writes)

        await _run(client, second_payload())

        assert len(client.writes) == writes_before
        assert len(client.created_of("RoutingPassword")) == 1
        assert client.keys[f"{DCI}-dci-key"] is key
        assert DCI_KEY not in client.claimed()

        await _run(client, interconnect_payload())

        assert len(client.created_of("RoutingPassword")) == 1
        assert client.created_of("RoutingPassword")[0][0]["password"] == password

    @pytest.mark.asyncio
    async def test_an_existing_key_is_reused_and_not_tracked(self) -> None:
        """A key that already exists is referenced as it is: nothing created and the run's cleanup cannot delete it."""
        client = FakeClient(key=True)

        await _run(client, interconnect_payload())

        assert client.created_of("RoutingPassword") == []
        assert DC_KEY_ID not in client.group_context.related_node_ids
        assert _peering(client, "underlay")["password"] == {"id": DC_KEY_ID}

    @pytest.mark.asyncio
    async def test_the_two_physical_ports_get_mtu_9216_untracked(self) -> None:
        """9216 is the jumbo routed-port MTU on DCI ports; the two ports are saved untracked."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        for port in ("if-eg", "if-bl"):
            assert client.interfaces[port].mtu.value == 9216
            assert client.saves()[port] == UNTRACKED
        assert client.interfaces["if-eg"].description.value == f"DCI {DCI} to bl-dc101101 Ethernet1/35"

    @pytest.mark.asyncio
    async def test_claims_prefix_addresses_and_peerings_only(self) -> None:
        """Only the prefix, its two addresses and the two peerings are tracked; interfaces and the key are not."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert client.claimed() == {
            f"prefix-vc-p2p__{VC_ID}",
            "IpamIPAddress:fd00:2200::/127",
            "IpamIPAddress:fd00:2200::1/127",
            f"ManagedBGPPeering:{DCI}-underlay",
            f"ManagedBGPPeering:{DCI}-evpn",
        }

    @pytest.mark.asyncio
    async def test_a_second_run_writes_no_interface_again(self) -> None:
        """Idempotency: a rerun does not save the already configured interfaces again."""
        client = FakeClient()
        await _run(client, interconnect_payload())
        first_interface_saves = _interface_saves(client)

        await _run(client, interconnect_payload())

        assert _interface_saves(client) == first_interface_saves

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(
                interconnect_payload(
                    ends=[
                        dc_border_port(where=deployment("TopologyDataCenter", "DC10", underlay="dual_stack")),
                        colo_edge_port(),
                    ]
                ),
                r"DC10 is dual_stack, a DCI needs one address family",
                id="dual-stack",
            ),
            pytest.param(
                interconnect_payload(
                    ends=[
                        dc_border_port(where=deployment("TopologyDataCenter", "DC10", underlay="ipv4")),
                        colo_edge_port(),
                    ]
                ),
                r"eg-fr201 is a colocation edge, whose VTEP loopback is IPv6 only",
                id="ipv4-towards-a-colocation-edge",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(roles=("underlay",)), colo_edge_port()]),
                r"bl-dc101101 needs exactly one overlay ManagedBGP process",
                id="no-overlay-process",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(), colo_edge_port(roles=("overlay", "overlay"))]),
                r"eg-fr201 needs exactly one overlay ManagedBGP process",
                id="two-overlay-processes",
            ),
            pytest.param(
                interconnect_payload(
                    ends=[
                        colo_edge_port(),
                        session_port(
                            "if-eg2",
                            "Ethernet1/10",
                            "eg-fr202",
                            where=deployment("TopologyColocationZone", "FR3"),
                            circuits=[underlay_circuit()],
                        ),
                    ]
                ),
                r"a DCI needs a DC fabric device at one end",
                id="no-dc-end",
            ),
            pytest.param(
                interconnect_payload(
                    ends=_two_dc_ends("ipv4", lambda n: f"10.0.0.{n}/32")[:1]
                    + [
                        session_port(
                            "if-bl2",
                            "Ethernet1/35",
                            "bl-dc111101",
                            where=deployment("TopologyDataCenter", "DC11", underlay="ipv6"),
                            circuits=[underlay_circuit()],
                            loopbacks=[loopback("bl-dc111101", "loopback2", "loopback-vtep", "fd00:11::2/128")],
                        )
                    ]
                ),
                r"the DC ends disagree on underlay_protocol \(ipv4, ipv6\)",
                id="dc-ends-disagree",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(kind="DcimVirtualInterface"), colo_edge_port()]),
                r"is a DcimVirtualInterface, a DCI ends on physical ports",
                id="end-is-not-a-physical-port",
            ),
            pytest.param(
                interconnect_payload(
                    ends=[
                        dc_border_port(
                            loopbacks=[loopback("bl-dc101101", "loopback3", "multisite-vip", "fd00:10::99/128")]
                        ),
                        colo_edge_port(),
                    ]
                ),
                r"bl-dc101101 has no loopback-vtep or loopback with an IPv6 address.*multisite-vip is never used",
                id="only-the-multisite-vip",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(), colo_edge_port(loopbacks=[])]),
                r"eg-fr201 has no loopback-vtep or loopback with an IPv6 address",
                id="no-loopback",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_refused_dci_has_written_nothing(self, payload: dict[str, Any], message: str) -> None:
        """Pool, key, prefix, addresses, interfaces and peerings: not one write before a refusal."""
        client = FakeClient()

        await _assert_refused(payload, message, client)

        assert client.created_of("RoutingPassword") == [] and client.interfaces == {}

    @pytest.mark.asyncio
    async def test_a_missing_pool_is_refused_before_any_write(self) -> None:
        """Without the DCI-Technical-IPv6 pool the run is refused before it writes anything."""
        await _assert_refused(interconnect_payload(), r"pool 'DCI-Technical-IPv6' not found", FakeClient(pools=False))
