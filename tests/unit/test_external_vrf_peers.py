"""Unit tests for external eBGP peers inside the tenant VRFs (cloud VIFs, SD-WAN gateway).

Covers:
  - _build_session_from_peering()  — ``external`` flag: peering_role regular in a prod/non_prod VRF
  - get_external_peer_prefixes()   — the far end's registered prefixes per handoff sub-interface and
                                     owner (a cloud VIF's reachable VPC blocks, the LANs and hosted
                                     segments of the offices behind an SD-WAN gateway); only a
                                     customer-owned prefix counts
  - get_onprem_prefixes()          — per-VRF, per-owner on-prem segment prefixes (retired and
                                     ownerless segments left out)
  - get_external_session_policies() — owners / in / out of one session: the same-owner exchange between
                                     the external sessions of a namespace (utils/ownership.py, tested
                                     in test_ownership.py), conflicts logged as warnings
  - Internal vs external           — a far end that is one of our devices is not an external peer
  - Edge transform                 — RM-<session>-IN / RM-<session>-OUT on edges/cisco_nxos.j2; the
                                     refusal on platforms without the owner prefix filters
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

from transforms.common import UNFILTERED_BANNER_PLATFORMS, _own_devices
from transforms.config.edge import Edge
from transforms.helpers.bgp import (
    _build_session_from_peering,
    _far_end_is_ours,
    get_external_peer_prefixes,
    get_external_session_policies,
    get_onprem_prefixes,
)
from utils.ownership import customer_owner

_ROOT = Path(__file__).parents[2]
_FIXTURE = _ROOT / "tests" / "smoke" / "configs" / "edge_cisco_nxos_external_vrf_peers" / "input.json"
_INTERCONNECTS = _ROOT / "data" / "demos" / "30_all" / "08_interconnects"
_PROD = {"name": "PROD", "namespace_type": "prod"}
_NON_PROD = {"name": "NON-PROD", "namespace_type": "non_prod"}


def _owner(org_id: str, typename: str = "OrganizationCustomer") -> dict[str, Any]:
    """A cleaned owner dict as the edge query returns it."""
    return {"typename": typename, "org_id": org_id, "display_label": f"{org_id} label"}


_C2, _C3, _C5, _C15 = _owner("C002"), _owner("C003"), _owner("C005"), _owner("C015")
_PROVIDER = _owner("P027", "OrganizationProvider")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peering(namespace: dict[str, Any] | None, *, peering_role: str | None = "regular") -> dict[str, Any]:
    """The eg-fr201 <-> EXT-AWS-DX-FR2 VIF peering as the transform sees it, local address in ``namespace``."""
    return {
        "name": "VRF-PROD-EQXFR2-AWS-EUC1",
        "session_type": "EBGP",
        "peering_role": peering_role,
        "ttl": 1,
        "maximum_routes": 100,
        "capabilities": [
            {
                "name": "Ethernet1/13.1000",
                "device": {"name": "eg-fr201"},
                "ip_address": {"address": "10.255.5.5/30", "ip_namespace": namespace},
            },
            {
                "name": "et-0/0/0.1000",
                "device": {"name": "EXT-AWS-DX-FR2"},
                "ip_address": {"address": "10.255.5.6/30", "ip_namespace": namespace},
            },
        ],
        "bgp_processes": [
            {"local_as": {"asn": 4200000100}, "capabilities": [{"name": "eg-fr201"}]},
            {"local_as": {"asn": 64512}, "capabilities": [{"name": "EXT-AWS-DX-FR2"}]},
        ],
    }


def _blocks(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    """Cleaned cidr_blocks: (prefix, namespace name)."""
    return [{"prefix": prefix, "ip_namespace": {"name": ns}} for prefix, ns in pairs]


def _transit_vif(*, vlan: int = 1000, device: str = "eg-fr201", attachments: list[dict[str, Any]]) -> dict[str, Any]:
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


def _vpc_attachment(tables: list[str], *blocks: tuple[str, str], owner: dict[str, Any] | None = _C3) -> dict[str, Any]:
    """A VPC attachment propagating to ``tables``; its account is owned by ``owner``."""
    return {
        "attachment_type": "vpc",
        "propagates_to_route_tables": [{"id": table} for table in tables],
        "virtual_network": {"cidr_blocks": _blocks(*blocks), "account": {"owner": owner}},
    }


def _subif(name: str, namespace: str | None) -> dict[str, Any]:
    return {"name": name, "ip_address": {"address": "10.255.5.5/30", "ip_namespace": {"name": namespace}}}


_GW1 = "EQX-FR2-SDWAN-GW1"
_NO_SDWAN: dict[str, Any] = {"device_capabilities": None, "offices": None}


def _bgp_facing(*ends: tuple[str, str]) -> list[dict[str, Any]]:
    """Cleaned device capabilities: one ManagedBGP whose peerings run eg-fr201 (local iface) -> remote device."""
    return [
        {"typename": "ManagedStandaloneVlanDomain", "name": "vlan-domain"},
        {
            "typename": "ManagedBGP",
            "peerings": [
                {
                    "name": f"peering-{local}-{remote}",
                    "capabilities": [
                        {"name": local, "device": {"name": "eg-fr201"}},
                        {"name": "eth0.1900", "device": {"name": remote}},
                    ],
                }
                for local, remote in ends
            ],
        },
    ]


def _office(
    gateway: str | None,
    *lans: tuple[str, str],
    hosted: tuple[tuple[str, str], ...] = (),
    owner: dict[str, Any] | None = _C2,
    segment_owner: dict[str, Any] | None = _C2,
    segment_status: str = "active",
) -> dict[str, Any]:
    """A cleaned TopologyCustomerOffice root entry: its SD-WAN gateway, (prefix, namespace) LANs and hosted segments.

    ``hosted`` are the (gateway prefix, namespace) of the segments the office hosts itself (segment_deployments),
    all owned by ``segment_owner`` with ``segment_status``; the LANs are owned by the office's ``owner``.
    """
    return {
        "owner": owner,
        "sdwan_gateway": {"name": gateway} if gateway else None,
        "lan_prefixes": _blocks(*lans),
        "segment_deployments": [
            {
                "segment": {
                    "status": segment_status,
                    "owner": segment_owner,
                    "gateway": {"ip_prefix": {"prefix": prefix, "ip_namespace": {"name": namespace}}},
                }
            }
            for prefix, namespace in hosted
        ],
    }


def _segment(
    prefix: str,
    namespace: dict[str, Any],
    member_type: str = "address",
    *,
    owner: dict[str, Any] | None = _C3,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "status": status,
        "owner": owner,
        "gateway": {"ip_prefix": {"prefix": prefix, "member_type": member_type, "ip_namespace": namespace}},
    }


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
        session = _build_session_from_peering(_peering(namespace), "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == namespace["name"]
        assert session["external"] is True
        assert "transit" not in session

    def test_internet_peer_is_transit_not_external(self) -> None:
        """The INTERNET VRF keeps the transit mode (default in, public out)."""
        namespace = {"name": "INTERNET", "namespace_type": "internet"}
        session = _build_session_from_peering(_peering(namespace), "eg-fr201", {"asn": 4200000100}, [])
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
            _peering(namespace, peering_role=peering_role), "eg-fr201", {"asn": 4200000100}, []
        )
        assert session is not None
        assert "external" not in session

    def test_default_namespace_is_not_a_vrf_session(self) -> None:
        """A global-table session is never external, whatever its role."""
        session = _build_session_from_peering(
            _peering({"name": "default"}), "eg-fr201", {"asn": 4200000100}, [_subif("Ethernet1/13.1000", "default")]
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
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}

    def test_private_vif_accepts_its_own_vpc_blocks_in_its_namespace(self) -> None:
        """A NON-PROD sub-interface never accepts the PROD block of the same VPC."""
        vif = {
            "name": "vif-private",
            "vlan_id": 1001,
            "direct_connect": {"physical_devices": [{"name": "eg-fr201"}]},
            "virtual_network": {
                "cidr_blocks": _blocks(("10.60.0.0/16", "NON-PROD"), ("10.31.0.0/16", "PROD")),
                "account": {"owner": _C3},
            },
        }
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1001", "NON-PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}

    def test_default_route_and_ipv6_are_never_accepted(self) -> None:
        """A registered 0/0 (or any IPv6 block) is dropped from the inbound list."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(
                    ["rt-shared"], ("0.0.0.0/0", "PROD"), ("2001:db8::/56", "PROD"), ("10.30.0.0/16", "PROD")
                )
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}

    def test_vif_on_another_device_or_vlan_is_ignored(self) -> None:
        """Only this device's Direct Connects, matched on the sub-interface's dot1q tag."""
        vifs = [
            _transit_vif(device="eg-fr202", attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))]),
            _transit_vif(vlan=1002, attachments=[_vpc_attachment(["rt-shared"], ("10.40.0.0/16", "PROD"))]),
        ]
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13", "PROD"), _subif("Loopback0", None)]
        assert get_external_peer_prefixes(interfaces, vifs, "eg-fr201", **_NO_SDWAN) == {}

    def test_two_vifs_on_one_vlan_accept_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ambiguous VLAN on one device: fail closed and say so."""
        vifs = [
            _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))]),
            {**_transit_vif(attachments=[]), "name": "vif-other"},
        ]
        with caplog.at_level(logging.WARNING):
            found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], vifs, "eg-fr201", **_NO_SDWAN)
        assert found == {}
        assert "accepts nothing" in caplog.text

    def test_hybrid_attachment_without_route_table_accepts_nothing(self) -> None:
        """No associated route table: nothing is reachable over the VIF."""
        vif = _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))])
        vif["transit_gateway_attachment"]["associated_route_table"] = None
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {}}

    def test_no_virtual_interfaces_is_empty(self) -> None:
        """A sub-interface with neither a VIF nor an SD-WAN gateway behind it is absent."""
        assert get_external_peer_prefixes([_subif("Ethernet1/13.1900", "PROD")], None, "eg-fr201", **_NO_SDWAN) == {}


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
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.2.0/24", "10.210.5.0/24"]}}

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
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.5.0/24"]}}

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
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.5.0/24"]}}

    def test_non_prod_session_never_gets_prod_lans(self) -> None:
        """A NON-PROD handoff to the same gateway accepts only NON-PROD LANs (here: none)."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1901", "NON-PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1901", _GW1)),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"), ("10.210.2.0/24", "PROD"))],
        )
        assert found == {"Ethernet1/13.1901": {}}

    def test_gateway_whose_offices_register_no_lan_accepts_nothing(self) -> None:
        """A known gateway with no registered LAN maps to [] (deny), not to a VIF guess."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[_office(_GW1)],
        )
        assert found == {"Ethernet1/13.1900": {}}

    def test_segments_hosted_by_an_office_behind_the_gateway_are_accepted(self) -> None:
        """An office that hosts its LAN as a segment registers the segment's prefix, not a typed lan_prefixes entry."""
        offices = [
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
            _office(_GW1, ("10.210.2.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24", "10.210.2.0/24"]}}

    def test_hosted_segments_and_lans_of_one_office_are_one_deduplicated_set(self) -> None:
        offices = [
            _office(_GW1, ("10.176.5.0/24", "PROD"), hosted=(("10.176.5.0/24", "PROD"), ("10.176.6.0/24", "PROD")))
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24", "10.176.6.0/24"]}}

    def test_hosted_segments_of_another_gateway_or_none_are_excluded(self) -> None:
        offices = [
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
            _office("EQX-AM5-SDWAN-GW1", hosted=(("10.176.9.0/24", "PROD"),)),
            _office(None, hosted=(("10.176.15.0/24", "PROD"),)),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24"]}}

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
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/13.1901", _GW1)),
            offices=offices,
        )
        assert found == {
            "Ethernet1/13.1900": {"C002": ["10.176.5.0/24"]},
            "Ethernet1/13.1901": {"C002": ["10.184.5.0/24"]},
        }

    def test_an_office_without_hosted_segments_keeps_its_lans(self) -> None:
        """A footprint with no segment_deployments key at all (an older query) registers its lan_prefixes alone."""
        office = {"owner": _C2, "sdwan_gateway": {"name": _GW1}, "lan_prefixes": _blocks(("10.210.2.0/24", "PROD"))}
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[office],
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.2.0/24"]}}

    def test_no_offices_leaves_the_handoff_absent(self) -> None:
        """No office footprint at all: the gateway is not known, so the handoff accepts nothing."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[],
        )
        assert found == {}

    def test_handoff_facing_a_non_gateway_device_gets_no_office_lans(self) -> None:
        """Office LANs go only to the session whose far end is their gateway, not to any PROD handoff."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/14.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/14.1900", "EXT-PARTNER-01")),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.5.0/24"]}}

    def test_handoff_with_several_far_ends_is_not_resolved_to_a_gateway(self) -> None:
        """Ambiguous far end on one sub-interface: fail closed rather than trust one of them."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
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
                "eg-fr201",
                device_capabilities=capabilities,
                offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
            )


# ---------------------------------------------------------------------------
# Ownership: who registered the prefix
# ---------------------------------------------------------------------------


class TestCustomerOwner:
    def test_only_a_customer_counts(self) -> None:
        """org_id first, display_label when it is empty; a provider, no owner or a malformed one is no customer."""
        assert customer_owner(_C3) == "C003"
        assert customer_owner({"typename": "OrganizationCustomer", "org_id": None, "display_label": "Acme"}) == "Acme"
        assert customer_owner({"typename": "OrganizationCustomer", "org_id": "", "display_label": ""}) is None
        assert customer_owner(_PROVIDER) is None
        assert customer_owner(None) is None
        assert customer_owner("C003") is None

    @pytest.mark.parametrize(
        ("owner", "expected"),
        [
            ({"typename": "OrganizationCustomer", "display_label": "Acme"}, "Acme"),
            ({"typename": "OrganizationCustomer", "org_id": "C009"}, "C009"),
            ({"typename": "OrganizationCustomer"}, None),
            ({"typename": "OrganizationCustomer", "org_id": None, "display_label": None}, None),
            ({"org_id": "C003", "display_label": "C003 label"}, None),
            ({"typename": "OrganizationProvider", "org_id": "P027"}, None),
            ({"typename": "OrganizationInternal", "org_id": "I001"}, None),
            ({}, None),
            (["OrganizationCustomer"], None),
            (3, None),
        ],
        ids=[
            "label-when-org_id-key-missing",
            "org_id-only",
            "customer-with-no-key-at-all",
            "customer-with-null-keys",
            "no-typename",
            "provider",
            "internal",
            "empty-dict",
            "list",
            "int",
        ],
    )
    def test_owner_key_for_malformed_or_foreign_owners(self, owner: Any, expected: str | None) -> None:
        """Missing keys fall back (org_id, display_label, None); anything that is not a customer dict is None."""
        assert customer_owner(owner) == expected


class TestPeerPrefixOwners:
    """get_external_peer_prefixes attributes each registered prefix to the object that registered it."""

    def test_vpc_blocks_take_the_owner_of_their_own_vpcs_account(self) -> None:
        """Two VPCs of two customers behind one transit VIF: each owner keeps its own blocks."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"), owner=_C3),
                _vpc_attachment(["rt-shared"], ("10.32.0.0/16", "PROD"), ("10.31.0.0/16", "PROD"), owner=_C15),
                _vpc_attachment(["rt-isolated"], ("10.150.0.0/16", "PROD"), owner=_C3),
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.31.0.0/16", "10.32.0.0/16"]}}

    @pytest.mark.parametrize("owner", [_PROVIDER, None], ids=["provider", "no-owner"])
    def test_a_vpc_block_without_a_customer_owner_is_never_accepted(self, owner: dict[str, Any] | None) -> None:
        """A provider-owned or ownerless account registers a prefix nobody may announce to the far end."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(["rt-shared"], ("10.33.0.0/16", "PROD"), owner=owner),
                _vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD")),
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}

    def test_a_vpc_without_an_account_is_never_accepted(self) -> None:
        """A virtual network whose account (or its owner) is null registers nothing acceptable."""
        vif = _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))])
        vif["transit_gateway_attachment"]["transit_gateway"]["attachments"][0]["virtual_network"]["account"] = None
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {}}

    def test_private_vif_blocks_take_the_owner_of_its_vpc(self) -> None:
        """A private VIF reads vif.virtual_network.account.owner."""
        vif = {
            "name": "vif-private",
            "vlan_id": 1001,
            "direct_connect": {"physical_devices": [{"name": "eg-fr201"}]},
            "virtual_network": {"cidr_blocks": _blocks(("10.60.0.0/16", "NON-PROD")), "account": {"owner": _C15}},
        }
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1001", "NON-PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1001": {"C015": ["10.60.0.0/16"]}}

    def test_office_lans_take_the_office_owner_and_hosted_segments_their_own(self) -> None:
        """C002's LAN, C005's hosted office-lan segment: one sub-interface, two owners."""
        offices = [
            _office(_GW1, ("10.210.2.0/24", "PROD"), owner=_C2),
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),), owner=_C5, segment_owner=_C5),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.2.0/24"], "C005": ["10.176.5.0/24"]}}

    def test_a_hosted_segment_is_owned_by_the_segment_not_by_the_office(self) -> None:
        """An office of C002 hosting a segment of C005: the prefix is C005's."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[_office(_GW1, hosted=(("10.176.5.0/24", "PROD"),), owner=_C2, segment_owner=_C5)],
        )
        assert found == {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}}

    def test_prefixes_without_a_customer_owner_are_never_accepted(self) -> None:
        """A provider-owned office LAN, an ownerless office and a provider-owned hosted segment register nothing."""
        offices = [
            _office(_GW1, ("10.210.7.0/24", "PROD"), owner=_PROVIDER),
            _office(_GW1, ("10.210.8.0/24", "PROD"), owner=None),
            _office(_GW1, hosted=(("10.176.7.0/24", "PROD"),), owner=_C2, segment_owner=_PROVIDER),
            _office(_GW1, hosted=(("10.176.8.0/24", "PROD"),), owner=_C2, segment_owner=None),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {}}

    def test_a_retired_hosted_segment_is_skipped(self) -> None:
        """A retired segment's prefix is no longer served: not accepted, while the active sibling is."""
        offices = [
            _office(_GW1, hosted=(("10.176.6.0/24", "PROD"),), segment_status="retired"),
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24"]}}


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
            {"gateway": None, "owner": _C3, "status": "active"},
        ]
        assert get_onprem_prefixes(segments) == {
            "NON-PROD": {"C003": ["10.6.10.0/24"]},
            "PROD": {"C003": ["10.5.10.0/24", "10.5.20.0/24"]},
        }

    def test_groups_by_owner_with_sorted_keys_and_prefixes(self) -> None:
        """VRF -> owner -> prefixes in address order; an owner's NON-PROD space is not its PROD space."""
        segments = [
            _segment("10.7.10.0/24", _PROD, owner=_C15),
            _segment("10.5.20.0/24", _PROD, owner=_C3),
            _segment("10.8.2.0/24", _PROD, owner=_C2),
            _segment("10.5.9.0/24", _PROD, owner=_C3),
            _segment("10.6.10.0/24", _NON_PROD, owner=_C3),
        ]
        found = get_onprem_prefixes(segments)
        assert found == {
            "NON-PROD": {"C003": ["10.6.10.0/24"]},
            "PROD": {"C002": ["10.8.2.0/24"], "C003": ["10.5.9.0/24", "10.5.20.0/24"], "C015": ["10.7.10.0/24"]},
        }
        assert list(found["PROD"]) == ["C002", "C003", "C015"]

    def test_retired_segments_are_skipped(self) -> None:
        """Every other status still qualifies; retired does not, and an all-retired VRF is absent."""
        segments = [
            _segment("10.5.1.0/24", _PROD, status="active"),
            _segment("10.5.2.0/24", _PROD, status="provisioning"),
            _segment("10.5.3.0/24", _PROD, status="maintenance"),
            _segment("10.5.4.0/24", _PROD, status="deprecated"),
            _segment("10.5.5.0/24", _PROD, status="retired"),
            _segment("10.6.5.0/24", _NON_PROD, status="retired"),
        ]
        assert get_onprem_prefixes(segments) == {
            "PROD": {"C003": ["10.5.1.0/24", "10.5.2.0/24", "10.5.3.0/24", "10.5.4.0/24"]}
        }

    @pytest.mark.parametrize("owner", [None, _PROVIDER], ids=["no-owner", "provider"])
    def test_a_segment_without_a_customer_owner_is_never_announced(self, owner: dict[str, Any] | None) -> None:
        """Ownership is what registers a prefix: no customer owner, no announcement."""
        segments = [
            _segment("10.5.77.0/24", _PROD, owner=owner),
            {"gateway": _segment("10.5.78.0/24", _PROD)["gateway"]},
        ]
        assert get_onprem_prefixes(segments) == {}

    def test_no_segments_is_empty(self) -> None:
        assert get_onprem_prefixes(None) == {}


class TestExternalSessionPolicies:
    """get_external_session_policies: the owners of a session, what it accepts, what it is announced."""

    _ONPREM = {
        "PROD": {"C002": ["10.8.2.0/24"], "C003": ["10.5.10.0/24", "10.5.20.0/24"], "C015": ["10.7.10.0/24"]},
        "NON-PROD": {"C003": ["10.6.10.0/24"], "C015": ["10.6.20.0/24"]},
    }

    def test_out_is_the_union_of_the_sessions_owners_only(self) -> None:
        """C002 owns nothing behind this session, so its space is neither accepted nor announced."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1000", "PROD")],
            {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.31.0.0/16", "10.32.0.0/16"]}},
            self._ONPREM,
        )
        assert policies == {
            "Ethernet1/13.1000": {
                "owners": ["C003", "C015"],
                "in": ["10.30.0.0/16", "10.31.0.0/16", "10.32.0.0/16"],
                "out": ["10.5.10.0/24", "10.5.20.0/24", "10.7.10.0/24"],
            }
        }

    def test_out_is_limited_to_the_sub_interfaces_namespace(self) -> None:
        """A NON-PROD session never announces the owner's PROD space."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1001", "NON-PROD")], {"Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}, self._ONPREM
        )
        assert policies["Ethernet1/13.1001"]["out"] == ["10.6.10.0/24"]

    def test_a_session_with_no_owner_accepts_and_announces_nothing(self) -> None:
        """A known far end with nothing customer-owned registered: empty owners, in and out."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1900", "PROD")], {"Ethernet1/13.1900": {}}, self._ONPREM
        )
        assert policies == {"Ethernet1/13.1900": {"owners": [], "in": [], "out": []}}

    def test_an_owner_without_onprem_space_in_the_namespace_gets_an_empty_out(self) -> None:
        """The far end registers C005's prefix, C005 has no segment here: accepts it, announces nothing."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1900", "PROD")], {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}}, self._ONPREM
        )
        assert policies["Ethernet1/13.1900"] == {"owners": ["C005"], "in": ["10.176.5.0/24"], "out": []}

    def test_only_sessions_with_a_known_far_end_have_a_policy(self) -> None:
        """No far end resolved: absent (the template denies both ways)."""
        assert get_external_session_policies([_subif("Ethernet1/13.1300", "PROD")], {}, self._ONPREM) == {}

    def test_two_owners_sharing_a_prefix_list_it_once_and_the_lists_are_sorted(self) -> None:
        """Dedup across owners, address order (10.9 before 10.10) and length order, whatever the input order."""
        onprem = {"PROD": {"C003": ["10.10.0.0/24", "10.9.0.0/24"], "C015": ["10.9.0.0/24", "10.9.0.0/16"]}}
        peers = {"Ethernet1/13.1000": {"C015": ["10.40.0.0/16", "10.4.0.0/16"], "C003": ["10.4.0.0/16"]}}
        policies = get_external_session_policies([_subif("Ethernet1/13.1000", "PROD")], peers, onprem)
        assert policies["Ethernet1/13.1000"] == {
            "owners": ["C003", "C015"],
            "in": ["10.4.0.0/16", "10.40.0.0/16"],
            "out": ["10.9.0.0/16", "10.9.0.0/24", "10.10.0.0/24"],
        }

    def test_the_result_does_not_depend_on_the_input_order(self) -> None:
        """Same inputs in another order: the identical policy, so the rendered route-maps are byte-stable."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {
            "Ethernet1/13.1000": {"C015": ["10.31.0.0/16"], "C003": ["10.30.0.0/16"]},
            "Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]},
        }
        forward = get_external_session_policies(interfaces, peers, self._ONPREM)
        reverse = get_external_session_policies(interfaces[::-1], dict(reversed(list(peers.items()))), self._ONPREM)
        assert forward == reverse
        assert forward["Ethernet1/13.1000"]["owners"] == ["C003", "C015"]

    def test_each_session_reads_only_its_own_sub_interfaces_namespace(self) -> None:
        """Two sessions of one owner in two namespaces: PROD announces the PROD space, NON-PROD the NON-PROD one."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}, "Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert policies["Ethernet1/13.1000"]["out"] == ["10.5.10.0/24", "10.5.20.0/24"]
        assert policies["Ethernet1/13.1001"]["out"] == ["10.6.10.0/24"]

    def test_a_peer_entry_for_an_unknown_sub_interface_announces_nothing(self) -> None:
        """No such sub-interface (or no interfaces at all): no namespace, so no on-prem space is looked up."""
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}
        for interfaces in (None, [], [_subif("Ethernet1/13.9", "PROD")]):
            policies = get_external_session_policies(interfaces, peers, self._ONPREM)
            assert policies["Ethernet1/13.1000"] == {"owners": ["C003"], "in": ["10.30.0.0/16"], "out": []}

    def test_a_namespace_without_onprem_space_announces_nothing(self) -> None:
        """The sub-interface sits in a namespace no segment is in: out stays empty, in is untouched."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1000", "OTHER")], {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}, self._ONPREM
        )
        assert policies["Ethernet1/13.1000"]["out"] == []
        assert policies["Ethernet1/13.1000"]["in"] == ["10.30.0.0/16"]

    def test_no_peers_is_no_policies(self) -> None:
        """No external session on the device: nothing to build."""
        assert get_external_session_policies([_subif("Ethernet1/13.1000", "PROD")], {}, {}) == {}

    def test_a_sub_interface_without_a_namespace_announces_nothing(self) -> None:
        """No address namespace to look the owners' space up in."""
        policies = get_external_session_policies(
            [{"name": "Ethernet1/13.1000", "ip_address": None}],
            {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}},
            self._ONPREM,
        )
        assert policies["Ethernet1/13.1000"]["out"] == []

    def test_an_owner_on_two_sessions_of_a_namespace_is_passed_between_them(self) -> None:
        """C003's office LAN behind the gateway reaches the VIF; its VPC block reaches the gateway; C015 stays home."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1900", "PROD")]
        peers = {
            "Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.32.0.0/16"]},
            "Ethernet1/13.1900": {"C003": ["10.210.3.0/24"]},
        }
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert policies["Ethernet1/13.1900"] == {
            "owners": ["C003"],
            "in": ["10.210.3.0/24"],
            "out": ["10.5.10.0/24", "10.5.20.0/24", "10.30.0.0/16"],
        }
        assert policies["Ethernet1/13.1000"] == {
            "owners": ["C003", "C015"],
            "in": ["10.30.0.0/16", "10.32.0.0/16"],
            "out": ["10.5.10.0/24", "10.5.20.0/24", "10.7.10.0/24", "10.210.3.0/24"],
        }

    def test_sessions_in_different_namespaces_do_not_exchange(self) -> None:
        """The same owner on a PROD and a NON-PROD session: each is announced its own namespace's space only."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}, "Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert "10.60.0.0/16" not in policies["Ethernet1/13.1000"]["out"]
        assert "10.30.0.0/16" not in policies["Ethernet1/13.1001"]["out"]

    def test_a_learned_segment_prefix_is_not_announced_back_nor_local(self) -> None:
        """C005's hosted segment is both a segment and registered behind the gateway session."""
        interfaces = [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/13.1000", "PROD")]
        peers = {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}, "Ethernet1/13.1000": {"C005": ["10.50.0.0/16"]}}
        onprem = {"PROD": {"C005": ["10.176.5.0/24"]}}
        policies = get_external_session_policies(interfaces, peers, onprem)
        assert policies["Ethernet1/13.1900"]["out"] == ["10.50.0.0/16"]
        assert policies["Ethernet1/13.1000"]["out"] == ["10.176.5.0/24"]

    def test_sessions_without_a_namespace_never_exchange_with_each_other(self) -> None:
        """Two sub-interfaces without an address stand alone: C003 on both does not cross-announce."""
        interfaces = [
            {"name": "Ethernet1/13.1000", "ip_address": None},
            {"name": "Ethernet1/13.1900", "ip_address": None},
        ]
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}, "Ethernet1/13.1900": {"C003": ["10.210.3.0/24"]}}
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert policies["Ethernet1/13.1000"]["out"] == []
        assert policies["Ethernet1/13.1900"]["out"] == []

    def test_the_result_keeps_the_peer_order(self) -> None:
        """One entry per peer, in the order given."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {"Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}, "Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}
        assert list(get_external_session_policies(interfaces, peers, self._ONPREM)) == list(peers)

    def test_conflicts_are_logged_as_warnings_with_the_device(self, caplog: pytest.LogCaptureFixture) -> None:
        """A prefix of two owners and an owner's prefix behind two sessions: WARNING each, never an error."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1900", "PROD")]
        peers = {
            "Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.40.0.0/16"]},
            "Ethernet1/13.1900": {"C003": ["10.30.0.0/16"], "C002": ["10.40.0.0/16"]},
        }
        with caplog.at_level("WARNING", logger="transforms.helpers.bgp"):
            policies = get_external_session_policies(interfaces, peers, {}, device_name="eg-fr201")
        assert set(policies) == set(peers)
        messages = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert len(messages) == 2
        assert all(message.startswith("eg-fr201: namespace PROD: prefix ") for message in messages)
        assert any("registered by more than one owner" in message and "10.40.0.0/16" in message for message in messages)
        assert any("more than one session" in message and "10.30.0.0/16" in message for message in messages)
        assert not [record for record in caplog.records if record.levelname == "ERROR"]

    def test_clean_registrations_log_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """No conflict, no warning."""
        with caplog.at_level("WARNING", logger="transforms.helpers.bgp"):
            get_external_session_policies(
                [_subif("Ethernet1/13.1000", "PROD")], {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}, self._ONPREM
            )
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Internal vs external
# ---------------------------------------------------------------------------


def _with_far_end_role(peering: dict[str, Any], role: str | None) -> dict[str, Any]:
    """The eg-fr201 <-> EXT-AWS-DX-FR2 peering with the far end's BGP process in ``role``."""
    peering["bgp_processes"][1]["process_role"] = role
    return peering


class TestInternalVersusExternal:
    """A regular VRF session is external unless its far end is one of our devices."""

    @pytest.mark.parametrize("role", ["underlay", "regular", None])
    def test_a_far_end_process_that_is_not_overlay_stays_external(self, role: str | None) -> None:
        session = _build_session_from_peering(
            _with_far_end_role(_peering(_PROD), role), "eg-fr201", {"asn": 4200000100}, []
        )
        assert session is not None
        assert session["external"] is True

    def test_an_overlay_far_end_process_makes_the_session_internal(self) -> None:
        """eg-fr201 <-> eg-am-ne01: the far end runs one of our overlay processes."""
        peering = _with_far_end_role(_peering(_PROD), "overlay")
        session = _build_session_from_peering(peering, "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == "PROD"
        assert "external" not in session

    def test_internal_from_both_sides(self) -> None:
        """Seen from the far end, the same peering is internal too: its process in this peering is the overlay one."""
        peering = _with_far_end_role(_peering(_PROD), "overlay")
        peering["bgp_processes"][0]["process_role"] = "overlay"
        session = _build_session_from_peering(peering, "EXT-AWS-DX-FR2", {"asn": 64512}, [])
        assert session is not None
        assert "external" not in session

    def test_only_the_far_ends_process_counts(self) -> None:
        """An overlay process on OUR end does not make a foreign far end ours."""
        peering = _peering(_PROD)
        peering["bgp_processes"][0]["process_role"] = "overlay"
        session = _build_session_from_peering(peering, "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["external"] is True

    def test_a_far_end_in_own_devices_is_internal(self) -> None:
        """The office's own SD-WAN edge (deployment.lisp_wan_edges) is ours whatever its process says."""
        session = _build_session_from_peering(
            _peering(_PROD), "eg-fr201", {"asn": 4200000100}, [], own_devices=frozenset({"EXT-AWS-DX-FR2"})
        )
        assert session is not None
        assert "external" not in session

    def test_another_device_in_own_devices_does_not_matter(self) -> None:
        session = _build_session_from_peering(
            _peering(_PROD), "eg-fr201", {"asn": 4200000100}, [], own_devices=frozenset({"someone-else"})
        )
        assert session is not None
        assert session["external"] is True

    def test_an_internal_vrf_session_keeps_its_vrf_neighbour_fields(self) -> None:
        """Internal is not a different kind of VRF session: same vrf, addresses and interface."""
        internal = _build_session_from_peering(
            _with_far_end_role(_peering(_PROD), "overlay"), "eg-fr201", {"asn": 4200000100}, []
        )
        external = _build_session_from_peering(_peering(_PROD), "eg-fr201", {"asn": 4200000100}, [])
        assert internal is not None and external is not None
        assert {k: v for k, v in external.items() if k != "external"} == internal

    def test_the_offices_sdwan_edges_are_its_own_devices(self) -> None:
        """BaseDeviceTransform fills own_devices from deployment.lisp_wan_edges (cleaned: a list of names)."""
        deployment = {"lisp_wan_edges": [{"name": "C005-OFFICE-EDGE1"}, {"name": None}, "junk"]}
        assert _own_devices(deployment) == frozenset({"C005-OFFICE-EDGE1"})
        assert _own_devices({"lisp_wan_edges": None}) == frozenset()
        assert _own_devices(None) == frozenset()

    def test_internet_transit_is_never_internal_by_role(self) -> None:
        """The INTERNET namespace is transit regardless of the far end's role."""
        namespace = {"name": "INTERNET", "namespace_type": "internet"}
        session = _build_session_from_peering(
            _with_far_end_role(_peering(namespace), "overlay"), "eg-fr201", {"asn": 4200000100}, []
        )
        assert session is not None
        assert session["transit"] is True


class TestFarEndIsOurs:
    """_far_end_is_ours: an overlay process on the FAR end, or a name in own_devices; nothing else."""

    @staticmethod
    def _node(*processes: Any) -> dict[str, Any]:
        """A peering node whose bgp_processes are exactly ``processes``."""
        return {"bgp_processes": list(processes)}

    @staticmethod
    def _process(role: str | None, *devices: str) -> dict[str, Any]:
        """A BGP process of ``role`` running on ``devices``."""
        return {"process_role": role, "capabilities": [{"name": device} for device in devices]}

    def test_an_overlay_process_on_the_far_end_makes_it_ours(self) -> None:
        """The far end runs one of our overlay processes in this peering."""
        node = self._node(self._process("overlay", "eg-fr201"), self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True

    def test_an_overlay_process_on_the_local_device_only_does_not_make_the_far_end_ours(self) -> None:
        """The local process is in bgp_processes too: only the process running ON the far end counts."""
        node = self._node(self._process("overlay", "eg-fr201"), self._process("regular", "EXT-AWS-DX-FR2"))
        assert _far_end_is_ours(node, "EXT-AWS-DX-FR2", frozenset()) is False

    @pytest.mark.parametrize("role", ["underlay", "regular", None, "", "Overlay"])
    def test_a_far_end_process_in_another_role_is_not_ours(self, role: str | None) -> None:
        """Only the exact role overlay counts: underlay, regular, null, empty or another case stay foreign."""
        node = self._node(self._process("overlay", "eg-fr201"), self._process(role, "EXT-AWS-DX-FR2"))
        assert _far_end_is_ours(node, "EXT-AWS-DX-FR2", frozenset()) is False

    @pytest.mark.parametrize("remote", ["", None])
    def test_a_missing_far_end_name_is_never_ours(self, remote: str | None) -> None:
        """No name to match: not even an overlay process, or an own_devices entry for the empty name, helps."""
        node = self._node(self._process("overlay", "eg-fr201", ""))
        assert _far_end_is_ours(node, remote or "", frozenset({""})) is False

    @pytest.mark.parametrize(
        "node",
        [
            {},
            {"bgp_processes": None},
            {"bgp_processes": []},
            {"bgp_processes": ["junk", None, 3]},
            {"bgp_processes": [{"process_role": "overlay"}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": None}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": []}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": ["EXT-AWS-DX-FR2", None]}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": [{"device": "EXT-AWS-DX-FR2"}]}]},
        ],
        ids=[
            "no-key",
            "none",
            "empty",
            "non-dict-processes",
            "no-capabilities-key",
            "null-capabilities",
            "empty-capabilities",
            "non-dict-capabilities",
            "capability-without-name",
        ],
    )
    def test_missing_or_malformed_process_data_leaves_the_far_end_foreign(self, node: dict[str, Any]) -> None:
        """No processes, no capabilities or junk entries: external, never an exception."""
        assert _far_end_is_ours(node, "EXT-AWS-DX-FR2", frozenset()) is False

    def test_a_far_end_in_own_devices_is_ours_without_any_process(self) -> None:
        """The office's own SD-WAN edge carries no overlay process, its name is the signal."""
        assert _far_end_is_ours({}, "C005-OFFICE-EDGE1", frozenset({"C005-OFFICE-EDGE1"})) is True

    def test_own_devices_wins_over_a_foreign_role(self) -> None:
        """A far-end process in role regular does not undo an own_devices hit."""
        node = self._node(self._process("regular", "C005-OFFICE-EDGE1"))
        assert _far_end_is_ours(node, "C005-OFFICE-EDGE1", frozenset({"C005-OFFICE-EDGE1"})) is True

    def test_another_name_in_own_devices_is_not_a_hit(self) -> None:
        """Exact, case-sensitive name match."""
        assert _far_end_is_ours({}, "EXT-AWS-DX-FR2", frozenset({"EXT-AWS-DX-FR2-2", "ext-aws-dx-fr2"})) is False

    def test_one_overlay_process_among_several_is_enough(self) -> None:
        """The far end may run several processes in the peering: any overlay one qualifies."""
        node = self._node(
            self._process("regular", "other"),
            self._process("overlay", "x", "eg-am-ne01"),
            self._process("underlay", "y"),
        )
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True

    @staticmethod
    def _node_with_far_end_kind(kind: str | None, *processes: Any) -> dict[str, Any]:
        """A peering node whose second end is eg-am-ne01 on a device of ``kind`` (None: no typename selected)."""
        device: dict[str, Any] = {"name": "eg-am-ne01"} if kind is None else {"name": "eg-am-ne01", "typename": kind}
        return {
            "capabilities": [
                {"name": "Ethernet1/12.1200", "device": {"name": "eg-fr201"}},
                {"name": "Ethernet1/12.1200", "device": device},
            ],
            "bgp_processes": list(processes),
        }

    def test_an_overlay_process_on_a_network_appliance_does_not_make_it_ours(self) -> None:
        """A provider-hosted Network Edge is never ours via its process role."""
        node = self._node_with_far_end_kind("CloudNetworkAppliance", self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is False

    @pytest.mark.parametrize("kind", ["DcimPhysicalDevice", None, "", "DcimVirtualDevice"])
    def test_an_overlay_process_on_any_other_device_kind_still_makes_it_ours(self, kind: str | None) -> None:
        """Only CloudNetworkAppliance is excluded; a missing typename (older fixtures) is not an appliance."""
        node = self._node_with_far_end_kind(kind, self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True

    def test_own_devices_wins_over_a_network_appliance(self) -> None:
        """The office's own SD-WAN edge stays ours whatever its device kind."""
        node = self._node_with_far_end_kind("CloudNetworkAppliance", self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset({"eg-am-ne01"})) is True

    def test_only_the_far_ends_device_kind_counts(self) -> None:
        """The LOCAL device being an appliance (first end) does not stop the far end being ours."""
        node = self._node_with_far_end_kind("DcimPhysicalDevice", self._process("overlay", "eg-am-ne01"))
        node["capabilities"][0]["device"]["typename"] = "CloudNetworkAppliance"
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True


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
        """C005's hosted segment, C002's and C003's LANs: not the retired segment, the provider's LAN, another gateway's."""
        output = _run_edge(_fixture())
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-IN") == [
            "10.176.5.0/24",
            "10.210.2.0/24",
            "10.210.3.0/24",
        ]
        assert "route-map RM-VRF-PROD-EQXFR2-SDWAN-IN permit 10\n" in output
        for excluded in ("10.211.2.0/24", "10.210.9.0/24", "10.210.15.0/24", "10.210.7.0/24", "10.176.6.0/24"):
            assert excluded not in output
        block = _neighbor_block(output, "10.255.5.1")
        assert "        route-map RM-VRF-PROD-EQXFR2-SDWAN-IN in\n" in block
        assert "        maximum-prefix 100\n" in block
        assert "send-community" not in block

    def test_the_policy_names_its_owners(self) -> None:
        output = _run_edge(_fixture())
        assert "! VRF-PROD-EQXFR2-SDWAN: owners C002, C003, C005\n" in output
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
            "10.30.0.0/16",
        ]
        assert _prefix_list(output, "PL-VRF-NONPROD-EQXFR2-AWS-EUC1-OUT") == ["10.6.10.0/24"]
        assert "route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT deny 20" in output
        assert "        route-map RM-VRF-NONPROD-EQXFR2-AWS-EUC1-OUT out\n" in _neighbor_block(output, "10.255.6.6")
        assert "le " not in output and "ge " not in output

    def test_unregistered_retired_and_ownerless_space_is_never_announced(self) -> None:
        """C001 is behind no session; a retired, an ownerless and a NON-PROD segment of other owners stay home."""
        output = _run_edge(_fixture())
        out_lines = "\n".join(line for line in output.splitlines() if "-OUT seq" in line)
        for never in ("10.9.1.0/24", "10.5.99.0/24", "10.5.77.0/24", "10.6.20.0/24", "10.6.30.0/24", "10.250.0.0/24"):
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


# ---------------------------------------------------------------------------
# 30_all data
# ---------------------------------------------------------------------------


def _external_handoffs() -> list[dict[str, Any]]:
    """30_all Handoff circuits in a tenant namespace (PROD / NON-PROD).

    The session of a handoff is generated by add_virtual_circuit (peering_role regular, both ends addressed from the
    namespace's own /31 pool), so the circuit is what the data holds; ``namespaces`` is the namespace of each end.
    """
    return [
        {**circuit, "namespaces": [circuit["ip_namespace"]] * len(circuit["capabilities"])}
        for circuit in _objects(_INTERCONNECTS, "TopologyHandoffCircuit")
        if circuit["ip_namespace"] in ("PROD", "NON-PROD")
    ]


class TestAllDemoData:
    def test_the_cloud_vif_and_sdwan_handoffs_are_external_peers(self) -> None:
        """Every tenant-VRF eBGP handoff of 30_all is in the external mode's scope, all in PROD."""
        assert sorted(p["name"] for p in _external_handoffs()) == [
            "HANDOFF-AMNE-AWS-EUC1-PROD",
            "HANDOFF-FR2-AMNE-PROD",
            "HANDOFF-FR2-AWS-EUC1-PROD",
            "HANDOFF-FR2-SDWAN-GW1-PROD",
            "HANDOFF-PA4-MEGAPORT-PROD",
        ]
        assert {p["ip_namespace"] for p in _external_handoffs()} == {"PROD"}

    @pytest.mark.parametrize("peering", _external_handoffs(), ids=lambda p: p["name"])
    def test_handoff_has_max_prefix_and_one_namespace(self, peering: dict[str, Any]) -> None:
        """max-prefix is set, and both ends sit in one namespace: a handoff never mixes environments."""
        assert isinstance(peering.get("maximum_routes"), int) and peering["maximum_routes"] > 0
        assert len(set(peering["namespaces"])) == 1, peering["namespaces"]

    @pytest.mark.parametrize(
        ("vif_name", "handoff_name"),
        [
            ("vif-transit-hub-euc1", "HANDOFF-FR2-AWS-EUC1-PROD"),
            ("vif-transit-am-euc1", "HANDOFF-AMNE-AWS-EUC1-PROD"),
        ],
    )
    def test_vif_handoff_is_port_dot_vlan_on_the_direct_connect_device(self, vif_name: str, handoff_name: str) -> None:
        """The session's sub-interface is <port>.<vif.vlan_id> on the DX's physical device, in PROD; the VIF names it."""
        (vif,) = [v for v in _objects(_INTERCONNECTS, "CloudVirtualInterface") if v["name"] == vif_name]
        (dx,) = [d for d in _objects(_INTERCONNECTS, "CloudDirectConnect") if d["name"] == vif["direct_connect"]]
        (peering,) = [p for p in _external_handoffs() if p["name"] == handoff_name]
        assert vif["virtual_circuit"] == handoff_name
        (device, interface) = next(end for end in peering["capabilities"] if end[0] in dx["physical_devices"])
        port, _, vlan = interface.rpartition(".")
        assert int(vlan) == vif["vlan_id"]
        assert peering["namespaces"] == ["PROD", "PROD"]
        # the dedicated port of the FR2 cage is the cross-connect's own; the hosted connection of the NE router is not
        crossconnects = [
            c for c in _objects(_INTERCONNECTS, "TopologyPhysicalCircuit") if c["name"] == "XC-EQXFR2-AWS-EUC1"
        ]
        assert ([device, port] in crossconnects[0]["capabilities"]) == (dx["connection_type"] == "dedicated")

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
        (peering,) = [p for p in _external_handoffs() if p["name"] == "HANDOFF-FR2-SDWAN-GW1-PROD"]
        (gateway,) = [device for device, _ in peering["capabilities"] if not device.startswith("eg-")]
        behind: list[list[list[str]]] = []
        for office in _objects(_ROOT / "data" / "demos" / "30_all" / "06_customer_boarding", "TopologyCustomerOffice"):
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
                            "owner": {
                                "node": {
                                    "__typename": "OrganizationCustomer",
                                    "org_id": {"value": "C005"},
                                    "display_label": "C005 label",
                                }
                            },
                            "sdwan_gateway": {"node": {"name": {"value": _GW1}}},
                            "lan_prefixes": {"edges": []},
                            "segment_deployments": {
                                "edges": [
                                    {
                                        "node": {
                                            "segment": {
                                                "node": {
                                                    "status": {"value": "active"},
                                                    "owner": {
                                                        "node": {
                                                            "__typename": "OrganizationCustomer",
                                                            "org_id": {"value": "C005"},
                                                            "display_label": "C005 label",
                                                        }
                                                    },
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
                                                    },
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
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
        )

        assert found == {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}}
