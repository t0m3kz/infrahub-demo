"""test_64's DCI render rule, proven on the golden edge config instead of a live render.

``_dci_session_errors`` reads the NX-OS config an edge renders for its DCI circuits (two neighbours per circuit: the
underlay eBGP and the loopback-to-loopback EVPN session). The smoke fixture ``edge_cisco_nxos_bgw_pair`` is a rendered
border gateway with two DCI circuits, so the rule must find nothing wrong with it, and name what is missing when the
render loses a line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration import firewall_config_helpers as firewall_config

_GOLDEN = Path(__file__).parents[1] / "smoke" / "configs" / "edge_cisco_nxos_bgw_pair" / "output.txt"


@pytest.fixture
def rendered() -> str:
    """The golden NX-OS config of the edge, a border gateway with two DCI circuits."""
    return _GOLDEN.read_text()


def test_the_golden_edge_renders_both_sessions_of_each_of_its_two_dci_circuits(rendered: str) -> None:
    """Two circuits expect two underlay and two EVPN neighbours; the golden render holds all four."""
    assert firewall_config._dci_session_errors("dc1-edge-01", rendered, 2) == []


def test_a_circuit_the_render_does_not_hold_is_reported_on_both_session_kinds(rendered: str) -> None:
    """Expecting three circuits when the render holds two reports both neighbour groups short by one."""
    errors = firewall_config._dci_session_errors("dc1-edge-01", rendered, 3)
    assert errors == [
        "dc1-edge-01: 2 DCI-UNDERLAY-PEERS neighbour(s), expected 3",
        "dc1-edge-01: 2 DCI-PEERS neighbour(s), expected 3",
    ]


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (
            "      route-map RM-peer-dc1-edge-01-dc10-border-leaf-01-ebgp-IPV6-IN in\n",
            "a DCI underlay neighbour has no 'route-map <name> in'",
        ),
        (
            "      route-map RM-peer-dc1-edge-01-dc10-border-leaf-01-ebgp-IPV6-OUT out\n",
            "a DCI underlay neighbour has no 'route-map <name> out'",
        ),
        ("      maximum-prefix 4\n", "a DCI underlay neighbour has no 'maximum-prefix <n>'"),
        ("    update-source Loopback1\n", "a DCI EVPN neighbour has no 'update-source'"),
        ("    ebgp-multihop 2\n", "DCI-PEERS-SESSION does not give 'ebgp-multihop 2'"),
        (
            "  set community no-export additive\n",
            "does not tag what the other site sends no-export",
        ),
    ],
)
def test_a_missing_line_of_the_dci_render_is_named(rendered: str, line: str, error: str) -> None:
    """Dropping one DCI line names it (the limit is derived from the far gateway; multihop 2 is loopback to loopback)."""
    assert line in rendered
    errors = firewall_config._dci_session_errors("dc1-edge-01", rendered.replace(line, ""), 2)
    assert any(error in e for e in errors)
