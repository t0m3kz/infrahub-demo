"""The provider's service edge: ONE CloudNetworkAppliance per (provider, region) with a virtual NIC per connection leg.

A cloud region hosts appliances (``appliances``) and has no ``devices`` key. A dedicated link's far carrier end is a
``DcimVirtualInterface`` NIC of the appliance: the far ``<nic>.<vlan>`` sub-interface rides on it through
``parent_interface``; a VPN's ``Tunnel<n>`` hangs on a NIC when the appliance has one.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.provider_connection import choose_stub, choose_tunnel_parent, parse_connection
from generators.logger import GeneratorError
from tests.unit import provider_connection_helpers as h

SERVICE_EDGE = h.SERVICE_EDGE[1]
SERVICE_EDGE_ID = h.SERVICE_EDGE[0]


async def _run(payload: dict[str, Any], client: h.FakeClient | None = None) -> h.FakeClient:
    client = client or h.FakeClient()
    await h.make_generator(client).generate(payload)
    return client


class TestRegionHasNoDevices:
    def test_the_far_region_of_the_payload_lists_only_appliances_and_the_stub_is_found_there(self) -> None:
        payload = h.connection()
        far = payload["TopologyProviderConnection"][0]["far"]

        assert "devices" not in far
        assert [stub["id"] for stub in far["appliances"]] == [SERVICE_EDGE_ID]
        assert choose_stub(parse_connection(payload["TopologyProviderConnection"][0])).id == SERVICE_EDGE_ID

    def test_a_stub_listed_under_devices_is_still_found_for_an_external_provider(self) -> None:
        raw = h.connection()["TopologyProviderConnection"][0]
        raw["far"] = {"id": h.FAR, "typename": "TopologyExternalProviders", "devices": raw["far"].pop("appliances")}

        assert choose_stub(parse_connection(raw)).id == SERVICE_EDGE_ID


class TestPlanRefusals:
    def test_no_appliance_in_the_far_region_is_refused(self) -> None:
        raw = h.connection(stubs=[])["TopologyProviderConnection"][0]

        with pytest.raises(ValueError, match="no provider appliance or device"):
            choose_stub(parse_connection(raw))

    def test_two_candidate_appliances_are_refused_without_the_provider_as_to_choose(self) -> None:
        stubs = [h.stub_device(), h.stub_device("dev-2", "EXT-2", [h.bgp("bgp-2", "as-other")])]
        raw = h.connection(stubs=stubs)["TopologyProviderConnection"][0]

        with pytest.raises(ValueError, match="exactly one provider stub"):
            choose_stub(parse_connection(raw))

    def test_the_provider_as_chooses_among_two_appliances(self) -> None:
        stubs = [h.stub_device(), h.stub_device("dev-2", "EXT-2", [h.bgp("bgp-2", "as-other")])]
        raw = h.connection(stubs=stubs, provider_as=h.AWS_AS)["TopologyProviderConnection"][0]

        assert choose_stub(parse_connection(raw)).name == SERVICE_EDGE


class TestSharedServiceEdge:
    @pytest.mark.asyncio
    async def test_two_connections_of_one_region_get_distinct_nics_and_sub_interfaces_on_one_appliance(self) -> None:
        first = await _run(h.connection(name="PC-A", conn_id="c-a", vlan_id=100))
        second = await _run(h.connection(name="PC-B", conn_id="c-b", vlan_id=100))

        nic_ids = {
            data["data"]["name"]: data["id"]
            for client in (first, second)
            for _, data in client.of("save", "DcimVirtualInterface")
            if data["data"]["name"].startswith("vnic-") and "." not in data["data"]["name"]
        }
        far = [
            data
            for client in (first, second)
            for _, data in client.of("create", "DcimVirtualInterface")
            if data["device"]["id"] == SERVICE_EDGE_ID and "." in data["name"]
        ]
        # same appliance, same VLAN, distinct NICs: distinct sub-interfaces, each riding its own NIC
        assert [(d["name"], d["parent_interface"]) for d in far] == [
            ("vnic-PC-A.100", {"id": nic_ids["vnic-PC-A"]}),
            ("vnic-PC-B.100", {"id": nic_ids["vnic-PC-B"]}),
        ]

    @pytest.mark.asyncio
    async def test_a_second_run_with_the_appliance_stub_writes_nothing(self) -> None:
        near = h.pid("eg-1", "Ethernet1/1")
        nic = h.pid("AWS-SE", "vnic-PC-A")
        client = h.FakeClient(subs=[h.sub("s-near", "Ethernet1/1.100", near), h.sub("s-far", "vnic-PC-A.100", nic)])
        client.add_far_end("vnic-PC-A")
        existing = h.session(
            "PC-A-L1",
            ends=[h.end("s-near", "Ethernet1/1.100", "eg-1"), h.end("s-far", "vnic-PC-A.100", SERVICE_EDGE)],
            session_id="sess-1",
        )
        existing["connection"] = "c-a"
        own = h.own_carrier(far_if="vnic-PC-A")

        await _run(h.connection(name="PC-A", conn_id="c-a", vlan_id=100, carriers=[own], sessions=[existing]), client)

        assert client.log == []
        assert set(client.group_context.related_node_ids) == {"c-a", "s-near", "s-far", "sess-1"}


class TestFailuresWriteNothing:
    @pytest.mark.asyncio
    async def test_no_appliance_in_the_far_region_fails_before_the_first_write(self) -> None:
        client = h.FakeClient()
        payload = h.connection(stubs=[])

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
            link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        )

    @pytest.mark.asyncio
    async def test_the_far_tunnels_ride_the_first_nic_of_the_appliance(self) -> None:
        client = h.FakeClient()
        client.nics["dev-AWS-SE"] = ["Loopback0", "vnic-b", "vnic-a"]

        await _run(self._vpn(), client)

        far = [data for _, data in client.of("create", "DcimVirtualInterface") if data["device"]["id"] == "dev-AWS-SE"]
        assert [d["parent_interface"] for d in far] == [{"id": "nic-vnic-a"}, {"id": "nic-vnic-a"}]

    @pytest.mark.asyncio
    async def test_an_appliance_without_a_nic_keeps_a_free_standing_tunnel(self) -> None:
        client = h.FakeClient()
        client.nics["dev-AWS-SE"] = ["Loopback0"]

        await _run(self._vpn(), client)

        far = [data for _, data in client.of("create", "DcimVirtualInterface") if data["device"]["id"] == "dev-AWS-SE"]
        assert all("parent_interface" not in d for d in far)

    def test_loopbacks_tunnels_and_sub_interfaces_are_no_nic(self) -> None:
        assert choose_tunnel_parent(["Loopback0", "Tunnel1", "vnic-a.100"]) is None
        assert choose_tunnel_parent(["vnic-b", "vnic-a"]) == "vnic-a"
