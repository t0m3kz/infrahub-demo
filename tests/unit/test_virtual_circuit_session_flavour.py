"""Unit tests for the connection flavour of add_virtual_circuit: link prefix length, address source, offsets, the vpn."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.logger import GeneratorError
from generators.topology.virtual_circuit import _prefix_length, connection_flavour
from tests.unit.simulators.circuit_fakes import (
    FakeClient,
    connection_node,
    handoff_payload,
    vpn_payload,
)
from tests.unit.virtual_circuit_session_helpers import _assert_refused, _peering, _run
from utils.provider_services import DEFAULT_FLAVOUR, FLAVOURS, PATH_DEDICATED, connection_provider

VPN = "VC-C001-ZS-VPN"
BASE = "10.255.128.0"
ASSIGNED = "169.254.10.0"


def _assigned(client: FakeClient, prefix: str) -> None:
    """Register the provider-assigned prefix the circuit's link_prefix points at."""
    node = MagicMock(id="link-prefix-1")
    node.prefix = MagicMock(value=prefix)
    node.ip_namespace = MagicMock(id="ns-pool")
    client.assigned_prefixes["link-prefix-1"] = node


def _addresses(client: FakeClient) -> dict[str, str]:
    """Interface id -> the address id it was given."""
    return {iface_id: iface.ip_address.id for iface_id, iface in client.interfaces.items()}


class TestFlavourOfTheConnection:
    def test_no_connection_is_the_default_flavour(self) -> None:
        """A circuit with no connection keeps today's behaviour."""
        assert connection_flavour({}) is DEFAULT_FLAVOUR
        assert connection_flavour({"connection": None}) is DEFAULT_FLAVOUR

    def test_the_profile_comes_from_the_account_first(self) -> None:
        """A cloud connection reads its account's provider."""
        connection = {"account": {"provider": {"id": "p-1", "connection_profile": "azure"}}, "path_type": "dedicated"}
        provider = connection_provider(connection)
        assert provider is not None
        assert provider["connection_profile"] == "azure"
        flavour = connection_flavour({"connection": {"id": "pc", **connection}})
        assert (flavour.address_source, flavour.prefix_length) == ("provider_assigned", 30)

    def test_the_profile_falls_back_to_the_far_facility_and_a_saas_region_parent(self) -> None:
        """An external provider has its own provider, a SaaS region the one of its SaaS cloud."""
        equinix = connection_provider({"far": {"provider": {"id": "p-1", "connection_profile": "equinix"}}})
        assert equinix is not None
        assert equinix["connection_profile"] == "equinix"
        zscaler = connection_provider({"far": {"parent": {"provider": {"id": "p-1", "connection_profile": "zscaler"}}}})
        assert zscaler is not None
        assert zscaler["connection_profile"] == "zscaler"
        assert connection_provider({"account": None, "far": {}}) is None

    def test_an_unlisted_pair_is_the_default_flavour(self) -> None:
        """A profile with no row for the path type is the default."""
        circuit = {
            "connection": {
                "id": "pc",
                "path_type": "internet_vpn",
                "far": {"provider": {"id": "p-1", "connection_profile": "equinix"}},
            }
        }
        assert connection_flavour(circuit) is DEFAULT_FLAVOUR

    @pytest.mark.parametrize(
        ("prefix", "length"), [("10.0.0.0/30", 30), ("fd00::/127", 127), (None, None), ("x", None)]
    )
    def test_prefix_length(self, prefix: Any, length: int | None) -> None:
        """The length of a prefix string; None for a malformed one."""
        assert _prefix_length(prefix) == length


class TestDefaultFlavourIsToday:
    @pytest.mark.asyncio
    async def test_a_connection_with_the_default_row_changes_nothing(self) -> None:
        """AWS Direct Connect is a /31 from our pool: byte-identical to a handoff with no connection."""
        plain, with_connection = FakeClient(), FakeClient()

        await _run(plain, handoff_payload())
        await _run(with_connection, handoff_payload(connection=connection_node(profile="aws")))

        assert with_connection.allocations[0]["prefix_length"] == 31
        assert _addresses(with_connection) == _addresses(plain)
        assert _peering(with_connection, "bgp") == _peering(plain, "bgp")
        assert [w[:2] for w in with_connection.writes] == [w[:2] for w in plain.writes]


class TestBfdIsTheFlavours:
    @pytest.mark.parametrize(("key", "flavour"), list(FLAVOURS.items()))
    def test_bfd_is_on_exactly_for_a_dedicated_path(self, key: tuple[str, str], flavour: Any) -> None:
        """One rule: a dedicated path runs BFD, a path over the internet does not; the default is a handoff's."""
        assert flavour.bfd is (key[1] == PATH_DEDICATED)
        assert DEFAULT_FLAVOUR.bfd is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile", [None, "aws", "megaport", "equinix", "azure"])
    async def test_a_handoff_reads_bfd_from_its_flavour(self, profile: str | None) -> None:
        """A handoff with no connection or a dedicated one keeps BFD on."""
        client = FakeClient()
        payload = handoff_payload(connection=connection_node(profile=profile)) if profile else handoff_payload()
        if profile == "azure":
            _assigned(client, f"{ASSIGNED}/30")
            payload = handoff_payload(connection=connection_node(profile=profile), link_prefix=f"{ASSIGNED}/30")

        await _run(client, payload)

        assert _peering(client, "bgp")["bfd_enabled"] is True


class TestOffsets:
    @pytest.mark.asyncio
    async def test_a_slash_31_uses_offsets_0_and_1(self) -> None:
        """Our end is the lower address of a /31."""
        client = FakeClient()

        await _run(client, handoff_payload())

        assert _addresses(client) == {"if-hs": f"IpamIPAddress:{BASE}/31", "if-aws": "IpamIPAddress:10.255.128.1/31"}

    @pytest.mark.asyncio
    async def test_a_slash_30_from_our_pool_uses_offsets_1_and_2(self) -> None:
        """A zscaler tunnel pair: /30 allocated from the namespace pool, hosts .1 and .2, multihop TTL 2."""
        client = FakeClient()
        connection = connection_node(path_type="internet_vpn", profile="zscaler", via="saas")

        await _run(client, vpn_payload(connection=connection))

        (allocation,) = client.allocations
        assert allocation["prefix_length"] == 30 and allocation["resource_pool"].pool_name == "Handoff-PROD-IPv4"
        assert _addresses(client) == {
            "if-tun-hs": "IpamIPAddress:10.255.128.1/30",
            "if-tun-aws": "IpamIPAddress:10.255.128.2/30",
        }
        peering = _peering(client, "bgp")
        assert peering["ttl"] == 2 and peering["session_type"] == "EBGP_MULTIHOP" and peering["bfd_enabled"] is False

    @pytest.mark.asyncio
    async def test_provider_assigned_slash_30_gives_us_the_lower_address_when_the_flavour_says_so(self) -> None:
        """Azure ExpressRoute: no allocation, the circuit's link_prefix, our end .1, the provider's .2, BFD on."""
        client = FakeClient()
        _assigned(client, f"{ASSIGNED}/30")

        await _run(client, handoff_payload(connection=connection_node(profile="azure"), link_prefix=f"{ASSIGNED}/30"))

        assert client.allocations == []
        assert _addresses(client) == {
            "if-hs": "IpamIPAddress:169.254.10.1/30",
            "if-aws": "IpamIPAddress:169.254.10.2/30",
        }
        assert _peering(client, "bgp")["bfd_enabled"] is True
        assert ("IpamPrefix", {"id": "link-prefix-1"}) in client.get_calls
        assert all(w[1] != "link-prefix-1" for w in client.writes if w[0] == "track")

    @pytest.mark.asyncio
    async def test_provider_assigned_slash_29_with_the_provider_lower(self) -> None:
        """Cloud Interconnect: /29, the provider takes offset 1 and we take 2."""
        client = FakeClient()
        _assigned(client, f"{ASSIGNED}/29")

        await _run(client, handoff_payload(connection=connection_node(profile="gcp"), link_prefix=f"{ASSIGNED}/29"))

        assert _addresses(client) == {
            "if-hs": "IpamIPAddress:169.254.10.2/29",
            "if-aws": "IpamIPAddress:169.254.10.1/29",
        }

    @pytest.mark.asyncio
    async def test_a_vpn_with_the_provider_lower(self) -> None:
        """AWS Site-to-Site VPN: /30 assigned by AWS, the provider end is the lower address."""
        client = FakeClient()
        _assigned(client, f"{ASSIGNED}/30")
        connection = connection_node(path_type="internet_vpn", profile="aws")

        await _run(client, vpn_payload(connection=connection, link_prefix=f"{ASSIGNED}/30"))

        assert client.allocations == []
        assert _addresses(client) == {
            "if-tun-hs": "IpamIPAddress:169.254.10.2/30",
            "if-tun-aws": "IpamIPAddress:169.254.10.1/30",
        }


class TestVpn:
    @pytest.mark.asyncio
    async def test_a_vpn_needs_no_carrier_and_builds_one_regular_session(self) -> None:
        """The tunnel interfaces carry no circuit: the session is still built, eBGP, no communities, BFD of the default flavour."""
        client = FakeClient()

        await _run(client, vpn_payload())

        peering = _peering(client, "bgp")
        assert peering["name"] == f"{VPN}-bgp"
        assert peering["peering_role"] == "regular" and peering["session_type"] == "EBGP"
        assert peering["ttl"] == 1 and peering["bfd_enabled"] is DEFAULT_FLAVOUR.bfd
        assert peering["send_community"] is False
        assert "VPN eBGP" in peering["description"]
        assert client.allocations[0]["data"]["description"] == f"VPN {VPN}"
        assert client.allocations[0]["prefix_length"] == 31

    @pytest.mark.asyncio
    async def test_a_physical_end_is_refused(self) -> None:
        """A VPN ends on tunnel interfaces, a physical port is no tunnel."""
        from tests.unit.simulators.circuit_fakes import session_port, tunnel_edge_port

        physical = session_port("if-p", "Ethernet1", "EXT-ZS-FR2", kind="DcimPhysicalInterface")
        await _assert_refused(vpn_payload(ends=[tunnel_edge_port(), physical]), "tunnel interfaces")

    @pytest.mark.asyncio
    async def test_a_vpn_needs_its_namespace(self) -> None:
        """No ip_namespace, no pool: refused before any write."""
        await _assert_refused(vpn_payload(namespace=None), "needs its ip_namespace")


class TestFailures:
    @pytest.mark.asyncio
    async def test_provider_assigned_without_a_link_prefix_is_refused(self) -> None:
        """The flavour says the provider assigns the range; the circuit has none."""
        await _assert_refused(handoff_payload(connection=connection_node(profile="azure")), "has no link_prefix")

    @pytest.mark.asyncio
    async def test_a_link_prefix_of_another_length_is_refused(self) -> None:
        """A /29 handed to an Azure (/30) circuit is a data error."""
        await _assert_refused(
            handoff_payload(connection=connection_node(profile="azure"), link_prefix=f"{ASSIGNED}/29"),
            "is a /29, the provider's link range is a /30",
        )

    @pytest.mark.asyncio
    async def test_a_missing_link_prefix_node_is_refused(self) -> None:
        """The link_prefix id points at nothing: refused, nothing written."""
        client = FakeClient()
        with pytest.raises(GeneratorError, match="link_prefix link-prefix-1 not found"):
            await _run(
                client, handoff_payload(connection=connection_node(profile="azure"), link_prefix=f"{ASSIGNED}/30")
            )
        assert client.created == []

    @pytest.mark.asyncio
    async def test_a_changed_prefix_length_is_reported_never_reallocated(self) -> None:
        """The first run allocated a /31; a flavour that now says /30 is refused with the remedy, nothing re-allocated."""
        client = FakeClient()
        await _run(client, vpn_payload())
        before = [w[:2] for w in client.writes]
        connection = connection_node(path_type="internet_vpn", profile="zscaler", via="saas")

        with pytest.raises(
            GeneratorError, match=r"already allocated with another length than the flavour's /30.*vc-p2p__vc-1"
        ):
            await _run(client, vpn_payload(connection=connection))

        assert client.prefixes["vc-p2p__vc-1"].prefix.value == f"{BASE}/31"
        assert [w[:2] for w in client.writes][: len(before)] == before
        assert not any(action == "track" and label.endswith("/30") for action, label, _ in client.writes)
