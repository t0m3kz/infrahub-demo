"""Every config query that renders the SVIs of a switch also reads what its PBR and its service contexts derive from.

A leaf steers an SVI to the firewall only when its segment has a footprint, in the device's DC, that a firewall context
serves (utils/device_services.py ``pbr_bindings``), and a border device finds the contexts behind its service ports from
their cables (utils/service_contexts.py). The fixtures are hand-built, so a query that forgets the ``DeviceServices`` root
renders every golden unchanged and silently drops PBR and the transit legs on a real device: this pins the query.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graphql import FragmentDefinitionNode, OperationDefinitionNode, parse

from tests.unit.query_shape_helpers import Tree, _selection_tree
from utils.service_contexts import DEVICE_SERVICES_ROOT

_CONFIG = Path(__file__).parents[2] / "queries" / "config"
_FRAGMENTS = Path(__file__).parents[2] / "queries" / "fragments"
_SVI_READERS = sorted(
    path
    for path in _CONFIG.glob("*.gql")
    if "InterfaceCapabilitiesWithSegment" in path.read_text() and path.stem != "firewall"
)


def _tree(path: Path) -> Tree:
    """The fields the config query selects, its repository fragments (queries/fragments) resolved."""
    text = path.read_text() + "\n" + "\n".join(f.read_text() for f in sorted(_FRAGMENTS.glob("*.gql")))
    document = parse(text)
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return _selection_tree(operation.selection_set, fragments)


def test_the_queries_that_read_svis_are_found() -> None:
    """Guard of the glob: the leaf and the border leaf are among them."""
    assert {"leaf.gql", "border_leaf.gql"} <= {path.name for path in _SVI_READERS}


@pytest.mark.parametrize("path", _SVI_READERS, ids=lambda path: path.name)
def test_the_query_has_the_services_root_after_the_device(path: Path) -> None:
    """The device stays the first root (transforms/common.py ``_device_and_platform``); the services root follows."""
    roots = list(_tree(path))
    assert roots[0] == "DcimDevice"
    assert DEVICE_SERVICES_ROOT in roots


@pytest.mark.parametrize("path", _SVI_READERS, ids=lambda path: path.name)
def test_the_services_root_reads_the_rows_the_hosting_and_the_service_ports(path: Path) -> None:
    """The segment rows with the footprints and their serving contexts, the hosting DC, the ports with their members."""
    node = _tree(path)[DEVICE_SERVICES_ROOT]["edges"]["node"]
    row = node["vlans"]["edges"]["node"]["segment_deployment"]["node"]["segment"]["node"]
    assert {"terminated_on", "customer_deployments", "gateway", "security_zone"} <= set(row)
    footprint = row["customer_deployments"]["edges"]["node"]
    assert {"parent", "serving_firewall_context", "serving_firewall_contexts"} <= set(footprint)
    assert "parent" in node["deployment"]["node"]
    far_end = node["service_ports"]["edges"]["node"]["cable"]["node"]["endpoints"]["edges"]["node"]
    member = far_end["device"]["node"]
    assert {"role", "shared_capabilities", "interfaces"} <= set(member)
    assert {"fw_served", "lb_served", "vlan_id"} <= set(member["shared_capabilities"]["edges"]["node"])


def test_the_firewall_query_reads_the_role_its_legs_are_derived_from() -> None:
    """A device's contexts are those of its role's kind (utils/exchange_transit.py BORDER_ROLE_FOR_SERVICES): a firewall
    query that leaves the role out derives no leg and renders every context empty, goldens included."""
    node = _tree(_CONFIG / "firewall.gql")["DcimDevice"]["edges"]["node"]
    assert {"role", "shared_capabilities", "interfaces"} <= set(node)
    interface = node["interfaces"]["edges"]["node"]
    assert {"role", "ip_address", "vrf", "parent_interface"} <= set(interface)
