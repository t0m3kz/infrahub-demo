"""Routing-identity fixtures: tagged segments originated by a leaf and a border leaf, owner tags on an external edge.

Each fixture is a NEW directory next to the untagged ones, whose goldens stay byte-identical: a tag in the model is
the only thing that makes a template render a community (transforms/helpers/origination.py, utils/routing_identity.py).
"""

from __future__ import annotations

from tests.smoke.fixture_builders.common import _edges, _v, write_data_fixtures
from tests.smoke.fixture_builders.edge import build_edge_external_vrf_peers_data
from tests.smoke.fixture_builders.fabric import build_device_data
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.leaf import Leaf

# Leaf per platform with a SVI-originating tagged segment; the border leaf on the two CLI platforms.
LEAF_PLATFORMS = ("cisco_nxos", "arista_eos", "sonic")
BORDER_LEAF_PLATFORMS = ("cisco_nxos", "arista_eos")

# Customers of the external-peers edge fixture and their routing tags. C001 (the tunnel owner) has none: its
# accepted prefix is still stripped of our communities, with no customer community added.
CUSTOMER_TAGS: dict[str, int | None] = {"C001": None, "C002": 2, "C003": 3, "C005": 5, "C015": 15, "C009": None}


def build_edge_external_vrf_peers_tagged_data(*, device_name: str, platform: str) -> dict:
    """The external-peers edge with the customers' routing tags, as the ``OrganizationCustomer`` root returns them."""
    data = build_edge_external_vrf_peers_data(device_name=device_name, platform=platform)
    data["OrganizationCustomer"] = _edges(
        [
            {"org_id": _v(org_id), "display_label": f"{org_id} Customer", "routing_tag": _v(tag)}
            for org_id, tag in CUSTOMER_TAGS.items()
        ]
    )
    return data


def write_fixtures() -> tuple[int, int]:
    """Write the tagged leaf and border-leaf fixtures and the tagged external-peers edge."""
    fixtures: list[tuple[type, str, dict]] = [
        (
            transform_cls,
            f"{type_prefix}_{platform}_routing_identity",
            build_device_data(
                device_name=f"dc1-{type_prefix.replace('_', '-')}-01",
                role=role,
                platform=platform,
                scenario="ebgp_ibgp",
                include_segments=True,
                vlan_segment_vrf=True,
                routing_identity=True,
            ),
        )
        for transform_cls, role, type_prefix, platforms in (
            (Leaf, "leaf", "leaf", LEAF_PLATFORMS),
            (BorderLeaf, "border-leaf", "border_leaf", BORDER_LEAF_PLATFORMS),
        )
        for platform in platforms
    ]
    fixtures.append(
        (
            Edge,
            "edge_cisco_nxos_external_vrf_peers_routing_identity",
            build_edge_external_vrf_peers_tagged_data(device_name="dc1-edge-01", platform="cisco_nxos"),
        )
    )
    print("\nGenerating routing-identity fixtures (leaf, border-leaf, external-peers edge):")
    return write_data_fixtures(fixtures)
