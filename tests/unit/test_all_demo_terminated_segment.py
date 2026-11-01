"""The 30_all segment terminated on a firewall context (data/demos/30_all/07_applications/c005/00_segments.yml).

``terminated_on`` names a context the footprint boarding GENERATES (add_customer_deployment_dc, one shared context per
firewall pair and tenant namespace), so the data can only reference it by the name the generators derive. These tests
hold the data to what the generators and the check will find: a DC footprint on a shared pair, the context named
`shared-{first}-{second}-{class}` after the DC's firewall pair, and a gateway the segment generator (not the file)
allocates, like every DC tenant segment.
"""

from __future__ import annotations

from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig
from tests.integration.demo_data import BOOTSTRAP, ROOT, declared, name
from utils.exchange_transit import namespace_type_for_environment

_DEMO = ROOT / "data" / "demos" / "30_all"


def _terminated_segments() -> list[dict]:
    """The 30_all segments that name a ``terminated_on`` context."""
    return [obj.item for obj in declared("ManagedVxlanSegment", _DEMO) if "terminated_on" in obj.item]


def _context_name(dc_name: str, dc_index: int, environment: str) -> str:
    """The shared firewall context of a DC, as the generators name it: the pair `{first}-{second}`, then the class."""
    naming = DeviceNamingConfig()
    members = sorted(
        naming.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name=dc_name, device_role="firewall", role_index=role_index, indexes=[dc_index]
            )
        ).lower()
        for role_index in (1, 2)
    )
    scope = "prod" if namespace_type_for_environment(environment) == "prod" else "non-prod"
    return f"shared-{members[0]}-{members[1]}-{scope}"


def test_the_demo_terminates_a_segment_on_a_dc_firewall_context() -> None:
    """At least one segment of the demo is terminated (the case the terminated_on fixtures stand for)."""
    assert _terminated_segments(), "no 30_all segment names a terminated_on context"


def test_a_terminated_segment_names_one_dc_footprint_on_a_shared_pair() -> None:
    """The footprint is a DC one whose design uses the shared pair (a dedicated pair hosts virtual firewalls, which
    have no cable of their own to the border leaf), and the context is the shared one of that DC and environment."""
    designs = {name(design): design for design in declared("TopologyCustomerTemplateDC", BOOTSTRAP)}
    footprints = {name(obj): obj for obj in declared("TopologyCustomerDC", _DEMO)}
    dcs = {name(obj): obj for obj in declared("TopologyDataCenter", _DEMO)}
    for segment in _terminated_segments():
        deployments = segment["customer_deployments"]
        assert len(deployments) == 1, f"{segment['customer_name']}: a terminated segment is local to one footprint"
        footprint = footprints[deployments[0]]
        assert not designs[footprint.item["design"]].value("dedicated_firewall"), deployments[0]
        dc = dcs[footprint.item["parent"]]
        expected = _context_name(footprint.item["parent"], dc.item["index"], footprint.item["environment"])
        assert segment["terminated_on"] == expected


def test_a_terminated_segment_types_no_gateway() -> None:
    """The gateway is the .1 add_vxlan_segment takes from the tenant pool: it is the pair's VIP, not typed data."""
    assert all("gateway" not in segment for segment in _terminated_segments())
