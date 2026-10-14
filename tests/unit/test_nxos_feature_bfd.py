"""Unit tests for templates/configs/common/cisco_nxos_feature_bfd.j2.

NX-OS rejects `bfd` under a BGP neighbor or peer-session template unless
`feature bfd` is enabled, so the device templates must emit it exactly when
cisco_nxos_bgp.j2 renders a `bfd` line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "configs"
_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATES)), trim_blocks=False, keep_trailing_newline=True)
_DEVICE_TEMPLATES = ["leafs", "border_leafs", "spines", "super_spines", "edges"]


def _bgp(
    peer_groups: list[dict[str, Any]] | None = None, sessions: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Build a minimal bgp profile list for the partial."""
    return [{"peer_groups": peer_groups or [], "sessions": sessions or []}]


def _render_feature(bgp: list[dict[str, Any]], **extra: Any) -> str:
    """Render only the feature-bfd partial."""
    return _ENV.get_template("common/cisco_nxos_feature_bfd.j2").render(bgp=bgp, **extra)


@pytest.mark.parametrize(
    ("bgp", "expected"),
    [
        (_bgp(), False),
        (_bgp(sessions=[{"neighbor": "10.0.0.1", "bfd_enabled": False}]), False),
        (_bgp(peer_groups=[{"name": "UNDERLAY", "bfd_enabled": True}]), True),
        (_bgp(peer_groups=[{"name": "UNDERLAY", "bfd_enabled": False}]), False),
        (_bgp(sessions=[{"neighbor": "10.0.0.1", "bfd_enabled": True}]), True),
        (_bgp(sessions=[{"neighbor": "10.0.0.1", "bfd_enabled": True, "vrf": "TENANT"}]), True),
        # bfd on a session that inherits a peer group is not rendered by cisco_nxos_bgp.j2
        (_bgp(sessions=[{"neighbor": "10.0.0.1", "bfd_enabled": True, "peer_group": "PG"}]), False),
        (_bgp(sessions=[{"neighbor": "10.0.0.1", "bfd_enabled": True, "peer_group": "PG", "vrf": "T"}]), True),
    ],
)
def test_feature_bfd_emitted_iff_a_bfd_line_is_rendered(bgp: list[dict[str, Any]], expected: bool) -> None:
    """`feature bfd` appears exactly when a neighbor or peer group renders `bfd`."""
    assert (_render_feature(bgp).strip() == "feature bfd") is expected
    assert _render_feature(bgp).strip() in ("", "feature bfd")


def test_feature_bfd_suppressed_when_bgp_disabled() -> None:
    """A device with BGP disabled by capability gets no `feature bfd`."""
    bgp = _bgp(peer_groups=[{"name": "PG", "bfd_enabled": True}])
    assert _render_feature(bgp, capabilities={"bgp_enabled": False}).strip() == ""


@pytest.mark.parametrize("role_dir", _DEVICE_TEMPLATES)
def test_device_templates_include_the_feature_partial(role_dir: str) -> None:
    """Every NX-OS device template that enables BGP includes the bfd feature partial."""
    source = (_TEMPLATES / role_dir / "cisco_nxos.j2").read_text()
    assert "feature bgp\n{%- include 'common/cisco_nxos_feature_bfd.j2' %}" in source
