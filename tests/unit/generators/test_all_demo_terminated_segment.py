"""The 30_all segment with a firewall gateway (data/demos/30_all/07_applications/c005/00_segments.yml)."""

from __future__ import annotations

from tests.integration.demo_data import BOOTSTRAP, ROOT, declared, name

_DEMO = ROOT / "data" / "demos" / "30_all"


def _terminated_segments() -> list[dict]:
    """The 30_all segments with ``firewall_gateway``."""
    return [obj.item for obj in declared("ManagedVxlanSegment", _DEMO) if obj.item.get("firewall_gateway")]


def test_the_demo_terminates_a_segment_on_a_dc_firewall_context() -> None:
    """At least one segment of the demo has a firewall gateway (the case the terminated fixtures stand for)."""
    assert _terminated_segments(), "no 30_all segment sets firewall_gateway"


def test_a_terminated_segment_names_one_dc_footprint_on_a_shared_pair() -> None:
    """The footprint is a DC one whose design uses the shared pair (a dedicated pair hosts virtual firewalls."""
    designs = {name(design): design for design in declared("TopologyCustomerTemplateDC", BOOTSTRAP)}
    footprints = {name(obj): obj for obj in declared("TopologyCustomerDC", _DEMO)}
    for segment in _terminated_segments():
        deployments = segment["customer_deployments"]
        assert len(deployments) == 1, f"{segment['customer_name']}: a terminated segment is local to one footprint"
        footprint = footprints[deployments[0]]
        assert not designs[footprint.item["design"]].value("dedicated_firewall"), deployments[0]


def test_a_terminated_segment_types_no_gateway() -> None:
    """The gateway is the .1 add_vxlan_segment takes from the tenant pool: it is the pair's VIP, not typed data."""
    assert all("gateway" not in segment for segment in _terminated_segments())
