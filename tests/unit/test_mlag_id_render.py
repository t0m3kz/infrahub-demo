"""The multi-chassis id of a host-facing port-channel is its lag_id, and the peer-link never gets one."""

import json
import re
from pathlib import Path

import pytest

CONFIGS = Path(__file__).resolve().parents[1] / "smoke" / "configs"
ROLES = ("leaf", "tor", "border_leaf")
RENDERED_ID = {
    "arista_eos": re.compile(r"^interface Port-Channel(\d+)\n(?:(?!interface ).*\n)*?   mlag (\d+)$", re.MULTILINE),
    "cisco_nxos": re.compile(r"^interface port-channel(\d+)\n(?:(?!interface ).*\n)*?  vpc (\d+)$", re.MULTILINE),
}


def _interfaces(fixture: str) -> list[dict]:
    data = json.loads((CONFIGS / fixture / "input.json").read_text())
    device = data["DcimDevice"]["edges"][0]["node"]
    return [edge["node"] for edge in device["interfaces"]["edges"]]


def _host_lag(fixture: str) -> dict:
    (lag,) = (i for i in _interfaces(fixture) if i["__typename"] == "DcimLAGInterface" and i["role"]["value"] == "lag")
    return lag


@pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos"])
@pytest.mark.parametrize("role", ROLES)
def test_rendered_mlag_id_is_the_lag_id_and_peer_link_has_none(role: str, platform: str) -> None:
    """`mlag <id>` / `vpc <id>` sits on the host port-channel with its lag_id; the peer-link carries none."""
    fixture = f"{role}_{platform}_mlag"
    output = (CONFIGS / fixture / "output.txt").read_text()
    lag_id = _host_lag(fixture)["lag_id"]["value"]

    assert [(int(n), int(i)) for n, i in RENDERED_ID[platform].findall(output)] == [(lag_id, lag_id)]
    peer_link = next(i for i in _interfaces(fixture) if i["role"]["value"] == "mlag-peer" and "lag_id" in i)
    assert f"{peer_link['name']['value']}\n" in output
    assert len(re.findall(r"^\s+(?:mlag|vpc) \d+$", output, re.MULTILINE)) == 1


@pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
@pytest.mark.parametrize("role", ROLES)
def test_sonic_mclag_interface_is_the_host_port_channel_only(role: str, platform: str) -> None:
    """MCLAG_INTERFACE lists the host port-channel (with its member), never the peer-link."""
    fixture = f"{role}_{platform}_mlag"
    output = json.loads((CONFIGS / fixture / "output.txt").read_text())
    host = _host_lag(fixture)["name"]["value"]

    assert list(output["MCLAG_INTERFACE"]) == [f"1|{host}"]
    assert f"{host}|Ethernet10" in output["PORTCHANNEL_MEMBER"]
    assert "PortChannel100" in output["PORTCHANNEL"]


def test_get_interfaces_sets_mlag_id_only_on_domain_port_channels() -> None:
    """mlag_id = lag_id for a port-channel in a domain; none for the peer-link or a plain port-channel."""
    from transforms.helpers.vxlan import get_interfaces

    def lag(name: str, lag_id: int, role: str, domain: dict | None) -> dict:
        return {
            "name": name,
            "typename": "DcimLAGInterface",
            "role": role,
            "lag_id": lag_id,
            "mlag_domain": domain,
            "member_interfaces": [],
            "segments": [],
        }

    domain = {"id": "dom-1"}
    result = {
        i["name"]: i
        for i in get_interfaces(
            [
                lag("Port-Channel10", 10, "lag", domain),
                lag("Port-Channel100", 100, "mlag-peer", domain),
                lag("Port-Channel20", 20, "lag", None),
            ]
        )
    }

    assert result["Port-Channel10"]["mlag_id"] == 10
    assert "mlag_id" not in result["Port-Channel100"]
    assert "mlag_id" not in result["Port-Channel20"]


@pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos"])
@pytest.mark.parametrize("role", ROLES)
def test_channel_group_ids_are_the_lag_ids_of_the_members(role: str, platform: str) -> None:
    """Each member port binds to its LAG's lag_id; the peer-link members keep their one channel-group each."""
    fixture = f"{role}_{platform}_mlag"
    output = (CONFIGS / fixture / "output.txt").read_text()
    interfaces = _interfaces(fixture)
    expected = {
        member["name"]["value"]: lag["lag_id"]["value"]
        for lag in interfaces
        if lag["__typename"] == "DcimLAGInterface"
        for member in lag["member_interfaces"]["edges"]
        for member in [member["node"] if "node" in member else member]
    }
    bound = re.findall(
        r"^interface (\S+)\n(?:(?!interface ).*\n)*?\s+channel-group (\d+) mode (\w+)$", output, re.MULTILINE
    )

    assert {name: int(group) for name, group, _ in bound} == expected
    assert len(bound) == len(expected) == 3
    assert {mode for _, _, mode in bound} == {"active"}


def test_get_interfaces_binds_members_with_the_lag_mode() -> None:
    """A member gets its LAG's id and keyword (static -> on); a LAG without members binds nothing."""
    from transforms.helpers.vxlan import get_interfaces

    def lag(name: str, lag_id: int, lacp_mode: str, members: list[str]) -> dict:
        return {
            "name": name,
            "typename": "DcimLAGInterface",
            "role": "lag",
            "lag_id": lag_id,
            "lacp_mode": lacp_mode,
            "member_interfaces": [{"name": m} for m in members],
            "segments": [],
        }

    def port(name: str) -> dict:
        return {"name": name, "typename": "DcimPhysicalInterface", "role": "customer", "segments": []}

    result = {
        i["name"]: i
        for i in get_interfaces(
            [
                lag("Port-Channel1", 1, "static", ["Ethernet1"]),
                lag("Port-Channel2", 2, "passive", ["Ethernet2"]),
                lag("Port-Channel3", 3, "active", []),
                port("Ethernet1"),
                port("Ethernet2"),
                port("Ethernet3"),
            ]
        )
    }

    assert (result["Ethernet1"]["channel_group"], result["Ethernet1"]["channel_group_mode"]) == (1, "on")
    assert (result["Ethernet2"]["channel_group"], result["Ethernet2"]["channel_group_mode"]) == (2, "passive")
    assert "channel_group" not in result["Ethernet3"]
    assert "channel_group" not in result["Port-Channel3"]


def test_a_static_lag_renders_on_never_static_in_every_channel_group_line() -> None:
    """EOS and NX-OS take `on | active | passive`, not `static`: the LAG, its members and the peer-link carry the
    mapped keyword, and the templates render only that."""
    from transforms.helpers.mlag import CHANNEL_GROUP_MODE, get_mlag
    from transforms.helpers.vxlan import get_interfaces

    assert CHANNEL_GROUP_MODE == {"active": "active", "passive": "passive", "static": "on"}
    lag = {
        "name": "Port-Channel1",
        "typename": "DcimLAGInterface",
        "role": "lag",
        "lag_id": 1,
        "lacp_mode": "static",
        "member_interfaces": [{"name": "Ethernet1"}],
        "segments": [],
    }
    port = {"name": "Ethernet1", "typename": "DcimPhysicalInterface", "role": "customer", "segments": []}
    result = {i["name"]: i for i in get_interfaces([lag, port])}
    assert result["Port-Channel1"]["channel_group_mode"] == "on"
    assert result["Ethernet1"]["channel_group_mode"] == "on"

    peer_link = {**lag, "name": "Port-Channel100", "role": "mlag-peer", "lag_id": 100, "member_interfaces": []}
    mlag = get_mlag(
        [
            {
                "typename": "ManagedMLAG",
                "id": "m1",
                "name": "pair",
                "domain_id": 1,
                "devices": [{"name": "a"}, {"name": "b"}],
            }
        ],
        [peer_link],
        "a",
    )
    assert mlag is not None
    assert mlag["peer_link_lacp_mode"] == "static"
    assert mlag["peer_link_channel_group_mode"] == "on"
