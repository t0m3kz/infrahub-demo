"""The VRF instance's deployment and the segment deployment's deployment must be the same node.

They are chosen by different code: ``hosting_deployment`` (utils/segment_addressing.py, the device side, used by the
VRF demand) and ``VlanDomainMixin._deployment_by_device_deployment`` (generators/vlan_domain.py, the row-writing side,
fed by ``_device_deployment_ids``). This pins that they agree for a pod, a colocation zone and a virtual colocation.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.vlan_domain import VlanDomainMixin
from utils.segment_addressing import hosting_deployment

_DC = "TopologyDataCenter"
_METRO = "TopologyColocationMetro"


def _state(parent_id: str, child_ids: list[str]) -> dict[str, Any]:
    """The segment_vlan_domains.gql state of a segment realized on one hosting parent with its children."""
    return {
        "deployments": [{"id": "segdep-1", "deployment_id": parent_id, "rows": {}}],
        "segment": {
            "customer_deployments": [
                {"id": "footprint", "parent": {"id": parent_id, "children": [{"id": c} for c in child_ids]}}
            ]
        },
    }


@pytest.mark.parametrize(
    ("child_type", "parent_type"),
    [
        ("TopologyPod", _DC),
        ("TopologyColocationZone", _METRO),
        ("TopologyVirtualColocation", _METRO),
        (_DC, None),
    ],
)
def test_the_device_side_and_the_row_side_pick_the_same_hosting_node(child_type: str, parent_type: str | None) -> None:
    """A device in a pod / zone / virtual colocation (or the DC itself) resolves to the node the rows sit under."""
    device_deployment: dict[str, Any] = {"id": "child-1", "typename": child_type}
    if parent_type:
        device_deployment["parent"] = {"id": "host-1", "typename": parent_type}
    else:
        device_deployment["id"] = "host-1"
    children = ["child-1"] if parent_type else []

    mapping = VlanDomainMixin._deployment_by_device_deployment(_state("host-1", children))
    hosting = hosting_deployment(device_deployment)

    assert hosting is not None
    assert hosting["id"] == "host-1"
    assert mapping[device_deployment["id"]] == "segdep-1"
    assert mapping[hosting["id"]] == "segdep-1"
