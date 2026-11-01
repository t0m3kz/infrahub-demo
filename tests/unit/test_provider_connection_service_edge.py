"""The provider's service edge: ONE CloudNetworkAppliance per (provider, region) with a virtual NIC per connection leg.

A cloud region hosts appliances (``appliances``) and has no ``devices`` key. A dedicated link's far carrier end is a
``DcimVirtualInterface`` NIC of the appliance: the far ``<nic>.<vlan>`` sub-interface rides on it through
``parent_interface``; a VPN's ``Tunnel<n>`` hangs on a NIC when the appliance has one.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.provider_connection import choose_tunnel_parent, parse_connection, plan_links
from generators.logger import GeneratorError
from tests.unit import provider_connection_helpers as h

SERVICE_EDGE = "AWS-EUC1-SE"
SERVICE_EDGE_ID = f"dev-{SERVICE_EDGE}"


def _nic_carrier(index: int, nic: str) -> dict[str, Any]:
    """A virtual connection from our cage edge to one NIC of the region's service edge appliance."""
    return h.carrier(
        f"vc-{index}",
        f"VC-{index}",
        [
            h.port(f"near-port-{index}", f"Ethernet1/{index}", f"eg-{index}", h.NEAR),
            h.port(nic, nic, SERVICE_EDGE, h.FAR, processes=[h.bgp()]),
        ],
        circuit_type="virtual_connection",
    )


def _connection(name: str, index: int, nic: str, **kwargs: Any) -> dict[str, Any]:
    payload = h.connection(name=name, carriers=[_nic_carrier(index, nic)], **kwargs)
    payload["TopologyProviderConnection"][0]["id"] = name
    return payload


async def _run(payload: dict[str, Any], client: h.FakeClient | None = None) -> h.FakeClient:
    client = client or h.FakeClient()
    await h.make_generator(client).generate(payload)
    return client


class TestRegionHasNoDevices:
    def test_the_far_region_of_the_payload_lists_only_appliances_and_the_stub_is_found_there(self) -> None:
        payload = h.connection(carriers=[_nic_carrier(1, "vnic-fr2")])
        far = payload["TopologyProviderConnection"][0]["far"]

        assert "devices" not in far
        assert [stub["id"] for stub in far["appliances"]] == [SERVICE_EDGE_ID]
        _, (link,) = plan_links(parse_connection(payload["TopologyProviderConnection"][0]))
        assert (link.far.device_id, link.far.port.name if link.far.port else None) == (SERVICE_EDGE_ID, "vnic-fr2")

    def test_a_stub_listed_under_devices_is_still_found_for_an_external_provider(self) -> None:
        raw = h.connection(carriers=[_nic_carrier(1, "vnic-fr2")])["TopologyProviderConnection"][0]
        raw["far"] = {"id": h.FAR, "typename": "TopologyExternalProviders", "devices": raw["far"].pop("appliances")}

        _, (link,) = plan_links(parse_connection(raw))

        assert link.far.device_id == SERVICE_EDGE_ID


class TestPlanRefusals:
    def test_no_appliance_in_the_far_region_is_refused(self) -> None:
        raw = h.connection(carriers=[_nic_carrier(1, "vnic-fr2")], stubs=[])["TopologyProviderConnection"][0]

        with pytest.raises(ValueError, match="no provider appliance or device"):
            plan_links(parse_connection(raw))

    def test_a_far_end_that_is_not_on_a_stub_of_the_far_facility_is_refused(self) -> None:
        other = h.stub_device("dev-other", "OTHER")
        raw = h.connection(carriers=[_nic_carrier(1, "vnic-fr2")], stubs=[other])["TopologyProviderConnection"][0]

        with pytest.raises(ValueError, match="not on a provider appliance or device of the far facility"):
            plan_links(parse_connection(raw))

    def test_two_candidate_appliances_for_a_vpn_are_refused_without_the_provider_as_to_choose(self) -> None:
        stubs = [h.stub_device(), h.stub_device("dev-2", "EXT-2", [h.bgp("bgp-2", "as-other")])]
        raw = h.connection(
            path_type="internet_vpn",
            internet=[h.internet_carrier("edge-1")],
            stubs=stubs,
            link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        )["TopologyProviderConnection"][0]

        with pytest.raises(ValueError, match="exactly one provider stub"):
            plan_links(parse_connection(raw))


class TestSharedServiceEdge:
    @pytest.mark.asyncio
    async def test_two_connections_of_one_region_get_distinct_nics_and_sub_interfaces_on_one_appliance(self) -> None:
        first = await _run(_connection("PC-A", 1, "vnic-a", vlan_id=100))
        second = await _run(_connection("PC-B", 2, "vnic-b", vlan_id=100))

        far = [
            data
            for client in (first, second)
            for _, data in client.of("create", "DcimVirtualInterface")
            if data["device"]["id"] == SERVICE_EDGE_ID
        ]
        # same appliance, same VLAN, distinct NICs: distinct sub-interfaces, each riding its own NIC
        assert [(d["name"], d["parent_interface"]) for d in far] == [
            ("vnic-a.100", {"id": "vnic-a"}),
            ("vnic-b.100", {"id": "vnic-b"}),
        ]

    @pytest.mark.asyncio
    async def test_a_second_run_with_the_appliance_stub_writes_nothing(self) -> None:
        first = await _run(_connection("PC-A", 1, "vnic-a", vlan_id=100))
        assert first.writes > 0
        existing = h.session(
            "PC-A-L1",
            ends=[h.end("s-near", "Ethernet1/1.100", "eg-1"), h.end("s-far", "vnic-a.100", SERVICE_EDGE)],
            session_id="sess-1",
        )
        existing["connection"] = "PC-A"
        subs = [h.sub("s-near", "Ethernet1/1.100", "near-port-1"), h.sub("s-far", "vnic-a.100", "vnic-a")]
        client = h.FakeClient(subs=subs)

        await _run(_connection("PC-A", 1, "vnic-a", vlan_id=100, sessions=[existing]), client)

        assert client.log == []
        assert set(client.group_context.related_node_ids) == {"PC-A", "s-near", "s-far", "sess-1"}


class TestFailuresWriteNothing:
    @pytest.mark.asyncio
    async def test_no_appliance_in_the_far_region_fails_before_the_first_write(self) -> None:
        client = h.FakeClient()
        payload = h.connection(carriers=[_nic_carrier(1, "vnic-fr2")], stubs=[])

        with pytest.raises(GeneratorError):
            await _run(payload, client)

        assert client.log == []

    @pytest.mark.asyncio
    async def test_two_candidate_appliances_for_a_vpn_fail_before_the_first_write(self) -> None:
        client = h.FakeClient()
        stubs = [h.stub_device(), h.stub_device("dev-2", "EXT-2", [h.bgp("bgp-2", "as-other")])]
        payload = h.connection(
            path_type="internet_vpn",
            internet=[h.internet_carrier("edge-1")],
            stubs=stubs,
            link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        )

        with pytest.raises(GeneratorError):
            await _run(payload, client)

        assert client.log == []


class TestTunnelOnTheApplianceNic:
    def _vpn(self) -> dict[str, Any]:
        return h.connection(
            path_type="internet_vpn",
            internet=[h.internet_carrier("edge-1")],
            stubs=[h.stub_device("dev-stub", "EXT-STUB")],
            link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        )

    @pytest.mark.asyncio
    async def test_the_far_tunnels_ride_the_first_nic_of_the_appliance(self) -> None:
        client = h.FakeClient()
        client.nics["dev-stub"] = ["Loopback0", "vnic-b", "vnic-a"]

        await _run(self._vpn(), client)

        far = [data for _, data in client.of("create", "DcimVirtualInterface") if data["device"]["id"] == "dev-stub"]
        assert [d["parent_interface"] for d in far] == [{"id": "nic-vnic-a"}, {"id": "nic-vnic-a"}]

    @pytest.mark.asyncio
    async def test_an_appliance_without_a_nic_keeps_a_free_standing_tunnel(self) -> None:
        client = h.FakeClient()
        client.nics["dev-stub"] = ["Loopback0"]

        await _run(self._vpn(), client)

        far = [data for _, data in client.of("create", "DcimVirtualInterface") if data["device"]["id"] == "dev-stub"]
        assert all("parent_interface" not in d for d in far)

    def test_loopbacks_tunnels_and_sub_interfaces_are_no_nic(self) -> None:
        assert choose_tunnel_parent(["Loopback0", "Tunnel1", "vnic-a.100"]) is None
        assert choose_tunnel_parent(["vnic-b", "vnic-a"]) == "vnic-a"
