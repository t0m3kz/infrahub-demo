"""The queries whose data goes through the fabric gate must select ``terminated_on``.

``utils/segment_addressing.is_terminated`` reads ``terminated_on`` of the cleaned segment. A query that returns a
segment's gateway without it reads the segment as routed, silently: the leaf renders an SVI the firewall owns, or the
segment generator tags a segment nothing routes. Each query below feeds one of the readers of the gate (the fabric
transforms, the VRF demand, ``is_routed_tenant_segment`` for the tags, the group policy and the SVI protection check).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.query_shape_helpers import compact

_FEEDS_THE_GATE = {
    # The fabric configs (leaf, border leaf, edge, ... spread these) and the VRF demand.
    "queries/fragments/network_segment.gql": 2,
    "queries/validation/vrf_instances.gql": 1,
    # is_routed_tenant_segment: the tags, the SVI protection, the group policy and the routing-tag check.
    "queries/fragments/group_policy.gql": 1,
    "queries/validation/tenant_svi_protection.gql": 1,
    "queries/validation/routing_tags.gql": 1,
    "queries/topology/add/vxlan_segment.gql": 1,
    "queries/topology/add/application.gql": 1,
    # The PBR bypass of a permit never covers a terminated peer.
    "queries/fragments/security_policy.gql": 1,
}


@pytest.mark.parametrize(("path", "selections"), sorted(_FEEDS_THE_GATE.items()))
def test_the_query_selects_terminated_on(root_dir: Path, path: str, selections: int) -> None:
    """Each selection of a segment's gateway for the gate sits next to a ``terminated_on { node { id } }``."""
    text = compact((root_dir / path).read_text())
    assert text.count("terminated_on { node { id } }") == selections
