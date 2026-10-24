"""Unit tests for rendering external VRF peers: the NX-OS edge, platform refusals and hosted appliances."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.external_vrf_peers_helpers import _ROOT
from transforms.common import UNFILTERED_BANNER_PLATFORMS
from transforms.config.edge import Edge

_FIXTURE = _ROOT / "tests" / "smoke" / "configs" / "edge_cisco_nxos_external_vrf_peers" / "input.json"


def _run_edge(data: dict[str, Any]) -> str:
    """Render the Edge transform on raw (uncleaned) query data."""
    client = MagicMock()
    client.clone.return_value = client
    client.schema = MagicMock()
    client.schema.get = AsyncMock(return_value=MagicMock())
    instance = Edge(client=client, infrahub_node=MagicMock(), root_directory=str(_ROOT))
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def _fixture() -> dict[str, Any]:
    return copy.deepcopy(json.loads(_FIXTURE.read_text()))


def _neighbor_block(output: str, neighbor: str) -> str:
    """The `neighbor <ip>` stanza up to the next neighbor or VRF end."""
    match = re.search(rf"^    neighbor {re.escape(neighbor)}\n(.*?)(?=^    neighbor |^  !)", output, re.M | re.S)
    assert match, f"neighbor {neighbor} not rendered"
    return match.group(1)


def _set_far_end_kind(data: dict[str, Any], peering_name: str, kind: str) -> dict[str, Any]:
    """Set the device ``__typename`` of the far end (second end) of the edge's peering ``peering_name``."""
    bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
    (peering,) = [e["node"] for e in bgp["peerings"]["edges"] if e["node"]["name"]["value"] == peering_name]
    peering["capabilities"]["edges"][1]["node"]["device"]["node"]["__typename"] = kind
    return data


_NE_HOP = "HANDOFF-FR2-AMNE-PROD"


# ---------------------------------------------------------------------------
# Rendering (edges/cisco_nxos.j2 + common/cisco_nxos_bgp.j2)
# ---------------------------------------------------------------------------


def _prefix_list(output: str, name: str) -> list[str]:
    """The prefixes of prefix-list ``name``, in sequence order."""
    return re.findall(rf"^ip prefix-list {re.escape(name)} seq \d+ permit (\S+)$", output, re.M)


class TestEdgeRender:
    def test_cloud_session_in_the_env_vrf_with_filters_and_max_prefix(self) -> None:
        """AC11: the VIF session sits in VRF PROD with its own RM-<session>-IN/-OUT, max-prefix and BFD."""
        output = _run_edge(_fixture())
        prod = output.split("  vrf PROD\n", 1)[1].split("  !\n", 1)[0]
        block = _neighbor_block(output, "10.255.5.6")
        assert "    neighbor 10.255.5.6\n" in prod
        assert "      remote-as 64512\n" in block
        assert "      bfd\n" in block
        assert "        maximum-prefix 100\n" in block
        assert "        route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-IN in\n" in block
        assert "        route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT out\n" in block
        assert "EXTERNAL-OUT" not in output

    def test_inbound_prefix_list_is_exactly_the_reachable_vpc_blocks_of_customers(self) -> None:
        """Both customers' rt-shared blocks; not the isolated one, the provider-owned one or a default."""
        output = _run_edge(_fixture())
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-IN") == ["10.30.0.0/16", "10.32.0.0/16"]
        assert _prefix_list(output, "PL-VRF-NONPROD-EQXFR2-AWS-EUC1-IN") == ["10.60.0.0/16"]
        for excluded in ("10.150.0.0/16", "10.33.0.0/16", "10.99.0.0/16", "10.31.0.0/16"):
            assert excluded not in output
        assert not [line for line in output.splitlines() if line.startswith("ip prefix-list") and "0.0.0.0/0" in line]

    def test_registered_default_route_is_not_accepted(self) -> None:
        """Even a VPC block of 0/0 in the data does not reach the inbound prefix-list."""
        data = _fixture()
        vpc = data["CloudVirtualInterface"]["edges"][0]["node"]["transit_gateway_attachment"]["node"]
        blocks = vpc["transit_gateway"]["node"]["attachments"]["edges"][1]["node"]["virtual_network"]["node"]
        blocks["cidr_blocks"]["edges"].append(
            {"node": {"prefix": {"value": "0.0.0.0/0"}, "ip_namespace": {"node": {"name": {"value": "PROD"}}}}}
        )
        assert "0.0.0.0/0" not in _run_edge(data)

    def test_sdwan_peer_accepts_what_the_offices_behind_its_gateway_registered(self) -> None:
        """C005's hosted segment, C002's and C003's LANs and C001's tunnel range: not the retired segment, the
        provider's LAN, another gateway's, the provider-owned and the NON-PROD tunnel's."""
        output = _run_edge(_fixture())
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-IN") == [
            "10.176.5.0/24",
            "10.210.2.0/24",
            "10.210.3.0/24",
            "198.18.0.0/24",
        ]
        assert "route-map RM-VRF-PROD-EQXFR2-SDWAN-IN permit 10\n" in output
        for excluded in (
            "10.211.2.0/24",
            "10.210.9.0/24",
            "10.210.15.0/24",
            "10.210.7.0/24",
            "10.176.6.0/24",
            "198.19.0.0/24",
            "198.20.0.0/24",
        ):
            assert excluded not in output
        block = _neighbor_block(output, "10.255.5.1")
        assert "        route-map RM-VRF-PROD-EQXFR2-SDWAN-IN in\n" in block
        assert "        maximum-prefix 100\n" in block
        assert "send-community" not in block

    def test_the_policy_names_its_owners(self) -> None:
        output = _run_edge(_fixture())
        assert "! VRF-PROD-EQXFR2-SDWAN: owners C001, C002, C003, C005\n" in output
        assert "! VRF-PROD-EQXFR2-AWS-EUC1: owners C003, C015\n" in output
        assert "! VRF-NONPROD-EQXFR2-AWS-EUC1: owners C003\n" in output

    def test_outbound_is_per_session_and_only_its_owners_prefixes(self) -> None:
        """Each session announces its owners' space in its own namespace (their segments and what they registered
        behind another session), nobody else's, and never what it learned itself."""
        output = _run_edge(_fixture())
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT") == [
            "10.5.10.0/24",
            "10.5.20.0/24",
            "10.7.10.0/24",
            "10.210.3.0/24",
        ]
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-OUT") == [
            "10.5.10.0/24",
            "10.5.20.0/24",
            "10.8.2.0/24",
            "10.9.1.0/24",
            "10.30.0.0/16",
        ]
        assert _prefix_list(output, "PL-VRF-NONPROD-EQXFR2-AWS-EUC1-OUT") == ["10.6.10.0/24"]
        assert "route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT deny 20" in output
        assert "        route-map RM-VRF-NONPROD-EQXFR2-AWS-EUC1-OUT out\n" in _neighbor_block(output, "10.255.6.6")
        assert "le " not in output and "ge " not in output

    def test_unregistered_retired_and_ownerless_space_is_never_announced(self) -> None:
        """C009 is behind no session; a retired, an ownerless and a NON-PROD segment of other owners stay home."""
        output = _run_edge(_fixture())
        out_lines = "\n".join(line for line in output.splitlines() if "-OUT seq" in line)
        for never in ("10.9.9.0/24", "10.5.99.0/24", "10.5.77.0/24", "10.6.20.0/24", "10.6.30.0/24", "10.250.0.0/24"):
            assert never not in out_lines

    def test_session_without_a_registered_owner_is_deny_all_both_ways(self) -> None:
        """The Megaport session: the comment says so, both maps deny, nothing is listed."""
        output = _run_edge(_fixture())
        assert (
            "! VRF-PROD-EQXFR2-MEGAPORT: no registered customer owner, announces nothing and accepts nothing\n"
            "route-map RM-VRF-PROD-EQXFR2-MEGAPORT-IN deny 10\n"
            "route-map RM-VRF-PROD-EQXFR2-MEGAPORT-OUT deny 10\n"
        ) in output
        assert "PL-VRF-PROD-EQXFR2-MEGAPORT" not in output
        block = _neighbor_block(output, "10.255.7.1")
        assert "        route-map RM-VRF-PROD-EQXFR2-MEGAPORT-IN in\n" in block
        assert "        route-map RM-VRF-PROD-EQXFR2-MEGAPORT-OUT out\n" in block

    def test_the_network_edge_hop_is_external_and_denied_both_ways(self) -> None:
        """eg-am-ne01 runs an overlay process but its device is a CloudNetworkAppliance: external, nothing registered."""
        output = _run_edge(_fixture())
        block = _neighbor_block(output, "10.255.8.1")
        assert "remote-as 4200000200" in block
        assert "        maximum-prefix 100\n" in block
        assert f"        route-map RM-{_NE_HOP}-IN in\n" in block
        assert f"        route-map RM-{_NE_HOP}-OUT out\n" in block
        assert (
            f"! {_NE_HOP}: no registered customer owner, announces nothing and accepts nothing\n"
            f"route-map RM-{_NE_HOP}-IN deny 10\n"
            f"route-map RM-{_NE_HOP}-OUT deny 10\n"
        ) in output
        assert f"PL-{_NE_HOP}" not in output

    def test_an_overlay_physical_device_far_end_stays_a_plain_vrf_neighbour(self) -> None:
        """The same hop to a DcimPhysicalDevice with an overlay process is ours: no route-maps, no filter lines."""
        output = _run_edge(_set_far_end_kind(_fixture(), _NE_HOP, "DcimPhysicalDevice"))
        block = _neighbor_block(output, "10.255.8.1")
        assert "remote-as 4200000200" in block
        assert "        maximum-prefix 100\n" in block
        assert "route-map" not in block
        assert _NE_HOP not in output.split("! Route-maps", 1)[1]
        assert f"RM-{_NE_HOP}" not in output
        assert f"PL-{_NE_HOP}" not in output
        assert f"! {_NE_HOP}:" not in output

    def test_a_far_end_without_a_selected_typename_is_not_an_appliance(self) -> None:
        """The older query shape (no __typename on the far end's device): the overlay process still makes it ours."""
        data = _fixture()
        bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
        for edge in bgp["peerings"]["edges"]:
            for end in edge["node"]["capabilities"]["edges"]:
                end["node"]["device"]["node"].pop("__typename", None)
        output = _run_edge(data)
        assert f"RM-{_NE_HOP}" not in output

    def test_every_external_session_has_exactly_one_comment_and_one_pair_of_maps(self) -> None:
        """Five external sessions: one owners / no-owner comment, one IN map and one OUT map each."""
        output = _run_edge(_fixture())
        sessions = [
            "VRF-PROD-EQXFR2-AWS-EUC1",
            "VRF-NONPROD-EQXFR2-AWS-EUC1",
            "VRF-PROD-EQXFR2-SDWAN",
            "VRF-PROD-EQXFR2-MEGAPORT",
            _NE_HOP,
        ]
        for name in sessions:
            assert len(re.findall(rf"^! {re.escape(name)}: ", output, re.M)) == 1
            assert len(re.findall(rf"^route-map RM-{re.escape(name)}-IN (?:permit|deny) 10$", output, re.M)) == 1
            assert len(re.findall(rf"^route-map RM-{re.escape(name)}-OUT (?:permit|deny) 10$", output, re.M)) == 1

    def test_the_render_is_byte_stable(self) -> None:
        """Idempotency: the same input renders the same bytes, however the dicts were ordered."""
        assert _run_edge(_fixture()) == _run_edge(_fixture())

    def test_two_owners_on_one_transit_vif_share_one_out_list_without_a_third_customer(self) -> None:
        """C003 and C015 behind the PROD transit VIF: one OUT list with both; C002 and C005 space is not in it."""
        output = _run_edge(_fixture())
        out = _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT")
        assert out == ["10.5.10.0/24", "10.5.20.0/24", "10.7.10.0/24", "10.210.3.0/24"]
        assert len(re.findall(r"^route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT permit 10$", output, re.M)) == 1
        for third in ("10.8.2.0/24", "10.176.5.0/24", "10.9.1.0/24"):
            assert third not in out

    def test_a_retired_segment_is_in_no_out_list(self) -> None:
        """10.5.99.0/24 is C003's but retired: in no PL-*-OUT, and the active sibling prefixes still are."""
        output = _run_edge(_fixture())
        out_prefix_lists = [line for line in output.splitlines() if re.match(r"ip prefix-list PL-\S+-OUT ", line)]
        assert out_prefix_lists
        assert not [line for line in out_prefix_lists if "10.5.99.0/24" in line]
        assert any("10.5.20.0/24" in line for line in out_prefix_lists)

    def test_peer_without_registered_prefixes_is_accepted_nothing(self) -> None:
        """No office footprint registered: the SD-WAN inbound map denies everything."""
        data = _fixture()
        data["TopologyCustomerOffice"] = {"edges": []}
        output = _run_edge(data)
        assert "route-map RM-VRF-PROD-EQXFR2-SDWAN-IN deny 10\n" in output
        assert "PL-VRF-PROD-EQXFR2-SDWAN-IN" not in output
        assert "        route-map RM-VRF-PROD-EQXFR2-SDWAN-IN in\n" in _neighbor_block(output, "10.255.5.1")

    def test_session_with_no_onprem_prefixes_announces_only_what_its_owners_registered_elsewhere(self) -> None:
        """No segment at all: an owner with nothing behind another session gets a bare deny; C003 still passes its
        VPC block to GW1 and its office LAN to the hub VIF."""
        data = _fixture()
        data["ManagedNetworkSegment"] = {"edges": []}
        output = _run_edge(data)
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT") == ["10.210.3.0/24"]
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-OUT") == ["10.30.0.0/16"]
        assert "route-map RM-VRF-NONPROD-EQXFR2-AWS-EUC1-OUT deny 10\n" in output
        assert "PL-VRF-NONPROD-EQXFR2-AWS-EUC1-OUT" not in output

    def test_the_gateway_session_is_announced_c003s_vpc_and_segments_but_nothing_of_c015(self) -> None:
        """C003 has an office behind GW1 and a VPC on the hub VIF: GW1 gets that VPC block and C003's segments."""
        output = _run_edge(_fixture())
        out = _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-OUT")
        assert {"10.30.0.0/16", "10.5.10.0/24", "10.5.20.0/24"} <= set(out)
        for c015 in ("10.7.10.0/24", "10.32.0.0/16", "10.150.0.0/16"):
            assert c015 not in out

    def test_the_hub_vif_session_is_announced_c003s_office_lan_and_both_owners_segments(self) -> None:
        """The hub VIF's owners are C003 and C015: C003's office LAN and both owners' segments, no one else's."""
        output = _run_edge(_fixture())
        out = _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT")
        assert {"10.210.3.0/24", "10.5.10.0/24", "10.7.10.0/24"} <= set(out)
        for other in ("10.210.2.0/24", "10.8.2.0/24", "10.176.5.0/24"):
            assert other not in out

    def test_a_prefix_learned_on_a_session_is_never_in_its_out(self) -> None:
        """C005's office-hosted segment is registered behind GW1: not announced back to it, not local anywhere."""
        output = _run_edge(_fixture())
        sdwan_out = _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-OUT")
        assert "10.176.5.0/24" in _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-IN")
        assert "10.176.5.0/24" not in sdwan_out
        assert not set(sdwan_out) & set(_prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-IN"))
        aws_out = _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT")
        assert not set(aws_out) & set(_prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-IN"))

    def test_an_owner_behind_one_session_only_announces_its_local_segments_there(self) -> None:
        """C002 sits behind GW1 alone: GW1 is announced its segment 10.8.2.0/24, and neither that segment nor its
        office LAN reaches another session."""
        output = _run_edge(_fixture())
        assert "10.8.2.0/24" in _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-OUT")
        for session in ("AWS-EUC1", "MEGAPORT"):
            assert "10.8.2.0/24" not in _prefix_list(output, f"PL-VRF-PROD-EQXFR2-{session}-OUT")
            assert "10.210.2.0/24" not in _prefix_list(output, f"PL-VRF-PROD-EQXFR2-{session}-OUT")

    def test_the_two_owners_of_one_vif_never_see_each_others_prefixes_elsewhere(self) -> None:
        """C015 is on the hub VIF only: it never reaches the GW1 or the NON-PROD session, and C003's office LAN
        reaches C015's session only because it is the same VIF as C003's."""
        output = _run_edge(_fixture())
        everywhere_else = "\n".join(
            line for line in output.splitlines() if "-OUT seq" in line and "VRF-PROD-EQXFR2-AWS-EUC1-OUT" not in line
        )
        assert "10.7.10.0/24" not in everywhere_else
        assert "10.32.0.0/16" not in everywhere_else
        assert "10.6.20.0/24" not in everywhere_else

    def test_other_vrf_sessions_render_unfiltered_as_before(self) -> None:
        """A non-regular VRF session keeps the old stanza: no route-maps, no filter definitions."""
        data = _fixture()
        bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
        for edge in bgp["peerings"]["edges"]:
            edge["node"]["peering_role"] = {"value": None}
        output = _run_edge(data)
        assert "-OUT" not in output
        assert "-IN in" not in output
        assert "ip prefix-list" not in output
        assert "        maximum-prefix 100\n" in _neighbor_block(output, "10.255.5.6")


# ---------------------------------------------------------------------------
# Refusal: a platform without the owner prefix filters
# ---------------------------------------------------------------------------


def _run_transform(transform: type, data: dict[str, Any]) -> str:
    """Render ``transform`` on raw (uncleaned) query data."""
    client = MagicMock()
    client.clone.return_value = client
    client.schema = MagicMock()
    client.schema.get = AsyncMock(return_value=MagicMock())
    instance = transform(client=client, infrahub_node=MagicMock(), root_directory=str(_ROOT))
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def _on_platform(data: dict[str, Any], netmiko: str) -> dict[str, Any]:
    data["DcimDevice"]["edges"][0]["node"]["platform"]["node"]["netmiko_device_type"] = {"value": netmiko}
    return data


class TestRefusalWithoutOwnerFilters:
    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_xe", "sonic", "nokia_sros"])
    def test_an_edge_that_cannot_filter_an_external_session_is_refused(self, platform: str) -> None:
        """No RM-<session>-* on these templates: refuse rather than render an open session."""
        data = _on_platform(_fixture(), platform)
        with pytest.raises(
            ValueError, match=rf"dc1-edge-01: {platform} edge cannot render the owner prefix filters"
        ) as err:
            _run_transform(Edge, data)
        message = str(err.value)
        assert "VRF-PROD-EQXFR2-AWS-EUC1 (EXT-AWS-DX-FR2)" in message
        assert "VRF-PROD-EQXFR2-MEGAPORT (EXT-MEGAPORT-MCR1)" in message
        assert f"{_NE_HOP} (eg-am-ne01)" in message  # a hosted appliance hop is external too
        assert message.endswith("refusing an open session")

    def test_a_cisco_nxos_border_leaf_is_refused(self) -> None:
        """The NX-OS border-leaf template includes cisco_nxos_bgp.j2 but defines none of the maps."""
        from transforms.config.border_leaf import BorderLeaf

        data = _fixture()
        data["DcimDevice"]["edges"][0]["node"]["role"] = {"value": "border-leaf"}
        with pytest.raises(ValueError, match="cisco_nxos border-leaf cannot render the owner prefix filters"):
            _run_transform(BorderLeaf, data)

    def test_a_cisco_nxos_edge_renders(self) -> None:
        assert "route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT out" in _run_edge(_fixture())

    def test_an_edge_with_only_internal_sessions_is_not_refused(self) -> None:
        """Drop every external session from the peerings: nothing to filter, nothing to refuse.

        cisco_xe is the only other platform that has an edge template (the others are refused before a
        template is looked up, whatever the sessions).
        """
        data = _on_platform(_fixture(), "cisco_xe")
        bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
        bgp["peerings"]["edges"] = [
            edge for edge in bgp["peerings"]["edges"] if edge["node"]["name"]["value"] == _NE_HOP
        ]
        _set_far_end_kind(data, _NE_HOP, "DcimPhysicalDevice")
        assert _run_transform(Edge, data)


def _bgp_node(data: dict[str, Any]) -> dict[str, Any]:
    """The edge's last ManagedBGP capability node of a fixture."""
    return data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]


def _wan_edges(data: dict[str, Any], *names: str) -> dict[str, Any]:
    """Set deployment.lisp_wan_edges (raw query shape) to ``names``."""
    data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["lisp_wan_edges"] = {
        "edges": [{"node": {"name": {"value": name}}} for name in names]
    }
    return data


_EXTERNAL_FAR_ENDS = ("EXT-AWS-DX-FR2", "EQX-FR2-SDWAN-GW1", "EXT-MEGAPORT-MCR1", "eg-am-ne01")


class TestRefusalScope:
    """What is, and what is not, an external session for the refusal (cisco_xe: the platform without the maps)."""

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_xe", "sonic", "nokia_sros"])
    def test_the_message_names_device_platform_session_and_far_end(self, platform: str) -> None:
        """Every refused platform gets the same message: device, platform, role, each session with its far end."""
        with pytest.raises(ValueError) as err:
            _run_transform(Edge, _on_platform(_fixture(), platform))
        assert str(err.value).startswith(f"dc1-edge-01: {platform} edge cannot render the owner prefix filters of ")
        for session, far_end in (
            ("VRF-PROD-EQXFR2-AWS-EUC1", "EXT-AWS-DX-FR2"),
            ("VRF-NONPROD-EQXFR2-AWS-EUC1", "EXT-AWS-DX-FR2"),
            ("VRF-PROD-EQXFR2-SDWAN", "EQX-FR2-SDWAN-GW1"),
            ("VRF-PROD-EQXFR2-MEGAPORT", "EXT-MEGAPORT-MCR1"),
            (_NE_HOP, "eg-am-ne01"),
        ):
            assert f"{session} ({far_end})" in str(err.value)

    def test_a_cisco_nxos_border_leaf_message_names_the_sessions(self) -> None:
        """The NX-OS border leaf shares the BGP template but not the maps: refused with the same detail."""
        from transforms.config.border_leaf import BorderLeaf

        data = _fixture()
        data["DcimDevice"]["edges"][0]["node"]["role"] = {"value": "border-leaf"}
        with pytest.raises(ValueError) as err:
            _run_transform(BorderLeaf, data)
        assert str(err.value).startswith("dc1-edge-01: cisco_nxos border-leaf cannot render the owner prefix filters")
        assert "VRF-PROD-EQXFR2-MEGAPORT (EXT-MEGAPORT-MCR1)" in str(err.value)

    def test_own_devices_from_the_deployment_make_every_session_internal(self) -> None:
        """All external far ends are the deployment's SD-WAN edges: no external session, so cisco_xe renders."""
        data = _wan_edges(_on_platform(_fixture(), "cisco_xe"), *_EXTERNAL_FAR_ENDS)
        assert _run_transform(Edge, data)

    def test_one_far_end_missing_from_lisp_wan_edges_is_still_refused(self) -> None:
        """The edge list is the only exemption: a single external far end left out brings the refusal back."""
        data = _wan_edges(_on_platform(_fixture(), "cisco_xe"), "EXT-AWS-DX-FR2", "EXT-MEGAPORT-MCR1", "eg-am-ne01")
        with pytest.raises(ValueError) as err:
            _run_transform(Edge, data)
        assert "VRF-PROD-EQXFR2-SDWAN (EQX-FR2-SDWAN-GW1)" in str(err.value)
        assert "EXT-AWS-DX-FR2" not in str(err.value)
        assert "MEGAPORT" not in str(err.value)

    def test_a_nxos_edge_with_own_devices_drops_their_route_maps(self) -> None:
        """On the platform that has the maps, an own device's session renders as a plain neighbour."""
        output = _run_edge(_wan_edges(_fixture(), "EQX-FR2-SDWAN-GW1"))
        assert "RM-VRF-PROD-EQXFR2-SDWAN" not in output
        assert "PL-VRF-PROD-EQXFR2-SDWAN" not in output
        assert "route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT out" in output

    def test_a_vrf_session_without_the_regular_role_is_not_refused(self) -> None:
        """Only a regular peer is external: with no peering role the old unfiltered stanza needs no maps."""
        data = _on_platform(_fixture(), "cisco_xe")
        for edge in _bgp_node(data)["peerings"]["edges"]:
            edge["node"]["peering_role"] = {"value": "overlay"}
        assert _run_transform(Edge, data)

    def test_an_external_session_without_a_far_end_name_is_refused_as_unknown(self) -> None:
        """The refusal helper names a session even when its far end has no name."""
        transform = Edge(client=MagicMock(), infrahub_node=MagicMock(), root_directory=str(_ROOT))
        config = {"name": "dc1-edge-01", "bgp": [{"sessions": [{"name": "S1", "external": True}]}]}
        with pytest.raises(ValueError, match=r"S1 \(unknown far end\)"):
            transform._refuse_unfiltered_external_sessions("sonic", config, "DcimPhysicalDevice")

    @pytest.mark.parametrize(
        "bgp",
        [None, [], [{}], [{"sessions": None}], [{"sessions": []}], [{"sessions": [{"name": "S", "vrf": "PROD"}]}]],
        ids=["no-bgp", "empty", "no-sessions-key", "null-sessions", "empty-sessions", "internal-vrf-session"],
    )
    def test_a_config_without_an_external_session_is_never_refused(self, bgp: Any) -> None:
        """No external session, whatever the platform: nothing to refuse (the check returns None)."""
        transform = Edge(client=MagicMock(), infrahub_node=MagicMock(), root_directory=str(_ROOT))
        assert transform._refuse_unfiltered_external_sessions("arista_eos", {"name": "d", "bgp": bgp}, None) is None

    def test_only_cisco_nxos_defines_the_maps_for_an_edge(self) -> None:
        """The platform allow-list is exactly cisco_nxos: adding one without its template would open sessions."""
        assert Edge.external_policy_platforms == frozenset({"cisco_nxos"})


_APPLIANCE_FIXTURE = _ROOT / "tests" / "smoke" / "configs" / "edge_cisco_xe_unfiltered_appliance" / "input.json"
_BANNER = (
    "UNFILTERED: no external-policy renderer for {platform}; this session announces and accepts everything "
    "the peer sends - provider-side filter required"
)


def _appliance_fixture() -> dict[str, Any]:
    """The hosted-appliance smoke input: eg-am-ne02, CloudNetworkAppliance, cisco_xe, one external and one internal session."""
    return copy.deepcopy(json.loads(_APPLIANCE_FIXTURE.read_text()))


class TestHostedApplianceRender:
    """A CloudNetworkAppliance is never refused: on a banner platform its external sessions render a warning comment."""

    def test_the_banner_is_rendered_once_above_the_external_neighbour_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One banner, directly above the external session's first neighbor line; none for the internal session."""
        with caplog.at_level(logging.DEBUG):
            output = _run_transform(Edge, _appliance_fixture())
        banner = _BANNER.format(platform="cisco_xe")
        assert output.count(banner) == 1
        lines = output.splitlines()
        at = lines.index(f"    ! {banner}")
        assert lines[at + 1] == "    neighbor 10.255.9.1 remote-as 64512"
        assert lines[at - 1].startswith("    neighbor 10.255.8.0 ")  # the internal session comes before, unmarked
        assert "UNFILTERED" not in "\n".join(line for line in lines if "10.255.8.0" in line)
        assert "RM-HANDOFF" not in output

    def test_a_warning_is_logged_and_no_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """The SDK turns an ERROR record into a failed artifact: the hosted appliance logs a WARNING only."""
        with caplog.at_level(logging.DEBUG):
            _run_transform(Edge, _appliance_fixture())
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "transforms.common"]
        assert len(warnings) == 1
        assert "HANDOFF-AMNE-AWS-EUC1-PROD" in warnings[0].getMessage()
        assert "EXT-AWS-DX-FR2" in warnings[0].getMessage()
        assert "provider-side filter required" in warnings[0].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_every_external_session_gets_its_own_banner(self) -> None:
        """Two external sessions: two banners, each above its own neighbor."""
        data = _appliance_fixture()
        _set_far_end_kind(data, "HANDOFF-FR2-AMNE-PROD", "DcimPhysicalDevice")
        bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
        for edge in bgp["peerings"]["edges"]:
            for end in edge["node"]["capabilities"]["edges"]:
                end["node"]["device"]["node"]["__typename"] = "DcimPhysicalDevice"
            for process in edge["node"]["bgp_processes"]["edges"]:
                process["node"].pop("process_role", None)
        output = _run_transform(Edge, data)
        assert output.count(_BANNER.format(platform="cisco_xe")) == 2

    def test_a_physical_device_with_an_external_session_on_cisco_xe_is_refused(self) -> None:
        """Only the appliance is let through: the same data on a DcimPhysicalDevice raises."""
        data = _appliance_fixture()
        data["DcimDevice"]["edges"][0]["node"]["__typename"] = "DcimPhysicalDevice"
        with pytest.raises(ValueError, match="cisco_xe edge cannot render the owner prefix filters"):
            _run_transform(Edge, data)

    def test_a_missing_device_typename_is_refused(self) -> None:
        """Fail-closed: without the device kind the device is not known to be an appliance."""
        data = _appliance_fixture()
        del data["DcimDevice"]["edges"][0]["node"]["__typename"]
        with pytest.raises(ValueError, match="cisco_xe edge cannot render the owner prefix filters"):
            _run_transform(Edge, data)

    def test_an_appliance_on_a_platform_without_the_banner_is_refused(self) -> None:
        """arista_eos has no banner in its template: an appliance there still raises."""
        data = _on_platform(_appliance_fixture(), "arista_eos")
        with pytest.raises(ValueError, match="arista_eos edge cannot render the owner prefix filters"):
            _run_transform(Edge, data)

    def test_the_banner_platforms_are_exactly_the_ones_using_the_ios_bgp_template(self) -> None:
        """Adding a platform here without its template change would silently drop the warning."""
        assert UNFILTERED_BANNER_PLATFORMS == frozenset({"cisco_ios", "cisco_xe"})

    def test_an_appliance_with_only_internal_sessions_gets_no_banner_and_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No external session: nothing to warn about."""
        data = _appliance_fixture()
        bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
        bgp["peerings"]["edges"] = [e for e in bgp["peerings"]["edges"] if e["node"]["name"]["value"] == _NE_HOP]
        with caplog.at_level(logging.DEBUG):
            output = _run_transform(Edge, data)
        assert "UNFILTERED" not in output
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_the_nxos_edge_does_not_get_a_banner_for_an_appliance_it_filters(self) -> None:
        """An appliance on cisco_nxos is filtered with route-maps, never marked unfiltered."""
        data = _on_platform(_fixture(), "cisco_nxos")
        data["DcimDevice"]["edges"][0]["node"]["__typename"] = "CloudNetworkAppliance"
        assert "UNFILTERED" not in _run_edge(data)
