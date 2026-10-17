"""The LISP fabric fields only resolve on the pod -> parent hop when their fragment is spread first."""

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
def test_lisp_site_fragment_is_spread_first(query: str) -> None:
    """Infrahub nulls SegmentHosting fields of the parent hop when a DeviceHosting spread precedes them."""
    spreads = _deployment_spreads(query)
    assert spreads[0] == "LispSiteOnDeploymentFields", spreads


@pytest.mark.parametrize("query", CONFIG_QUERIES)
def test_lisp_site_fields_live_in_a_single_segment_hosting_fragment(query: str) -> None:
    """A second SegmentHosting-typed LISP fragment would resolve to nulls, so there is exactly one."""
    text = (QUERIES / "fragments" / "lisp_fabric.gql").read_text()
    typed = re.findall(r"^fragment (\w+) on TopologySegmentHosting", text, re.M)
    assert typed == ["LispSiteOnSegmentHostingFields"]
    assert query in CONFIG_QUERIES
