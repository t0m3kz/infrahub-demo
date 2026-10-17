"""The DC fields (EVPN route-target ASN, LISP site) only resolve on the pod -> parent hop in one shape.

An office's switches hang directly off the office (the direct shape of a border-leaf), where the order is not
known to matter; the rule is kept for them so the fields never depend on it.
"""

import re
from pathlib import Path

import pytest

QUERIES = Path(__file__).resolve().parents[2] / "queries"
CONFIG_QUERIES = ["leaf", "border_leaf", "access_switch", "distribution_switch"]


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


@pytest.mark.parametrize("query", ["access_switch", "distribution_switch"])
def test_office_switch_queries_read_the_lisp_inputs_from_the_office(query: str) -> None:
    """The office is the LISP site: its name, key and map-servers come from the one deployment selection."""
    spreads = _deployment_spreads(query)
    assert "LispMapServersOnDeploymentFields" in spreads, spreads
    # No fleet roots: an office has no data center, reference fabric or group policy.
    text = (QUERIES / "config" / f"{query}.gql").read_text()
    assert "TopologyDataCenter" not in text and "DcimGroupPolicyProfile" not in text


def test_only_the_border_office_switch_reads_the_segment_activations() -> None:
    """The distribution-switch registers the site's EID records; the access-switch hosts its own segments."""
    assert "SegmentDeploymentsOnDeploymentFields" in _deployment_spreads("distribution_switch")
    assert "SegmentDeploymentsOnDeploymentFields" not in _deployment_spreads("access_switch")


def test_the_map_servers_are_the_border_leaves_and_the_distribution_switches() -> None:
    """One fragment serves both fabrics: a footprint holds only its own kind of border."""
    text = (QUERIES / "fragments" / "lisp_fabric.gql").read_text()
    assert 'lisp_map_servers: devices(role__values: ["border-leaf", "distribution-switch"])' in text


@pytest.mark.parametrize("query", ["access_switch", "distribution_switch"])
def test_office_switch_queries_read_the_offices_sdwan_edge_from_the_office(query: str) -> None:
    """The WAN exit's signal (the office has an SD-WAN edge) rides a DeviceHosting fragment, after the SegmentHosting one."""
    spreads = _deployment_spreads(query)
    assert "LispWanEdgesOnDeploymentFields" in spreads, spreads
    assert spreads.index("EvpnRtAsOnDeploymentFields") == 0


def test_the_sdwan_edge_fragment_is_deployment_hosting_typed_and_in_no_dc_query() -> None:
    """A SegmentHosting-typed fragment would join the pod -> parent hop quirk; and a DC's LISP fabric has no WAN exit."""
    text = (QUERIES / "fragments" / "lisp_fabric.gql").read_text()
    assert "fragment LispWanEdgesOnDeploymentFields on TopologyDeviceHosting" in text
    assert 'lisp_wan_edges: devices(role__value: "edge")' in text
    for query in ("leaf", "border_leaf"):
        assert "LispWanEdgesOnDeploymentFields" not in _deployment_spreads(query)
