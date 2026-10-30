"""Readers of a cable end tolerate a DcimPatchPanelPort end.

A cable ends on a physical interface or on a DcimPatchPanelPort (both are DcimEndpoint). A query that reads an end
through ``... on DcimPhysicalInterface`` gets an EMPTY node ``{}`` for a panel port (queries/topology/cabling.gql,
clab.gql), one that selects ``id`` gets ``{"id": ...}`` only. The payloads here are raw GraphQL shapes run through
clean_data where the reader does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import jinja2
import yaml

from transforms.helpers.group_policy.nxos import _place_instance
from transforms.topology.cabling import TopologyCabling
from utils.circuit_underlay import port_cabled

_ROOT = Path(__file__).resolve().parents[2]


def _interface_end(device: str, name: str) -> dict[str, Any]:
    return {
        "node": {
            "hfid": [device, name],
            "name": {"value": name},
            "device": {"node": {"name": {"value": device}, "rack": {"node": {"name": {"value": "R1"}}}}},
        }
    }


def test_cabling_matrix_skips_a_cable_with_a_panel_port_end_and_keeps_the_rest() -> None:
    """The panel end comes back as ``{}`` (no hfid): that cable is left out of the CSV, the plain cable stays."""
    plain = {
        "node": {
            "id": "c-1",
            "type": {"value": "smf"},
            "endpoints": {"edges": [_interface_end("leaf-1", "Ethernet1"), _interface_end("spine-1", "Ethernet1")]},
        }
    }
    via_panel = {
        "node": {
            "id": "c-2",
            "type": {"value": "smf"},
            "endpoints": {"edges": [_interface_end("leaf-1", "Ethernet2"), {"node": {}}]},
        }
    }
    data = {
        "TopologyPhysicalDeployment": {
            "edges": [{"node": {"name": {"value": "DC1"}, "cables": {"edges": [plain, via_panel]}}}]
        }
    }
    transform = cast(Any, TopologyCabling.__new__(TopologyCabling))

    csv_text = asyncio.run(transform.transform(data))

    rows = csv_text.strip().splitlines()
    assert len(rows) == 2 and rows[1].startswith("DC1,R1,leaf-1,Ethernet1,R1,spine-1,Ethernet1,smf")


def _clab_render(endpoints: list[dict[str, Any]]) -> dict[str, Any]:
    """Render clab_topology.j2 for one device ``leaf-a`` whose only port carries a cable with ``endpoints``."""
    device = {
        "node": {
            "name": {"value": "leaf-a"},
            "platform": {"node": {"containerlab_os": {"value": "ceos"}}},
            "software_image": {"node": {"containerlab_image": {"value": "ceos:4"}}},
            "interfaces": {
                "edges": [
                    {"node": {"name": {"value": "Ethernet1"}, "cable": {"node": {"endpoints": {"edges": endpoints}}}}}
                ]
            },
        }
    }
    template = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_ROOT / "templates")), trim_blocks=True, lstrip_blocks=True
    ).get_template("clab_topology.j2")
    rendered = template.render(
        data={"TopologyDeployment": {"edges": [{"node": {"name": {"value": "lab"}, "devices": {"edges": [device]}}}]}}
    )
    return next(yaml.safe_load_all(rendered))


def _clab_end(device: str, name: str) -> dict[str, Any]:
    return {
        "node": {
            "name": {"value": name},
            "device": {
                "node": {
                    "name": {"value": device},
                    "platform": {"node": {"containerlab_os": {"value": "ceos"}}},
                }
            },
        }
    }


def test_containerlab_topology_renders_with_a_panel_port_end_and_drops_that_link() -> None:
    """An empty endpoint node (fragment on DcimPhysicalInterface did not match) draws no link and does not raise."""
    topology = _clab_render([_clab_end("leaf-a", "Ethernet1"), {"node": {}}])

    assert "links" in topology["topology"] and not topology["topology"]["links"]


def test_containerlab_topology_still_draws_a_link_between_two_interfaces() -> None:
    """The guard changes nothing for two physical ends."""
    topology = _clab_render([_clab_end("leaf-a", "Ethernet1"), _clab_end("leaf-b", "Ethernet1")])

    assert topology["topology"]["links"] == [{"endpoints": ["leaf-a:eth1", "leaf-b:eth1"]}]


def test_group_policy_host_nic_cabled_to_a_panel_port_is_a_problem_not_a_crash() -> None:
    """The far end selects ``id`` only: the NIC 'is cabled to something that is not a switch port'."""
    instance = {
        "typename": "DcimPhysicalDevice",
        "id": "srv-1",
        "name": "srv-1",
        "interfaces": [
            {"id": "nic-1", "name": "eth0", "cable": {"endpoints": [{"id": "nic-1"}, {"id": "panel-port-1"}]}}
        ],
    }

    ports, problems = _place_instance(instance, "seg-1", vtep_names={"leaf-1"}, other_vtep_names=set())

    assert ports == set()
    assert any("not a switch port" in problem for problem in problems)


def test_a_port_cabled_to_a_panel_counts_as_carried() -> None:
    """utils/circuit_underlay.port_cabled asks only whether a cable is on the port: a panel cable carries it."""
    assert port_cabled({"cable": {"id": "c-1"}}) is True
    assert port_cabled({"cable": None}) is False
