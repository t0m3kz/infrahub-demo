"""A hyper-spine renders as an EVPN route reflector, exactly like a super-spine.

The hyper-spine artifact reuses the SuperSpine transform and templates; only the
device role in the query data differs, so any role list that omits it silently
drops the route-reflector config.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from transforms.config.super_spine import SuperSpine

_CONFIGS = Path(__file__).resolve().parents[1] / "smoke" / "configs"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RR_MARKER = {
    "arista_eos": "neighbor EVPN-PEERS route-reflector-client",
    "cisco_nxos": "route-reflector-client",
    "sonic": '"route_reflector_client": "true"',
    "dell_sonic": '"route_reflector_client": "true"',
}


def _render(role: str, platform: str, scenario: str) -> str:
    data: dict[str, Any] = json.loads((_CONFIGS / f"super_spine_{platform}_{scenario}" / "input.json").read_text())
    data["DcimDevice"]["edges"][0]["node"]["role"] = {"value": role}
    transform = SuperSpine(client=MagicMock(), infrahub_node=MagicMock(), root_directory=str(_PROJECT_ROOT))
    return asyncio.run(transform.transform(data))


@pytest.mark.parametrize("scenario", ["ospf_ibgp", "ebgp_ibgp"])
@pytest.mark.parametrize("platform", sorted(_RR_MARKER))
def test_hyper_spine_renders_route_reflector_peer_group(platform: str, scenario: str) -> None:
    """With RR-flagged overlay sessions a hyper-spine's EVPN peer group is a route-reflector-client group."""
    rendered = _render("hyper-spine", platform, scenario)
    assert _RR_MARKER[platform] in rendered


@pytest.mark.parametrize("platform", sorted(_RR_MARKER))
def test_hyper_spine_matches_super_spine_route_reflector_config(platform: str) -> None:
    """The route-reflector lines are identical for the two roles on the same data."""

    def rr_lines(text: str) -> list[str]:
        return [line for line in text.splitlines() if "reflector" in line or "cluster" in line]

    assert rr_lines(_render("hyper-spine", platform, "ospf_ibgp")) == rr_lines(
        _render("super-spine", platform, "ospf_ibgp")
    )


@pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos"])
def test_leaf_role_on_same_data_is_not_a_route_reflector(platform: str) -> None:
    """Failure path: a non-RR role on the identical RR-flagged data gets no route-reflector-client."""
    assert _RR_MARKER[platform] not in _render("l2-leaf", platform, "ospf_ibgp")
