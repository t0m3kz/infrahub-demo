"""Unit tests for HA (High Availability) configuration helpers.

Covers:
- get_ha() — domain extraction, HA link detection, missing-capability path
- Integration with device data shapes used by BaseDeviceTransform
"""

from __future__ import annotations

from typing import Any

import pytest

from transforms.helpers.ha import HA_LINK_ADDRESSES, get_ha, leg_virtual_ip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ha_cap(
    *,
    name: str = "DC1-FW-HA",
    group_id: int = 1,
    mode: str = "active-passive",
    priority: int = 100,
    preempt: bool = False,
    ha_timer: str = "standard",
    ha_protocol: str = "vrrp",
    devices: list[str] | None = None,
) -> dict:
    return {
        "typename": "ManagedFirewallHA",
        "name": name,
        "group_id": group_id,
        "mode": mode,
        "priority": priority,
        "preempt": preempt,
        "ha_timer": ha_timer,
        "ha_protocol": ha_protocol,
        "capabilities": [{"name": d} for d in (devices or ["dc1-fw-01", "dc1-fw-02"])],
    }


def _ha_iface(name: str = "Management0") -> dict:
    return {"name": name, "role": "ha"}


def _uplink_iface(name: str = "Ethernet1/1") -> dict:
    return {"name": name, "role": "uplink"}


# ---------------------------------------------------------------------------
# get_ha — empty / missing capability
# ---------------------------------------------------------------------------


class TestGetHaNoDomain:
    @pytest.mark.parametrize(
        "caps",
        [
            pytest.param([], id="empty"),
            pytest.param(None, id="none"),
            pytest.param([{"typename": "ManagedBGP"}, {"typename": "ManagedOSPF"}], id="no-ha-typename"),
            pytest.param([{"typename": "ManagedMLAG", "name": "some-mlag", "domain_id": 1}], id="mlag-is-not-ha"),
        ],
    )
    def test_no_ha_capability_returns_none(self, caps: list[dict] | None) -> None:
        assert get_ha(caps) is None


# ---------------------------------------------------------------------------
# get_ha — domain field extraction
# ---------------------------------------------------------------------------


class TestGetHaDomainExtraction:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "DC1-FW-HA"),
            ("group_id", 5),
            ("mode", "active-active"),
            ("priority", 110),
            ("preempt", True),
            ("preempt", False),
            ("ha_timer", "aggressive"),
            ("ha_protocol", "clusterxl"),
        ],
    )
    def test_returns_the_field(self, field: str, value: Any) -> None:
        result = get_ha([_ha_cap(**{field: value})])
        assert result is not None
        assert result[field] == value
        assert type(result[field]) is type(value)

    def test_peer_management_ip_is_the_other_members_primary_address(self) -> None:
        members = [
            {"name": "lb-01", "primary_address": {"address": "192.168.2.1/32"}},
            {"name": "lb-02", "primary_address": {"address": "192.168.2.2/32"}},
        ]
        cap = {"typename": "ManagedLoadbalancerHA", "name": "lb-ha", "capabilities": members}
        for device, peer_ip in (("lb-01", "192.168.2.2"), ("lb-02", "192.168.2.1")):
            result = get_ha([cap], device_name=device)
            assert result is not None
            assert result["peer_management_ip"] == peer_ip

    def test_peer_management_ip_is_none_without_an_address_or_membership(self) -> None:
        cap = {"typename": "ManagedLoadbalancerHA", "name": "lb-ha", "capabilities": [{"name": "a"}, {"name": "b"}]}
        for device in ("a", "other"):
            result = get_ha([cap], device_name=device)
            assert result is not None
            assert result["peer_management_ip"] is None

    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param(["dc1-fw-01", "dc1-fw-02"], id="two"),
            pytest.param(["dc1-fw-01"], id="one"),
            pytest.param(["fw-01", "fw-02", "fw-03"], id="three"),
        ],
    )
    def test_returns_device_names(self, devices: list[str]) -> None:
        result = get_ha([_ha_cap(devices=devices)])
        assert result is not None
        assert result["devices"] == devices

    @pytest.mark.parametrize("members", [pytest.param([], id="empty"), pytest.param(None, id="none")])
    def test_no_members_gives_empty_devices(self, members: list[dict] | None) -> None:
        cap = {"typename": "ManagedFirewallHA", "name": "X", "group_id": 1, "capabilities": members}
        result = get_ha([cap])
        assert result is not None
        assert result["devices"] == []

    def test_devices_key_in_cap_is_not_used(self) -> None:
        """A 'devices' key on the cap (MLAG-style) must NOT be picked up."""
        cap = {
            "typename": "ManagedFirewallHA",
            "name": "DC1-FW-HA",
            "group_id": 1,
            "devices": [{"name": "wrong-fw-01"}],
            "capabilities": [{"name": "dc1-fw-01"}, {"name": "dc1-fw-02"}],
        }
        result = get_ha([cap])
        assert result is not None
        assert result["devices"] == ["dc1-fw-01", "dc1-fw-02"]

    def test_lb_ha_typename_is_matched(self) -> None:
        cap = {
            "typename": "ManagedLoadbalancerHA",
            "name": "DC3-LB-HA",
            "group_id": 10,
            "mode": "active-passive",
            "capabilities": [{"name": "dc3-lb-01"}, {"name": "dc3-lb-02"}],
        }
        result = get_ha([cap])
        assert result is not None
        assert result["name"] == "DC3-LB-HA"
        assert result["devices"] == ["dc3-lb-01", "dc3-lb-02"]


# ---------------------------------------------------------------------------
# get_ha — defaults when fields are missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "default"),
    [("priority", 100), ("preempt", False), ("ha_timer", "standard"), ("mode", "active-passive")],
)
def test_a_missing_field_takes_its_default(field: str, default: object) -> None:
    """A ManagedFirewallHA cap with only typename and name: every optional field absent."""
    result = get_ha([{"typename": "ManagedFirewallHA", "name": "MIN-HA"}])
    assert result is not None
    assert result[field] == default
    assert type(result[field]) is type(default)


# ---------------------------------------------------------------------------
# get_ha — HA link detection
# ---------------------------------------------------------------------------


class TestGetHaLink:
    @pytest.mark.parametrize(
        ("interfaces", "expected"),
        [
            pytest.param([_uplink_iface("Ethernet1/1"), _ha_iface("Management0")], "Management0", id="by-role"),
            pytest.param([_uplink_iface("Ethernet1/1"), _uplink_iface("Ethernet1/2")], None, id="no-ha-role"),
            pytest.param([], None, id="empty"),
            pytest.param([_ha_iface("Management0"), _ha_iface("Management1")], "Management0", id="first-wins"),
            pytest.param([{"name": "Port-Channel100", "role": "mlag-peer"}], None, id="mlag-peer-is-not-ha"),
        ],
    )
    def test_ha_link(self, interfaces: list[dict], expected: str | None) -> None:
        result = get_ha([_ha_cap()], interfaces)
        assert result is not None
        assert result["ha_link"] == expected

    def test_ha_link_none_when_no_interfaces_passed(self) -> None:
        result = get_ha([_ha_cap()])
        assert result is not None
        assert result["ha_link"] is None


# ---------------------------------------------------------------------------
# get_ha — first HA capability wins
# ---------------------------------------------------------------------------


class TestGetHaFirstCapabilityWins:
    def test_first_ha_cap_returned_when_multiple(self) -> None:
        caps = [
            _ha_cap(name="FIRST-HA", group_id=1),
            _ha_cap(name="SECOND-HA", group_id=2),
        ]
        result = get_ha(caps)
        assert result is not None
        assert result["name"] == "FIRST-HA"
        assert result["group_id"] == 1

    @pytest.mark.parametrize(
        "caps",
        [
            pytest.param([{"typename": "ManagedBGP"}, {"typename": "ManagedMLAG"}, _ha_cap(name="MY-HA")], id="before"),
            pytest.param([_ha_cap(name="MY-HA"), {"typename": "ManagedBGP"}], id="after"),
        ],
    )
    def test_non_ha_caps_around_the_ha_are_skipped(self, caps: list[dict]) -> None:
        result = get_ha(caps)
        assert result is not None
        assert result["name"] == "MY-HA"


# ---------------------------------------------------------------------------
# Real firewall HA scenario
# ---------------------------------------------------------------------------


def test_dc1_firewall_ha_scenario() -> None:
    """A typical DC1 active-passive firewall HA pair, next to a BGP capability, with uplinks and an HA port."""
    caps = [
        {"typename": "ManagedBGP"},
        _ha_cap(
            name="DC1-FW-HA",
            group_id=1,
            mode="active-passive",
            priority=110,
            preempt=True,
            ha_timer="standard",
            ha_protocol="clusterxl",
            devices=["dc1-fw-01", "dc1-fw-02"],
        ),
    ]
    interfaces = [
        {"name": "Ethernet1/1", "role": "uplink"},
        {"name": "Ethernet1/2", "role": "uplink"},
        {"name": "Management0", "role": "ha"},
    ]
    result = get_ha(caps, interfaces)
    assert result is not None
    assert result["name"] == "DC1-FW-HA"
    assert result["group_id"] == 1
    assert result["mode"] == "active-passive"
    assert result["priority"] == 110
    assert result["preempt"] is True
    assert result["ha_protocol"] == "clusterxl"
    assert result["ha_link"] == "Management0"
    assert "dc1-fw-01" in result["devices"]
    assert "dc1-fw-02" in result["devices"]


def _ha_of(capabilities: list[dict], interfaces: list[dict], device_name: str) -> dict:
    """get_ha() for a device that has an HA domain."""
    ha = get_ha(capabilities, interfaces, device_name=device_name)
    assert ha is not None
    return ha


class TestHaUnit:
    """get_ha's unit and HA-link addresses follow the members sorted by name."""

    @staticmethod
    def _cap() -> dict:
        return {
            "typename": "ManagedFirewallHA",
            "name": "fw-ha",
            "capabilities": [{"name": "fw-02"}, {"name": "fw-01"}],
        }

    def test_first_member_by_name_is_primary(self) -> None:
        """The lowest name is primary regardless of query order."""
        assert _ha_of([self._cap()], [], "fw-01")["unit"] == "primary"
        assert _ha_of([self._cap()], [], "fw-02")["unit"] == "secondary"
        assert _ha_of([self._cap()], [], "other")["unit"] is None

    def test_link_addresses_only_with_an_ha_link(self) -> None:
        """The fixed link-local /30 is offered only when the device has an HA sync port."""
        assert _ha_of([self._cap()], [], "fw-01")["link_addresses"] is None
        ha = _ha_of([self._cap()], [{"name": "eth7", "role": "ha"}], "fw-01")
        assert ha["link_addresses"] == HA_LINK_ADDRESSES


class TestLegVirtualIp:
    """The pair's virtual IP on an exchange transit leg."""

    @staticmethod
    def _ha() -> dict:
        return _ha_of(
            [
                {
                    "typename": "ManagedFirewallHA",
                    "name": "fw-ha",
                    "capabilities": [{"name": "fw-01"}, {"name": "fw-02"}],
                }
            ],
            [],
            "fw-01",
        )

    def test_transit_leg_virtual_ip_is_a_fixed_offset_of_the_slash_29(self) -> None:
        """Member A's own .5 gives the .4 VIP; no capability lookup."""
        assert leg_virtual_ip(self._ha(), "100.66.0.5/29") == {"virtual_ip": "100.66.0.4/29"}

    def test_transit_leg_gives_the_same_pair_addresses_from_member_b(self) -> None:
        """Both members' config is identical: the VIP does not depend on which one renders."""
        assert leg_virtual_ip(self._ha(), "100.66.0.6/29") == leg_virtual_ip(self._ha(), "100.66.0.5/29")

    def test_transit_leg_without_ha_or_ipv4_has_none(self) -> None:
        """No HA pair, or an IPv6 leg, gives no virtual IP."""
        none = {"virtual_ip": None}
        assert leg_virtual_ip(None, "100.66.0.5/29") == none
        assert leg_virtual_ip(self._ha(), "fd00::5/127") == none
