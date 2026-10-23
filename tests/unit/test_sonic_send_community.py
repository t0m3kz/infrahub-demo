"""SONiC BGP send_community mapping (templates/configs/common/sonic_common.j2).

The two model flags map to three modes: both set -> "both", extended only -> "extended", standard only ->
"standard", neither -> no key. Checked on a peer group and on a neighbour without a peer group (a VRF session).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2
import pytest

from transforms.helpers.bgp import add_template_fields

_TEMPLATES = Path(__file__).parents[2] / "templates" / "configs"


def _render(*, standard: bool, extended: bool) -> dict[str, Any]:
    """Render the SONiC spine template with one peer group and one VRF neighbour carrying the two flags."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)))
    session = {
        "name": "to-peer",
        "ttl": 1,
        "vrf": "PROD",
        "remote_ip": {"address": "10.0.0.2/31"},
        "remote_as": {"asn": 65002},
        "send_community": standard,
        "send_extended_community": extended,
        "address_families": ["ipv4"],
    }
    group = {
        "name": "PG",
        "type": "underlay",
        "session_type": "EBGP",
        "send_community": standard,
        "send_extended_community": extended,
        "address_families": ["ipv4"],
    }
    bgp = [
        {
            "local_as": {"asn": 65001},
            "router_id": {"address": "10.0.0.1/32"},
            "sessions": [session],
            "peer_groups": [group],
        }
    ]
    add_template_fields(bgp[0])
    rendered = env.get_template("spines/sonic.j2").render(hostname="spine-1", bgp=bgp, interfaces=[], vlans=[], ospf=[])
    return json.loads(rendered)


@pytest.mark.parametrize(
    ("standard", "extended", "expected"),
    [(True, True, "both"), (False, True, "extended"), (True, False, "standard"), (False, False, None)],
    ids=["both", "extended-only", "standard-only", "neither"],
)
def test_send_community_mode_follows_the_two_model_flags(standard: bool, extended: bool, expected: str | None) -> None:
    """Peer group and neighbour both map the flags the same way; no flag, no key."""
    config = _render(standard=standard, extended=extended)
    assert config["BGP_PEER_GROUP"]["PG"].get("send_community") == expected
    assert config["BGP_NEIGHBOR"]["PROD|10.0.0.2"].get("send_community") == expected
