"""Routing identity in the rendered configs: origination on the leaf, the owner community on an external session.

Inputs are the raw GraphQL responses of tests/smoke/fixture_builders (before ``clean_data``), run through the real
transforms and templates. A tag renders a community (a route-map sequence per tagged segment on NX-OS, EOS and
SONiC/FRR; per owner on the NX-OS edge's external sessions); a model without tags renders as before. The community is
a tag for visibility: the prefix-lists stay the gate, so every sequence still matches a model-derived prefix-list.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.smoke.fixture_builders.common import _edges, _v
from tests.smoke.fixture_builders.edge import build_edge_external_vrf_peers_data
from tests.smoke.fixture_builders.fabric import build_device_data
from tests.smoke.fixture_builders.routing_identity import CUSTOMER_TAGS, build_edge_external_vrf_peers_tagged_data
from tests.smoke.test_configs_smoke import _run_transform
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.leaf import Leaf
from transforms.helpers.bgp_external import get_external_session_policies
from transforms.helpers.templates import load_template
from utils.routing_identity import customer_community, routing_community_regexes

_TEMPLATES = str(Path(__file__).parents[2] / "templates" / "configs")


def _leaf(platform: str, *, tagged: bool = True, role: str = "leaf", **kwargs: Any) -> str:
    """The rendered leaf (or border leaf) of the fixture device, tagged or not."""
    data = build_device_data(
        device_name="dc1-leaf-01",
        role=role,
        platform=platform,
        scenario="ebgp_ibgp",
        include_segments=True,
        vlan_segment_vrf=True,
        routing_identity=tagged,
        **kwargs,
    )
    return _run_transform(BorderLeaf if role == "border-leaf" else Leaf, data)


def _lines(config: str, start: str) -> list[str]:
    """The lines of ``config`` beginning with ``start``."""
    return [line for line in config.splitlines() if line.startswith(start)]


# ---------------------------------------------------------------------------
# Origination on the leaf
# ---------------------------------------------------------------------------


class TestNxosOrigination:
    """RM-VRF-DIRECT-2-EVPN: a sequence per tagged segment, then the catch-all."""

    def test_each_tagged_segment_gets_a_sequence_with_its_communities(self) -> None:
        """Customer then segment community, additive; the match is the segment's own prefix-list."""
        config = _leaf("cisco_nxos")
        assert "ip prefix-list PL-ORIG-seg-100 seq 10 permit 10.100.0.0/24" in config
        assert (
            "route-map RM-VRF-DIRECT-2-EVPN permit 10\n"
            "  match ip address prefix-list PL-ORIG-seg-100\n"
            "  set community 64496:5 64497:12 additive\n"
            "route-map RM-VRF-DIRECT-2-EVPN permit 20\n"
            "  match ip address prefix-list PL-ORIG-seg-200\n"
            "  set community 64497:13 additive\n"
        ) in config

    def test_the_catch_all_follows_the_tagged_sequences_and_nothing_follows_it(self) -> None:
        """An untagged connected route still leaves the VRF: the unqualified permit has the highest sequence."""
        sequences = [
            int(m) for m in re.findall(r"^route-map RM-VRF-DIRECT-2-EVPN permit (\d+)$", _leaf("cisco_nxos"), re.M)
        ]
        assert sequences == [10, 20, 30]
        catch_all = re.search(r"^route-map RM-VRF-DIRECT-2-EVPN permit 30\n(.*)", _leaf("cisco_nxos"), re.M)
        assert catch_all is not None and catch_all.group(1) == "!"

    def test_prefix_lists_and_route_maps_are_defined_before_router_bgp(self) -> None:
        """NX-OS pushes a whole config: what `router bgp` names must precede it."""
        config = _leaf("cisco_nxos")
        assert config.index("PL-ORIG-seg-100") < config.index("router bgp")
        assert config.index("route-map RM-VRF-DIRECT-2-EVPN permit 10") < config.index("router bgp")

    def test_untagged_segments_render_the_single_catch_all_as_before(self) -> None:
        """No tag: no prefix-list, no community, and the catch-all keeps sequence 10."""
        config = _leaf("cisco_nxos", tagged=False)
        assert "PL-ORIG" not in config and "community" not in config.replace("send-community", "")
        assert "route-map RM-VRF-DIRECT-2-EVPN permit 10\n" in config

    def test_the_border_leaf_originates_the_same_way(self) -> None:
        """The route-map partial is shared by every NX-OS role that redistributes direct routes."""
        assert "set community 64496:5 64497:12 additive" in _leaf("cisco_nxos", role="border-leaf")

    def test_a_transit_deny_keeps_its_place_ahead_of_the_tagged_sequences(self) -> None:
        """deny 5 (the leaf's exchange transit /29s) matches first, then the tagged permits, then the catch-all."""
        template = load_template(_TEMPLATES, "common/cisco_nxos_route_maps.j2")
        entry = {"name": "s", "prefix": "10.1.0.0/24", "communities": ["64496:1"]}
        rendered = template.render(
            loopback_name="loopback0",
            vxlan={"l3_vni_mappings": [{"vrf_name": "A"}]},
            leaf_transit_prefixes=["100.66.0.0/29"],
            origination=[entry],
        )
        sequences = re.findall(r"^route-map RM-VRF-DIRECT-2-EVPN (deny|permit) (\d+)$", rendered, re.M)
        assert sequences == [("deny", "5"), ("permit", "10"), ("permit", "20")]


class TestEosOrigination:
    """RM-VRF-CONN-2-EVPN, applied by `redistribute connected` in the tenant VRF of `router bgp`."""

    @pytest.mark.parametrize("role", ["leaf", "border-leaf"])
    def test_the_route_map_sets_the_communities_and_the_vrf_applies_it(self, role: str) -> None:
        """Leaf and border leaf share the partial; the redistribution names the map only when it has work."""
        config = _leaf("arista_eos", role=role)
        assert (
            "route-map RM-VRF-CONN-2-EVPN permit 10\n"
            "   match ip address prefix-list PL-ORIG-seg-100\n"
            "   set community 64496:5 64497:12 additive\n"
        ) in config
        assert "      redistribute connected route-map RM-VRF-CONN-2-EVPN\n" in config
        assert config.index("RM-VRF-CONN-2-EVPN permit 10") < config.index(
            "redistribute connected route-map RM-VRF-CONN-2-EVPN"
        )

    @pytest.mark.parametrize("role", ["leaf", "border-leaf"])
    def test_untagged_renders_a_plain_redistribution(self, role: str) -> None:
        """No tag and no transit leg: no route-map at all."""
        config = _leaf("arista_eos", tagged=False, role=role)
        assert "RM-VRF-CONN-2-EVPN" not in config and "PL-ORIG" not in config
        assert "      redistribute connected\n" in config

    def test_the_partial_orders_transit_deny_origination_catch_all(self) -> None:
        """deny 5 first, the tagged permits at 10, 20, ..., the unqualified permit last (here 30)."""
        template = load_template(_TEMPLATES, "common/arista_eos_vrf_conn_route_map.j2")
        entry = {"name": "s", "prefix": "10.1.0.0/24", "communities": ["64496:1"]}
        rendered = template.render(vrf_conn_transit=["100.66.0.0/29"], origination=[entry, {**entry, "name": "t"}])
        sequences = re.findall(r"^route-map RM-VRF-CONN-2-EVPN (deny|permit) (\d+)$", rendered, re.M)
        assert sequences == [("deny", "5"), ("permit", "10"), ("permit", "20"), ("permit", "30")]

    def test_the_partial_with_a_transit_only_is_the_old_stanza(self) -> None:
        """No tag: deny 5 then the catch-all `permit 10`, byte for byte what the leaf rendered inline."""
        template = load_template(_TEMPLATES, "common/arista_eos_vrf_conn_route_map.j2")
        rendered = template.render(vrf_conn_transit=["100.66.0.0/29"], origination=[])
        assert rendered == (
            "\n!\n! The firewall transit /29s this leaf holds locally for PBR are originated by the borders only\n"
            "! documented-not-lab-verified: a deny entry keeps the leaf's connected transit /29 out of the EVPN type-5 table\n"
            "ip prefix-list PL-XCHG-TRANSIT seq 10 permit 100.66.0.0/29\n"
            "route-map RM-VRF-CONN-2-EVPN deny 5\n"
            "   match ip address prefix-list PL-XCHG-TRANSIT\n"
            "route-map RM-VRF-CONN-2-EVPN permit 10"
        )

    def test_the_partial_renders_nothing_without_transit_or_tags(self) -> None:
        """Nothing to do: nothing rendered, so a border without tags is unchanged."""
        template = load_template(_TEMPLATES, "common/arista_eos_vrf_conn_route_map.j2")
        assert template.render(origination=[]) == ""


class TestSonicOrigination:
    """FRR through the config DB: ROUTE_MAP / PREFIX_SET / PREFIX entries, applied by the VRF's redistribute."""

    def test_the_route_map_matches_a_prefix_set_per_segment_and_sets_the_communities(self) -> None:
        """One ROUTE_MAP key per sequence; the catch-all permit comes after the tagged ones."""
        db = json.loads(_leaf("sonic"))
        assert db["ROUTE_MAP"] == {
            "RM-VRF-CONN-2-EVPN|10": {
                "route_operation": "permit",
                "match_prefix_set": "PL-ORIG-seg-100",
                "set_community_inline": ["64496:5", "64497:12", "additive"],
            },
            "RM-VRF-CONN-2-EVPN|20": {
                "route_operation": "permit",
                "match_prefix_set": "PL-ORIG-seg-200",
                "set_community_inline": ["64497:13", "additive"],
            },
            "RM-VRF-CONN-2-EVPN|30": {"route_operation": "permit"},
        }
        assert db["PREFIX_SET"] == {"PL-ORIG-seg-100": {"mode": "IPv4"}, "PL-ORIG-seg-200": {"mode": "IPv4"}}
        assert set(db["PREFIX"]) == {
            "PL-ORIG-seg-100|10|10.100.0.0/24|exact",
            "PL-ORIG-seg-200|10|10.200.0.0/24|exact",
        }

    def test_the_vrf_redistributes_connected_through_the_route_map(self) -> None:
        """Without the reference the route map would be defined and never applied."""
        af = json.loads(_leaf("sonic"))["BGP_GLOBALS_AF"]["VRF_A|ipv4_unicast"]
        assert af["route_map_redistribute_connected"] == "RM-VRF-CONN-2-EVPN"

    def test_untagged_renders_no_route_map_tables(self) -> None:
        """No tag, no leaf transit: no ROUTE_MAP / PREFIX_SET / PREFIX and a bare redistribute."""
        db = json.loads(_leaf("sonic", tagged=False))
        assert not {"ROUTE_MAP", "PREFIX_SET", "PREFIX"} & set(db)
        assert "route_map_redistribute_connected" not in db["BGP_GLOBALS_AF"]["VRF_A|ipv4_unicast"]


# ---------------------------------------------------------------------------
# The edge's external sessions
# ---------------------------------------------------------------------------


def _edge(data: dict[str, Any]) -> str:
    """The rendered NX-OS edge."""
    return _run_transform(Edge, data)


class TestEdgeExternalSessions:
    """RM-<session>-IN sets the owner's community per prefix, RM-<session>-OUT strips ours."""

    _SDWAN = "VRF-PROD-EQXFR2-SDWAN"
    _AWS = "VRF-PROD-EQXFR2-AWS-EUC1"

    @pytest.fixture(scope="class")
    @staticmethod
    def tagged() -> str:
        """The external-peers edge with the customers' routing tags."""
        return _edge(build_edge_external_vrf_peers_tagged_data(device_name="dc1-edge-01", platform="cisco_nxos"))

    @pytest.fixture(scope="class")
    @staticmethod
    def untagged() -> str:
        """The same edge, no tag anywhere."""
        return _edge(build_edge_external_vrf_peers_data(device_name="dc1-edge-01", platform="cisco_nxos"))

    def test_a_session_with_several_owners_gets_one_sequence_per_owner(self, tagged: str) -> None:
        """C002, C003 and C005 each match their own accepted prefixes and add their own community."""
        for sequence, owner in ((20, "C002"), (30, "C003"), (40, "C005")):
            assert (
                f"route-map RM-{self._SDWAN}-IN permit {sequence}\n"
                f"  match ip address prefix-list PL-{self._SDWAN}-IN-{owner}\n"
                "  set comm-list CL-ROUTING-IDENTITY delete\n"
                f"  set community {customer_community(CUSTOMER_TAGS[owner] or 0)} additive\n"
            ) in tagged

    def test_an_owner_without_a_tag_still_has_our_communities_deleted_and_gets_none(self, tagged: str) -> None:
        """C001 (no tag) is accepted on its prefix; whatever identity the far end set is removed, none is added."""
        assert (
            f"route-map RM-{self._SDWAN}-IN permit 10\n"
            f"  match ip address prefix-list PL-{self._SDWAN}-IN-C001\n"
            "  set comm-list CL-ROUTING-IDENTITY delete\n"
            f"ip prefix-list PL-{self._SDWAN}-IN-C002"
        ) in tagged

    def test_every_accepted_prefix_stays_in_a_model_derived_prefix_list_of_its_owner(self, tagged: str) -> None:
        """The community never widens the filter: the prefixes accepted are those of the untagged edge."""
        before = build_edge_external_vrf_peers_data(device_name="dc1-edge-01", platform="cisco_nxos")
        untagged_in = set(re.findall(r"IN seq \d+ permit (\S+)", _edge(before)))
        tagged_in = set(re.findall(r"-IN-\w+ seq \d+ permit (\S+)", tagged))
        assert tagged_in == untagged_in

    def test_the_outbound_map_strips_our_communities_after_the_prefix_match(self, tagged: str) -> None:
        """Internal tags never leave: the OUT permit deletes the list, the prefix-list match is unchanged."""
        for session in (self._SDWAN, self._AWS):
            assert (
                f"route-map RM-{session}-OUT permit 10\n"
                f"  match ip address prefix-list PL-{session}-OUT\n"
                "  set comm-list CL-ROUTING-IDENTITY delete\n"
                f"route-map RM-{session}-OUT deny 20\n"
            ) in tagged

    def test_the_community_list_selects_exactly_our_two_ranges_and_precedes_its_references(self, tagged: str) -> None:
        """One expanded entry per ASN half, defined before any route-map that deletes through it."""
        entries = _lines(tagged, "ip community-list expanded CL-ROUTING-IDENTITY permit ")
        assert [entry.rsplit(" ", 1)[1] for entry in entries] == routing_community_regexes()
        assert tagged.index("ip community-list expanded") < tagged.index("set comm-list")

    def test_a_session_without_an_owner_stays_deny_all_without_a_community(self, tagged: str) -> None:
        """No registered owner: both maps deny, nothing to tag or strip."""
        assert (
            "route-map RM-VRF-PROD-EQXFR2-MEGAPORT-IN deny 10\nroute-map RM-VRF-PROD-EQXFR2-MEGAPORT-OUT deny 10"
            in tagged
        )

    def test_without_any_tag_the_edge_renders_as_before(self, untagged: str) -> None:
        """No community-list, no comm-list delete, one IN prefix-list per session."""
        assert "community" not in untagged.replace("send-community", "")
        assert f"ip prefix-list PL-{self._SDWAN}-IN seq 10 permit" in untagged

    def test_a_tagged_segment_alone_still_strips_and_adds_no_customer_community(self) -> None:
        """Identity in use (a segment tag) with no customer tag: our ranges are deleted, none is added."""
        data = build_edge_external_vrf_peers_data(device_name="dc1-edge-01", platform="cisco_nxos")
        data["ManagedNetworkSegment"]["edges"][0]["node"]["routing_tag"] = _v(7)
        data["OrganizationCustomer"] = _edges([])
        config = _edge(data)
        assert "set comm-list CL-ROUTING-IDENTITY delete" in config
        assert "set community" not in config

    def test_the_edge_not_on_nxos_still_refuses_an_external_session(self) -> None:
        """Platforms that cannot render the owner maps keep refusing, tags or not."""
        data = build_edge_external_vrf_peers_tagged_data(device_name="dc1-edge-01", platform="cisco_ios")
        with pytest.raises(ValueError, match="cannot render the owner prefix filters"):
            _edge(data)


class TestExternalSessionPolicyData:
    """get_external_session_policies: the per-owner inbound entries."""

    _PEERS = {
        "Ethernet1/13.1900": {"C003": ["10.210.3.0/24", "10.30.0.0/16"], "C001": ["198.18.0.0/24"], "C002": []},
    }
    _IFACES = [{"name": "Ethernet1/13.1900", "ip_address": {"ip_namespace": {"name": "PROD"}}}]

    def _policies(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return get_external_session_policies(copy.deepcopy(self._IFACES), copy.deepcopy(self._PEERS), {}, **kwargs)

    def test_without_the_flag_the_policy_is_exactly_the_three_fields(self) -> None:
        """No identity keys unless the model has a tag."""
        policy = self._policies(customer_tags={"C003": 3})["Ethernet1/13.1900"]
        assert set(policy) == {"owners", "in", "out"}

    def test_each_owner_with_a_prefix_has_an_entry_with_its_community(self) -> None:
        """Sorted by owner; an owner with no prefix has no entry; an owner without a tag has community None."""
        policy = self._policies(customer_tags={"C003": 3}, routing_identity=True)["Ethernet1/13.1900"]
        assert policy["identity"] is True
        assert policy["in_owners"] == [
            {"owner": "C001", "name": "C001", "prefixes": ["198.18.0.0/24"], "community": None},
            {"owner": "C003", "name": "C003", "prefixes": ["10.30.0.0/16", "10.210.3.0/24"], "community": "64496:3"},
        ]

    def test_the_union_in_is_unchanged_by_the_identity(self) -> None:
        """The inbound filter is the same set of prefixes either way."""
        plain = self._policies()["Ethernet1/13.1900"]
        tagged = self._policies(customer_tags={"C003": 3}, routing_identity=True)["Ethernet1/13.1900"]
        assert tagged["in"] == plain["in"] and tagged["out"] == plain["out"]

    def test_an_owner_name_is_made_safe_for_a_prefix_list_name(self) -> None:
        """A customer keyed by display_label may contain spaces."""
        peers = {"Ethernet1/13.1900": {"Acme Corp": ["10.1.0.0/24"]}}
        policy = get_external_session_policies(
            copy.deepcopy(self._IFACES), peers, {}, customer_tags={"Acme Corp": 9}, routing_identity=True
        )["Ethernet1/13.1900"]
        assert policy["in_owners"][0]["name"] == "Acme-Corp"
