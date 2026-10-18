"""Unit tests for external eBGP peers inside the tenant VRFs (cloud VIFs, SD-WAN gateway).

Covers:
  - _build_session_from_peering()  — ``external`` flag: peering_role regular in a prod/non_prod VRF
  - get_external_peer_prefixes()   — the far end's registered prefixes, per handoff sub-interface
                                     (a cloud VIF's reachable VPC blocks, the LANs and hosted
                                     segments of the offices behind an SD-WAN gateway)
  - get_onprem_prefixes()          — per-VRF on-prem segment prefixes announced to those peers
  - Edge transform                 — RM-<peer>-IN / RM-<VRF>-EXTERNAL-OUT on edges/cisco_nxos.j2
  - 30_all data                    — the cloud VIF and SD-WAN handoffs are wired for that mode; every
                                     hub VPC is reachable from on-prem; GW1's offices register PROD LANs
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from transforms.config.edge import Edge
from transforms.helpers.bgp import _build_session_from_peering, get_external_peer_prefixes, get_onprem_prefixes

_ROOT = Path(__file__).parents[2]
_FIXTURE = _ROOT / "tests" / "smoke" / "configs" / "edge_cisco_nxos_external_vrf_peers" / "input.json"
_INTERCONNECTS = _ROOT / "data" / "demos" / "30_all" / "08_interconnects"
_PROD = {"name": "PROD", "namespace_type": "prod"}
_NON_PROD = {"name": "NON-PROD", "namespace_type": "non_prod"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peering(namespace: dict[str, Any] | None, *, peering_role: str | None = "regular") -> dict[str, Any]:
    """The eg-fr01 <-> EXT-AWS-DX-FR2 VIF peering as the transform sees it, local address in ``namespace``."""
    return {
        "name": "VRF-PROD-EQXFR2-AWS-EUC1",
        "session_type": "EBGP",
        "peering_role": peering_role,
        "ttl": 1,
        "maximum_routes": 100,
        "capabilities": [
            {
                "name": "Ethernet1/13.1000",
                "device": {"name": "eg-fr01"},
                "ip_address": {"address": "10.255.5.5/30", "ip_namespace": namespace},
            },
            {
                "name": "et-0/0/0.1000",
                "device": {"name": "EXT-AWS-DX-FR2"},
                "ip_address": {"address": "10.255.5.6/30", "ip_namespace": namespace},
            },
        ],
        "bgp_processes": [
            {"local_as": {"asn": 4200000100}, "capabilities": [{"name": "eg-fr01"}]},
            {"local_as": {"asn": 64512}, "capabilities": [{"name": "EXT-AWS-DX-FR2"}]},
        ],
    }


def _blocks(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    """Cleaned cidr_blocks: (prefix, namespace name)."""
    return [{"prefix": prefix, "ip_namespace": {"name": ns}} for prefix, ns in pairs]


def _transit_vif(*, vlan: int = 1000, device: str = "eg-fr01", attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """A cleaned transit VIF on ``device`` whose hybrid attachment is associated with rt-shared."""
    return {
        "name": f"vif-transit-{vlan}-{device}",
        "vlan_id": vlan,
        "direct_connect": {"physical_devices": [{"name": device}]},
        "virtual_network": None,
        "transit_gateway_attachment": {
            "associated_route_table": {"id": "rt-shared"},
            "transit_gateway": {"attachments": attachments},
        },
    }


def _vpc_attachment(tables: list[str], *blocks: tuple[str, str]) -> dict[str, Any]:
    return {
        "attachment_type": "vpc",
        "propagates_to_route_tables": [{"id": table} for table in tables],
        "virtual_network": {"cidr_blocks": _blocks(*blocks)},
    }


def _subif(name: str, namespace: str | None) -> dict[str, Any]:
    return {"name": name, "ip_address": {"address": "10.255.5.5/30", "ip_namespace": {"name": namespace}}}


_GW1 = "EQX-FR2-SDWAN-GW1"
_NO_SDWAN: dict[str, Any] = {"device_capabilities": None, "offices": None}


def _bgp_facing(*ends: tuple[str, str]) -> list[dict[str, Any]]:
    """Cleaned device capabilities: one ManagedBGP whose peerings run eg-fr01 (local iface) -> remote device."""
    return [
        {"typename": "ManagedStandaloneVlanDomain", "name": "vlan-domain"},
        {
            "typename": "ManagedBGP",
            "peerings": [
                {
                    "name": f"peering-{local}-{remote}",
                    "capabilities": [
                        {"name": local, "device": {"name": "eg-fr01"}},
                        {"name": "eth0.1900", "device": {"name": remote}},
                    ],
                }
                for local, remote in ends
            ],
        },
    ]


def _office(gateway: str | None, *lans: tuple[str, str], hosted: tuple[tuple[str, str], ...] = ()) -> dict[str, Any]:
    """A cleaned TopologyCustomerOffice root entry: its SD-WAN gateway, (prefix, namespace) LANs and hosted segments.

    ``hosted`` are the (gateway prefix, namespace) of the segments the office hosts itself (segment_deployments).
    """
    return {
        "sdwan_gateway": {"name": gateway} if gateway else None,
        "lan_prefixes": _blocks(*lans),
        "segment_deployments": [
            {"segment": {"gateway": {"ip_prefix": {"prefix": prefix, "ip_namespace": {"name": namespace}}}}}
            for prefix, namespace in hosted
        ],
    }


def _segment(prefix: str, namespace: dict[str, Any], member_type: str = "address") -> dict[str, Any]:
    return {"gateway": {"ip_prefix": {"prefix": prefix, "member_type": member_type, "ip_namespace": namespace}}}


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


def _objects(directory: Path, kind: str) -> list[dict[str, Any]]:
    """Every data entry of ``kind`` in the YAML files under ``directory``."""
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc["spec"]["kind"] == kind:
                entries.extend(doc["spec"]["data"])
    return entries


# ---------------------------------------------------------------------------
# Session mode
# ---------------------------------------------------------------------------


class TestExternalFlag:
    @pytest.mark.parametrize("namespace", [_PROD, _NON_PROD])
    def test_regular_peer_in_a_tenant_vrf_is_external(self, namespace: dict[str, Any]) -> None:
        """A regular (partner/provider) eBGP peer in PROD or NON-PROD gets the filtered mode."""
        session = _build_session_from_peering(_peering(namespace), "eg-fr01", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == namespace["name"]
        assert session["external"] is True
        assert "transit" not in session

    def test_internet_peer_is_transit_not_external(self) -> None:
        """The INTERNET VRF keeps the transit mode (default in, public out)."""
        namespace = {"name": "INTERNET", "namespace_type": "internet"}
        session = _build_session_from_peering(_peering(namespace), "eg-fr01", {"asn": 4200000100}, [])
        assert session is not None
        assert session["transit"] is True
        assert "external" not in session

    @pytest.mark.parametrize(
        ("namespace", "peering_role"),
        [
            (_PROD, None),
            (_PROD, "dci"),
            ({"name": "MANAGEMENT", "namespace_type": "management"}, "regular"),
            ({"name": "PROD"}, "regular"),
        ],
    )
    def test_other_vrf_sessions_are_not_external(self, namespace: dict[str, Any], peering_role: str | None) -> None:
        """No regular role, a non-tenant VRF or an unknown namespace type: rendered as before, unfiltered."""
        session = _build_session_from_peering(
            _peering(namespace, peering_role=peering_role), "eg-fr01", {"asn": 4200000100}, []
        )
        assert session is not None
        assert "external" not in session

    def test_default_namespace_is_not_a_vrf_session(self) -> None:
        """A global-table session is never external, whatever its role."""
        session = _build_session_from_peering(
            _peering({"name": "default"}), "eg-fr01", {"asn": 4200000100}, [_subif("Ethernet1/13.1000", "default")]
        )
        assert session is not None
        assert "vrf" not in session
        assert "external" not in session


# ---------------------------------------------------------------------------
# Inbound: the far end's registered prefixes
# ---------------------------------------------------------------------------


class TestExternalPeerPrefixes:
    def test_transit_vif_accepts_only_vpcs_on_its_route_table_in_its_namespace(self) -> None:
        """rt-shared VPCs in PROD only: an isolated-table VPC and a NON-PROD block are not accepted."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD")),
                _vpc_attachment(["rt-isolated"], ("10.150.0.0/16", "PROD")),
                _vpc_attachment(["rt-shared"], ("10.60.0.0/16", "NON-PROD")),
                {"attachment_type": "hybrid", "propagates_to_route_tables": [{"id": "rt-shared"}]},
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr01", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": ["10.30.0.0/16"]}

    def test_private_vif_accepts_its_own_vpc_blocks_in_its_namespace(self) -> None:
        """A NON-PROD sub-interface never accepts the PROD block of the same VPC."""
        vif = {
            "name": "vif-private",
            "vlan_id": 1001,
            "direct_connect": {"physical_devices": [{"name": "eg-fr01"}]},
            "virtual_network": {"cidr_blocks": _blocks(("10.60.0.0/16", "NON-PROD"), ("10.31.0.0/16", "PROD"))},
        }
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1001", "NON-PROD")], [vif], "eg-fr01", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1001": ["10.60.0.0/16"]}

    def test_default_route_and_ipv6_are_never_accepted(self) -> None:
        """A registered 0/0 (or any IPv6 block) is dropped from the inbound list."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(
                    ["rt-shared"], ("0.0.0.0/0", "PROD"), ("2001:db8::/56", "PROD"), ("10.30.0.0/16", "PROD")
                )
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr01", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": ["10.30.0.0/16"]}

    def test_vif_on_another_device_or_vlan_is_ignored(self) -> None:
        """Only this device's Direct Connects, matched on the sub-interface's dot1q tag."""
        vifs = [
            _transit_vif(device="eg-fr02", attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))]),
            _transit_vif(vlan=1002, attachments=[_vpc_attachment(["rt-shared"], ("10.40.0.0/16", "PROD"))]),
        ]
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13", "PROD"), _subif("Loopback0", None)]
        assert get_external_peer_prefixes(interfaces, vifs, "eg-fr01", **_NO_SDWAN) == {}

    def test_two_vifs_on_one_vlan_accept_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ambiguous VLAN on one device: fail closed and say so."""
        vifs = [
            _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))]),
            {**_transit_vif(attachments=[]), "name": "vif-other"},
        ]
        with caplog.at_level(logging.WARNING):
            found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], vifs, "eg-fr01", **_NO_SDWAN)
        assert found == {}
        assert "accepts nothing" in caplog.text

    def test_hybrid_attachment_without_route_table_accepts_nothing(self) -> None:
        """No associated route table: nothing is reachable over the VIF."""
        vif = _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))])
        vif["transit_gateway_attachment"]["associated_route_table"] = None
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr01", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": []}

    def test_no_virtual_interfaces_is_empty(self) -> None:
        """A sub-interface with neither a VIF nor an SD-WAN gateway behind it is absent."""
        assert get_external_peer_prefixes([_subif("Ethernet1/13.1900", "PROD")], None, "eg-fr01", **_NO_SDWAN) == {}


class TestSdwanGatewayPrefixes:
    """The SD-WAN handoff accepts the LANs of the offices behind the gateway it faces."""

    def test_offices_behind_the_gateway_are_accepted_sorted(self) -> None:
        """The union of every GW1 office's PROD LAN, in address order, deduplicated."""
        offices = [
            _office(_GW1, ("10.210.5.0/24", "PROD")),
            _office(_GW1, ("10.210.2.0/24", "PROD"), ("10.210.5.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.210.2.0/24", "10.210.5.0/24"]}

    def test_offices_of_another_gateway_or_none_are_excluded(self) -> None:
        """Only offices whose sdwan_gateway is the far end of this handoff count."""
        offices = [
            _office(_GW1, ("10.210.5.0/24", "PROD")),
            _office("EQX-AM5-SDWAN-GW1", ("10.210.9.0/24", "PROD")),
            _office(None, ("10.210.15.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.210.5.0/24"]}

    def test_wrong_namespace_default_and_ipv6_lans_are_excluded(self) -> None:
        """A LAN registered in another namespace, a 0/0 or an IPv6 LAN never reaches the PROD filter."""
        offices = [
            _office(
                _GW1,
                ("10.210.5.0/24", "PROD"),
                ("10.211.5.0/24", "NON-PROD"),
                ("0.0.0.0/0", "PROD"),
                ("2001:db8:210::/64", "PROD"),
            )
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.210.5.0/24"]}

    def test_non_prod_session_never_gets_prod_lans(self) -> None:
        """A NON-PROD handoff to the same gateway accepts only NON-PROD LANs (here: none)."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1901", "NON-PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1901", _GW1)),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"), ("10.210.2.0/24", "PROD"))],
        )
        assert found == {"Ethernet1/13.1901": []}

    def test_gateway_whose_offices_register_no_lan_accepts_nothing(self) -> None:
        """A known gateway with no registered LAN maps to [] (deny), not to a VIF guess."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[_office(_GW1)],
        )
        assert found == {"Ethernet1/13.1900": []}

    def test_segments_hosted_by_an_office_behind_the_gateway_are_accepted(self) -> None:
        """An office that hosts its LAN as a segment registers the segment's prefix, not a typed lan_prefixes entry."""
        offices = [
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
            _office(_GW1, ("10.210.2.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.176.5.0/24", "10.210.2.0/24"]}

    def test_hosted_segments_and_lans_of_one_office_are_one_deduplicated_set(self) -> None:
        offices = [
            _office(_GW1, ("10.176.5.0/24", "PROD"), hosted=(("10.176.5.0/24", "PROD"), ("10.176.6.0/24", "PROD")))
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.176.5.0/24", "10.176.6.0/24"]}

    def test_hosted_segments_of_another_gateway_or_none_are_excluded(self) -> None:
        offices = [
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
            _office("EQX-AM5-SDWAN-GW1", hosted=(("10.176.9.0/24", "PROD"),)),
            _office(None, hosted=(("10.176.15.0/24", "PROD"),)),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.176.5.0/24"]}

    def test_hosted_segments_in_another_namespace_default_or_ipv6_never_reach_the_filter(self) -> None:
        """A hosted NON-PROD segment, a segment without a gateway prefix and an IPv6 one are not accepted on PROD."""
        offices = [
            _office(
                _GW1,
                hosted=(
                    ("10.176.5.0/24", "PROD"),
                    ("10.184.5.0/24", "NON-PROD"),
                    ("2001:db8:176::/64", "PROD"),
                ),
            )
        ]
        offices[0]["segment_deployments"].append({"segment": {"gateway": None}})
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/13.1901", "NON-PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/13.1901", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": ["10.176.5.0/24"], "Ethernet1/13.1901": ["10.184.5.0/24"]}

    def test_an_office_without_hosted_segments_keeps_its_lans(self) -> None:
        """A footprint with no segment_deployments key at all (an older query) registers its lan_prefixes alone."""
        office = {"sdwan_gateway": {"name": _GW1}, "lan_prefixes": _blocks(("10.210.2.0/24", "PROD"))}
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[office],
        )
        assert found == {"Ethernet1/13.1900": ["10.210.2.0/24"]}

    def test_no_offices_leaves_the_handoff_absent(self) -> None:
        """No office footprint at all: the gateway is not known, so the handoff accepts nothing."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[],
        )
        assert found == {}

    def test_handoff_facing_a_non_gateway_device_gets_no_office_lans(self) -> None:
        """Office LANs go only to the session whose far end is their gateway, not to any PROD handoff."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/14.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/14.1900", "EXT-PARTNER-01")),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
        )
        assert found == {"Ethernet1/13.1900": ["10.210.5.0/24"]}

    def test_handoff_with_several_far_ends_is_not_resolved_to_a_gateway(self) -> None:
        """Ambiguous far end on one sub-interface: fail closed rather than trust one of them."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/13.1900", "EXT-PARTNER-01")),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
        )
        assert found == {}

    def test_malformed_peering_raises(self) -> None:
        """A peering without exactly two ends is refused, as when the session itself is built."""
        capabilities = _bgp_facing(("Ethernet1/13.1900", _GW1))
        capabilities[1]["peerings"][0]["capabilities"].pop()
        with pytest.raises(ValueError, match="expected 2 capabilities"):
            get_external_peer_prefixes(
                [_subif("Ethernet1/13.1900", "PROD")],
                None,
                "eg-fr01",
                device_capabilities=capabilities,
                offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
            )


# ---------------------------------------------------------------------------
# Outbound: the namespace's on-prem prefixes
# ---------------------------------------------------------------------------


class TestOnpremPrefixes:
    def test_groups_segment_prefixes_by_tenant_vrf(self) -> None:
        """PROD and NON-PROD keep separate lists; containers, exchange VRFs, IPv6 and 0/0 are left out."""
        segments = [
            _segment("10.5.20.0/24", _PROD),
            _segment("10.5.10.0/24", _PROD),
            _segment("10.5.10.0/24", _PROD),
            _segment("10.5.0.0/16", _PROD, member_type="prefix"),
            _segment("2001:db8:5::/64", _PROD),
            _segment("0.0.0.0/0", _PROD),
            _segment("10.6.10.0/24", _NON_PROD),
            _segment("10.250.0.0/24", {"name": "MANAGEMENT", "namespace_type": "management"}),
            _segment("192.0.2.0/24", {"name": "INTERNET", "namespace_type": "internet"}),
            {"gateway": None},
        ]
        assert get_onprem_prefixes(segments) == {
            "NON-PROD": ["10.6.10.0/24"],
            "PROD": ["10.5.10.0/24", "10.5.20.0/24"],
        }

    def test_no_segments_is_empty(self) -> None:
        assert get_onprem_prefixes(None) == {}


# ---------------------------------------------------------------------------
# Rendering (edges/cisco_nxos.j2 + common/cisco_nxos_bgp.j2)
# ---------------------------------------------------------------------------


class TestEdgeRender:
    def test_cloud_session_in_the_env_vrf_with_filters_and_max_prefix(self) -> None:
        """AC11: the VIF session sits in VRF PROD with RM-<peer>-IN, the VRF's out map, max-prefix and BFD."""
        output = _run_edge(_fixture())
        prod = output.split("  vrf PROD\n", 1)[1].split("  !\n", 1)[0]
        block = _neighbor_block(output, "10.255.5.6")
        assert "    neighbor 10.255.5.6\n" in prod
        assert "      remote-as 64512\n" in block
        assert "      bfd\n" in block
        assert "        maximum-prefix 100\n" in block
        assert "        route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-IN in\n" in block
        assert "        route-map RM-PROD-EXTERNAL-OUT out\n" in block

    def test_inbound_prefix_list_is_exactly_the_reachable_vpc_blocks(self) -> None:
        """One entry per registered block of that namespace; never 0/0 in any external filter."""
        output = _run_edge(_fixture())
        assert re.findall(r"^ip prefix-list PL-VRF-PROD-EQXFR2-AWS-EUC1-IN .*$", output, re.M) == [
            "ip prefix-list PL-VRF-PROD-EQXFR2-AWS-EUC1-IN seq 10 permit 10.30.0.0/16"
        ]
        assert re.findall(r"^ip prefix-list PL-VRF-NONPROD-EQXFR2-AWS-EUC1-IN .*$", output, re.M) == [
            "ip prefix-list PL-VRF-NONPROD-EQXFR2-AWS-EUC1-IN seq 10 permit 10.60.0.0/16"
        ]
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

    def test_sdwan_peer_accepts_the_lans_of_the_offices_behind_its_gateway(self) -> None:
        """The SD-WAN handoff permits exactly GW1's PROD office LANs and hosted segments, never another gateway's or NON-PROD."""
        output = _run_edge(_fixture())
        assert re.findall(r"^ip prefix-list PL-VRF-PROD-EQXFR2-SDWAN-IN .*$", output, re.M) == [
            "ip prefix-list PL-VRF-PROD-EQXFR2-SDWAN-IN seq 10 permit 10.176.5.0/24",
            "ip prefix-list PL-VRF-PROD-EQXFR2-SDWAN-IN seq 20 permit 10.210.2.0/24",
        ]
        assert "route-map RM-VRF-PROD-EQXFR2-SDWAN-IN permit 10\n" in output
        for excluded in ("10.211.2.0/24", "10.210.9.0/24", "10.210.15.0/24"):
            assert excluded not in output
        block = _neighbor_block(output, "10.255.5.1")
        assert "        route-map RM-VRF-PROD-EQXFR2-SDWAN-IN in\n" in block
        assert "        maximum-prefix 100\n" in block

    def test_peer_without_registered_prefixes_is_accepted_nothing(self) -> None:
        """No office footprint registered: the SD-WAN inbound map denies everything."""
        data = _fixture()
        data["TopologyCustomerOffice"] = {"edges": []}
        output = _run_edge(data)
        assert "route-map RM-VRF-PROD-EQXFR2-SDWAN-IN deny 10\n" in output
        assert "PL-VRF-PROD-EQXFR2-SDWAN-IN" not in output
        assert "        route-map RM-VRF-PROD-EQXFR2-SDWAN-IN in\n" in _neighbor_block(output, "10.255.5.1")

    def test_outbound_is_per_vrf_and_never_mixes_environments(self) -> None:
        """PROD announces only PROD's segment prefixes, NON-PROD only its own."""
        output = _run_edge(_fixture())
        prod = re.findall(r"^ip prefix-list PL-PROD-ONPREM seq \d+ permit (\S+)$", output, re.M)
        non_prod = re.findall(r"^ip prefix-list PL-NON-PROD-ONPREM seq \d+ permit (\S+)$", output, re.M)
        assert prod == ["10.5.10.0/24", "10.5.20.0/24"]
        assert non_prod == ["10.6.10.0/24"]
        assert "route-map RM-PROD-EXTERNAL-OUT deny 20" in output
        assert "        route-map RM-NON-PROD-EXTERNAL-OUT out\n" in _neighbor_block(output, "10.255.6.6")

    def test_vrf_without_onprem_prefixes_announces_nothing(self) -> None:
        """No known on-prem prefix: the out map is a bare deny."""
        data = _fixture()
        data["ManagedNetworkSegment"] = {"edges": []}
        output = _run_edge(data)
        assert "route-map RM-PROD-EXTERNAL-OUT deny 10\n" in output
        assert "PL-PROD-ONPREM" not in output

    def test_other_vrf_sessions_render_unfiltered_as_before(self) -> None:
        """A non-regular VRF session keeps the old stanza: no route-maps, no filter definitions."""
        data = _fixture()
        bgp = data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"][-1]["node"]
        for edge in bgp["peerings"]["edges"]:
            edge["node"]["peering_role"] = {"value": None}
        output = _run_edge(data)
        assert "EXTERNAL-OUT" not in output
        assert "-IN in" not in output
        assert "ip prefix-list" not in output
        assert "        maximum-prefix 100\n" in _neighbor_block(output, "10.255.5.6")


# ---------------------------------------------------------------------------
# 30_all data
# ---------------------------------------------------------------------------


def _external_handoffs() -> list[dict[str, Any]]:
    """30_all peerings with peering_role regular whose eg-* end sits in PROD/NON-PROD."""
    addresses = {
        (iface["device"], iface["name"]): iface["ip_address"]["data"].get("ip_namespace")
        for iface in _objects(_INTERCONNECTS, "DcimVirtualInterface")
        if (iface.get("ip_address") or {}).get("data")
    }
    return [
        {**peering, "namespaces": [addresses.get(tuple(end)) for end in peering["capabilities"]]}
        for peering in _objects(_INTERCONNECTS, "ManagedBGPPeering")
        if peering.get("peering_role") == "regular"
        and any(addresses.get(tuple(end)) in ("PROD", "NON-PROD") for end in peering["capabilities"])
    ]


class TestAllDemoData:
    def test_the_cloud_vif_and_sdwan_handoffs_are_external_peers(self) -> None:
        """Both tenant-VRF eBGP handoffs of 30_all are in the external mode's scope."""
        assert sorted(p["name"] for p in _external_handoffs()) == ["VRF-PROD-EQXFR2-AWS-EUC1", "VRF-PROD-EQXFR2-SDWAN"]

    @pytest.mark.parametrize("peering", _external_handoffs(), ids=lambda p: p["name"])
    def test_handoff_has_max_prefix_and_one_namespace(self, peering: dict[str, Any]) -> None:
        """max-prefix is set, and both ends sit in one namespace: a handoff never mixes environments."""
        assert isinstance(peering.get("maximum_routes"), int) and peering["maximum_routes"] > 0
        assert len(set(peering["namespaces"])) == 1, peering["namespaces"]

    def test_vif_handoff_is_port_dot_vlan_on_the_direct_connect_device(self) -> None:
        """The session's sub-interface is <DX port>.<vif.vlan_id> on the DX's physical device, in PROD."""
        (vif,) = [v for v in _objects(_INTERCONNECTS, "CloudVirtualInterface") if v["name"] == "vif-transit-hub-euc1"]
        (dx,) = [d for d in _objects(_INTERCONNECTS, "CloudDirectConnect") if d["name"] == vif["direct_connect"]]
        (peering,) = [p for p in _external_handoffs() if p["name"] == "VRF-PROD-EQXFR2-AWS-EUC1"]
        (xc,) = [c for c in _objects(_INTERCONNECTS, "TopologyPhysicalCircuit") if c["name"] == "XC-EQXFR2-AWS-EUC1"]
        device, interface = peering["capabilities"][0]
        port, _, vlan = interface.rpartition(".")
        assert device in dx["physical_devices"]
        assert [device, port] in xc["capabilities"]
        assert int(vlan) == vif["vlan_id"]
        assert peering["namespaces"] == ["PROD", "PROD"]

    def test_vif_accounts_are_prod_like_the_handoff(self) -> None:
        """Every account behind the PROD VIF is environment p (a non-prod account needs its own VIF)."""
        accounts = {
            account["name"]: account["environment"]
            for account in _objects(_ROOT / "data" / "demos" / "30_all", "CloudAccount")
        }
        attachments = _objects(_INTERCONNECTS / "02_cloud_hub", "CloudTransitGatewayAttachment")
        assert {accounts[a["owner_account"]] for a in attachments} == {"p"}

    def test_every_hub_vpc_propagates_into_the_hybrid_attachments_table(self) -> None:
        """On-prem traffic is looked up in the hybrid attachment's table, so every VPC must propagate there.

        This is what makes novantis (associated with rt-hub-isolated) reachable from on-prem, and its
        10.150.0.0/16 accepted on the colo edge's transit VIF session.
        """
        attachments = _objects(_INTERCONNECTS / "02_cloud_hub", "CloudTransitGatewayAttachment")
        (hybrid,) = [a for a in attachments if a["attachment_type"] == "hybrid"]
        vpcs = [a for a in attachments if a["attachment_type"] == "vpc"]
        assert vpcs
        missing = [a["name"] for a in vpcs if hybrid["associated_route_table"] not in a["propagates_to_route_tables"]]
        assert missing == []

    def test_offices_behind_the_sdwan_handoff_gateway_register_lans_in_its_namespace(self) -> None:
        """The SD-WAN handoff's far end has offices, and each of their LANs is in the handoff's namespace.

        Office footprints are created in 06_customer_boarding with lan_prefixes and sdwan_gateway set
        inline (the gateway loads with the foundation stage). An office without a LAN adds nothing.
        """
        (peering,) = [p for p in _external_handoffs() if p["name"] == "VRF-PROD-EQXFR2-SDWAN"]
        (gateway,) = [device for device, _ in peering["capabilities"] if not device.startswith("eg-")]
        behind: list[list[list[str]]] = []
        for customer in _objects(
            _ROOT / "data" / "demos" / "30_all" / "06_customer_boarding", "TopologyOfficeCustomer"
        ):
            for child in customer.get("children") or []:
                if isinstance(child, dict) and child.get("kind") == "TopologyCustomerOffice":
                    entries = child["data"]
                    for office in entries if isinstance(entries, list) else [entries]:
                        if office.get("sdwan_gateway") == gateway:
                            behind.append(office.get("lan_prefixes") or [])
        registered = [prefix for office_lans in behind for prefix in office_lans]
        assert behind and registered
        assert {namespace for _, namespace in registered} == set(peering["namespaces"])


class TestOfficeQuerySelection:
    """The edge query selects what get_external_peer_prefixes reads off an office, in the shape it expects."""

    def test_the_office_root_selects_the_gateway_the_lans_and_the_hosted_segment_prefixes(self) -> None:
        text = (_ROOT / "queries" / "config" / "edge.gql").read_text()
        office = text.split("  TopologyCustomerOffice {", 1)[1]

        assert "sdwan_gateway { node { name { value } } }" in office
        assert "lan_prefixes {" in office
        deployments = office.split("segment_deployments(", 1)[1]
        assert deployments.startswith('status__values: ["active", "provisioning"])')
        assert (
            "gateway { node { ip_prefix { node { prefix { value } ip_namespace { node { name { value } } } } } } }"
            in (deployments)
        )

    def test_the_selected_fields_exist_on_the_office_schema(self) -> None:
        """segment_deployments comes from TopologySegmentHosting, which the office inherits."""
        from tests.integration.demo_data import relationship_names

        assert {"sdwan_gateway", "lan_prefixes", "segment_deployments"} <= relationship_names("TopologyCustomerOffice")

    def test_a_cleaned_query_response_has_the_shape_the_helper_reads(self) -> None:
        """clean_data turns the selection into segment_deployments[i].segment.gateway.ip_prefix.{prefix, ip_namespace}."""
        from utils.data_cleaning import clean_data

        raw = {
            "TopologyCustomerOffice": {
                "edges": [
                    {
                        "node": {
                            "sdwan_gateway": {"node": {"name": {"value": _GW1}}},
                            "lan_prefixes": {"edges": []},
                            "segment_deployments": {
                                "edges": [
                                    {
                                        "node": {
                                            "segment": {
                                                "node": {
                                                    "gateway": {
                                                        "node": {
                                                            "ip_prefix": {
                                                                "node": {
                                                                    "prefix": {"value": "10.176.5.0/24"},
                                                                    "ip_namespace": {
                                                                        "node": {"name": {"value": "PROD"}}
                                                                    },
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }

        offices = clean_data(raw)["TopologyCustomerOffice"]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr01",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )

        assert found == {"Ethernet1/13.1900": ["10.176.5.0/24"]}
