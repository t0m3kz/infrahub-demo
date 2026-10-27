"""Routing identity: the tag readers, the origination entries of a VTEP and the query fields that carry the tags.

utils/routing_identity.py derives the two standard communities (``64496:<customer tag>``, ``64497:<segment tag>``) from
the ``routing_tag`` of a customer and of a segment; transforms/helpers/origination.py turns the routed, tagged segments
of a device into the entries its templates render. The community is a tag for visibility, never a filter. Fixtures of
code that reads a query payload are built from the raw GraphQL shape through ``clean_data``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from graphql import FieldNode, FragmentDefinitionNode, OperationDefinitionNode, parse

from tests.integration import demo_data
from transforms.helpers.origination import get_origination
from utils.data_cleaning import clean_data
from utils.routing_identity import (
    customer_community,
    customer_tags,
    identity_in_use,
    list_name_part,
    routing_communities,
    routing_community_regexes,
    routing_tag,
    segment_community,
)

_QUERIES = Path(__file__).parents[2] / "queries"


# ---------------------------------------------------------------------------
# utils/routing_identity.py
# ---------------------------------------------------------------------------


class TestReaders:
    """The tag readers of a cleaned customer or segment."""

    @pytest.mark.parametrize(
        ("node", "expected"), [({"routing_tag": 12}, 12), ({"routing_tag": None}, None), ({}, None)]
    )
    def test_routing_tag_of_a_cleaned_node(self, node: dict[str, Any], expected: int | None) -> None:
        """The tag is the cleaned ``routing_tag`` attribute; a node without one has none."""
        assert routing_tag(node) == expected

    @pytest.mark.parametrize("node", [None, "C005", ["routing_tag"], 7])
    def test_routing_tag_of_anything_that_is_no_node_is_none(self, node: Any) -> None:
        """A relationship that cleans to None or to a bare id has no tag."""
        assert routing_tag(node) is None

    def test_a_tag_of_zero_is_a_tag_that_the_community_refuses(self) -> None:
        """0 is no 'missing' marker: it is read as given and rejected when the community is derived."""
        assert routing_tag({"routing_tag": 0}) == 0
        with pytest.raises(ValueError, match="outside 1..65534"):
            routing_communities(0, None)

    def test_communities_are_customer_first_and_only_the_tags_that_exist(self) -> None:
        """Both tags give the two communities in order; one tag gives one; none gives nothing."""
        assert routing_communities(5, 12) == ["64496:5", "64497:12"]
        assert routing_communities(None, 12) == ["64497:12"]
        assert routing_communities(5, None) == ["64496:5"]
        assert routing_communities(None, None) == []

    def test_customer_tags_key_a_customer_like_customer_owner(self) -> None:
        """org_id, else display_label, is the key; a customer without a tag is absent."""
        raw = {
            "OrganizationCustomer": {
                "edges": [
                    {"node": {"org_id": {"value": "C005"}, "display_label": "C005 x", "routing_tag": {"value": 5}}},
                    {"node": {"org_id": {"value": "C001"}, "display_label": "C001 x", "routing_tag": {"value": None}}},
                    {"node": {"org_id": {"value": None}, "display_label": "Acme", "routing_tag": {"value": 9}}},
                ]
            }
        }
        assert customer_tags(clean_data(raw)["OrganizationCustomer"]) == {"C005": 5, "Acme": 9}
        assert customer_tags(None) == {}

    def test_identity_is_in_use_when_any_customer_or_segment_is_tagged(self) -> None:
        """One tag anywhere switches the handoff stripping on; none keeps today's rendering."""
        assert identity_in_use({"C005": 5}, None)
        assert identity_in_use({}, [{"routing_tag": None}, {"routing_tag": 12}])
        assert not identity_in_use({}, [{"routing_tag": None}, {}])
        assert not identity_in_use({}, None)

    def test_the_strip_expressions_match_the_communities_this_module_derives(self) -> None:
        """One expression per ASN half (no alternation); together they select the communities this module derives."""
        import re

        regexes = [re.compile(expression.strip("^$").join(("^", "$"))) for expression in routing_community_regexes()]
        for community in (customer_community(1), customer_community(65534), segment_community(12)):
            assert any(regex.match(community) for regex in regexes)
        for other in ("65000:5", "64498:5", "64496:", "64496:x", "7224:5"):
            assert not any(regex.match(other) for regex in regexes)
        assert all("|" not in expression for expression in routing_community_regexes())

    @pytest.mark.parametrize(
        ("name", "expected"), [("c005-web-p", "c005-web-p"), ("C 005/web", "C-005-web"), ("a_b.c-d", "a_b.c-d")]
    )
    def test_list_name_part_keeps_only_characters_a_prefix_list_name_allows(self, name: str, expected: str) -> None:
        """Anything else becomes a hyphen."""
        assert list_name_part(name) == expected


# ---------------------------------------------------------------------------
# transforms/helpers/origination.py
# ---------------------------------------------------------------------------


def _raw_segment(
    name: str,
    *,
    typename: str = "ManagedVxlanSegment",
    prefix: str | None = "10.100.0.0/24",
    address: str | None = "10.100.0.1/24",
    namespace: str = "VRF_A",
    segment_tag: int | None = 12,
    owner_tag: int | None = 5,
    inline: bool = False,
) -> dict[str, Any]:
    """A ManagedNetworkSegment node as the leaf query returns it (raw GraphQL, before clean_data)."""
    node: dict[str, Any] = {
        "__typename": typename,
        "id": f"seg-{name}",
        "name": {"value": name},
        "routing_tag": {"value": segment_tag},
        "owner": {"node": {"id": "cust", "org_id": {"value": "C005"}, "routing_tag": {"value": owner_tag}}},
        "terminate_inline": {"value": inline},
        "gateway": {"node": None},
    }
    if prefix:
        node["gateway"] = {
            "node": {
                "address": {"value": address},
                "ip_prefix": {
                    "node": {
                        "prefix": {"value": prefix},
                        "ip_namespace": {"node": {"name": {"value": namespace}, "l3_vni": {"value": 50001}}},
                    }
                },
            }
        }
    return node


def _activations(*segments: dict[str, Any]) -> list[dict[str, Any]]:
    """Cleaned segment activations (``segment_deployments`` of the device) of raw segment nodes."""
    return [
        {"vlan_id": 100 + index, "vni": 10100 + index, "segment": clean_data({"s": {"node": segment}})["s"]}
        for index, segment in enumerate(segments)
    ]


class TestOrigination:
    """Which routed segment prefixes originate with which communities."""

    def test_a_tagged_segment_originates_its_prefix_with_both_communities(self) -> None:
        """Customer community first, then the segment's, on the gateway's subnet in its tenant VRF."""
        assert get_origination(_activations(_raw_segment("seg-100")), ["VRF_A"]) == [
            {
                "name": "seg-100",
                "vrf": "VRF_A",
                "prefix": "10.100.0.0/24",
                "communities": ["64496:5", "64497:12"],
            }
        ]

    def test_an_untagged_segment_originates_nothing(self) -> None:
        """No tag, no entry: the device renders exactly as before."""
        activations = _activations(_raw_segment("seg-100", segment_tag=None, owner_tag=None))
        assert get_origination(activations, ["VRF_A"]) == []
        assert get_origination(None, ["VRF_A"]) == []
        assert get_origination([], ["VRF_A"]) == []

    def test_one_missing_tag_leaves_the_other_community(self) -> None:
        """A customer without a tag still tags its segment, and the reverse."""
        activations = _activations(
            _raw_segment("seg-a", owner_tag=None),
            _raw_segment("seg-b", prefix="10.101.0.0/24", address="10.101.0.1/24", segment_tag=None),
        )
        assert [(e["name"], e["communities"]) for e in get_origination(activations, ["VRF_A"])] == [
            ("seg-a", ["64497:12"]),
            ("seg-b", ["64496:5"]),
        ]

    def test_entries_are_sorted_by_segment_name(self) -> None:
        """The order of the activations does not move a route-map sequence."""
        activations = _activations(
            _raw_segment("seg-b", prefix="10.101.0.0/24", address="10.101.0.1/24"),
            _raw_segment("seg-a"),
        )
        assert [e["name"] for e in get_origination(activations, ["VRF_A"])] == ["seg-a", "seg-b"]

    @pytest.mark.parametrize(
        "segment",
        [
            _raw_segment("seg-inline", inline=True),
            _raw_segment("seg-nogw", prefix=None),
            _raw_segment("seg-v6", prefix="2001:db8::/64", address="2001:db8::1/64"),
            _raw_segment("seg-default", namespace="default"),
            _raw_segment("seg-other-vrf", namespace="VRF_B"),
        ],
        ids=lambda segment: segment["name"]["value"],
    )
    def test_a_segment_the_fabric_does_not_route_into_evpn_is_skipped(self, segment: dict[str, Any]) -> None:
        """Inline-terminated, gateway-less, IPv6, default-namespace and not-in-the-VRFs segments have no SVI export."""
        assert get_origination(_activations(segment), ["VRF_A"]) == []

    def test_the_vrfs_are_the_ones_that_redistribute_into_evpn(self) -> None:
        """A tagged segment in a VRF with no L3 VNI mapping is not part of the export."""
        assert get_origination(_activations(_raw_segment("seg-100")), []) == []

    def test_a_segment_name_is_made_safe_for_a_prefix_list_name(self) -> None:
        """The entry name feeds ``PL-ORIG-<name>``; the segment keeps its own name elsewhere."""
        entry = get_origination(_activations(_raw_segment("c005 web/p")), ["VRF_A"])[0]
        assert entry["name"] == "c005-web-p"


# ---------------------------------------------------------------------------
# The queries carry the tags
# ---------------------------------------------------------------------------


def _fragment(path: str, name: str) -> FragmentDefinitionNode:
    """The named fragment of a queries/fragments file."""
    document = parse((_QUERIES / "fragments" / path).read_text())
    return next(d for d in document.definitions if isinstance(d, FragmentDefinitionNode) and d.name.value == name)


def _fields(selection_owner: Any, *path: str) -> dict[str, FieldNode]:
    """The fields selected below ``selection_owner`` after following the field names in ``path``."""
    for name in path:
        selection_owner = _fields(selection_owner)[name]
    assert selection_owner.selection_set is not None
    return {s.name.value: s for s in selection_owner.selection_set.selections if isinstance(s, FieldNode)}


class TestQueriesSelectTheTags:
    """The leaf, border and edge payloads carry the tags the templates read."""

    def test_the_segment_fragment_selects_the_segment_and_owner_tag(self) -> None:
        """NetworkSegmentFields (every interface capability of the leaf) reads the tag next to the owner."""
        fragment = _fragment("network_segment.gql", "SegmentSecurityPolicyFields")
        assert "routing_tag" in _fields(fragment)
        assert set(_fields(fragment, "owner", "node")) == {"id", "org_id", "routing_tag"}

    def test_the_segment_deployments_fragment_selects_them_on_the_generic(self) -> None:
        """The border and BGW activations come from the deployment's segments: tag and owner tag, never in a `... on`."""
        fragment = _fragment("network_segment.gql", "SegmentDeploymentsOnDeploymentFields")
        segment = _fields(fragment, "segment_deployments", "edges", "node", "segment", "node")
        assert "routing_tag" in segment
        assert set(_fields(fragment, "segment_deployments", "edges", "node", "segment", "node", "owner", "node")) == {
            "routing_tag"
        }

    def _edge_roots(self) -> dict[str, FieldNode]:
        """The top-level fields of the edge_config operation."""
        document = parse((_QUERIES / "config" / "edge.gql").read_text())
        operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
        return _fields(operation)

    def test_the_edge_reads_the_customers_tags_and_the_segments_tag(self) -> None:
        """OrganizationCustomer is keyed like customer_owner (org_id, display_label); the segment root adds its tag."""
        roots = self._edge_roots()
        assert set(_fields(roots["OrganizationCustomer"], "edges", "node")) == {
            "org_id",
            "display_label",
            "routing_tag",
        }
        assert "routing_tag" in _fields(roots["ManagedNetworkSegment"], "edges", "node")

    def test_the_selected_fields_exist_on_the_schema(self) -> None:
        """routing_tag is a Number on the customer and on the segment generic (so on every segment kind)."""
        for kind in ("OrganizationCustomer", "ManagedNetworkSegment", "ManagedVxlanSegment"):
            assert "routing_tag" in demo_data.attribute_names(kind)
        assert (demo_data.attribute("OrganizationCustomer", "routing_tag") or {}).get("kind") == "Number"

    def test_a_raw_edge_response_cleans_to_the_shape_the_transform_reads(self) -> None:
        """customer_tags reads the cleaned OrganizationCustomer root; identity_in_use the segment root."""
        raw = {
            "OrganizationCustomer": {
                "edges": [
                    {"node": {"org_id": {"value": "C003"}, "display_label": "C003 x", "routing_tag": {"value": 3}}}
                ]
            },
            "ManagedNetworkSegment": {
                "edges": [{"node": {"status": {"value": "active"}, "routing_tag": {"value": 7}}}]
            },
        }
        cleaned = clean_data(raw)
        tags = customer_tags(cleaned["OrganizationCustomer"])
        assert tags == {"C003": 3}
        assert identity_in_use({}, cleaned["ManagedNetworkSegment"])
