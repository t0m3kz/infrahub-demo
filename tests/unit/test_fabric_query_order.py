"""The DC fields (EVPN route-target ASN, LISP site) only resolve on the pod -> parent hop in one shape."""

import re
from pathlib import Path

import pytest

QUERIES = Path(__file__).resolve().parents[2] / "queries"
CONFIG_QUERIES = ["leaf", "border_leaf"]


def _deployment_spreads(query: str) -> list[str]:
    """The named fragments spread directly in the device's ``deployment { node { ... } }`` selection."""
    text = (QUERIES / "config" / f"{query}.gql").read_text()
    match = re.search(r"deployment\s*\{\s*node\s*\{(.*?)\n\s*\}\s*\n\s*\}", text, re.S)
    assert match, f"{query}.gql has no deployment selection"
    return re.findall(r"\.\.\.(\w+)", match.group(1))


@pytest.mark.parametrize("query", CONFIG_QUERIES)
def test_evpn_fabric_fragment_is_spread_first(query: str) -> None:
    """Infrahub nulls SegmentHosting fields of the parent hop when a DeviceHosting spread precedes them.

    On the 30_all stack every leaf and access-leaf got evpn_rt_as = null that way and derived its
    route-targets from its own ASN, while the border-leafs used the fabric's.
    """
    spreads = _deployment_spreads(query)
    assert spreads[0] == "EvpnRtAsOnDeploymentFields", spreads


def test_the_pod_tier_query_reads_the_dc_fields_from_a_single_segment_hosting_fragment() -> None:
    """A second SegmentHosting-typed fragment on the pod -> parent hop would resolve to nulls.

    SegmentDeploymentsOnDeploymentFields is also SegmentHosting-typed but is read by the DC-level
    border-leaf query only (direct match, no parent hop), which is why it is not in the leaf query.
    """
    typed = sorted(
        name
        for path in (QUERIES / "fragments").glob("*.gql")
        for name in re.findall(r"^fragment (\w+) on TopologySegmentHosting", path.read_text(), re.M)
    )
    assert typed == ["EvpnRtAsOnSegmentHostingFields", "SegmentDeploymentsOnDeploymentFields"], typed
    assert "SegmentDeploymentsOnDeploymentFields" not in _deployment_spreads("leaf")
