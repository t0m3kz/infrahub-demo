"""Unit tests for HA (High Availability) configuration helpers.

Covers:
- get_ha() — domain extraction, HA link detection, missing-capability path
- get_capabilities() HA flag — ManagedFirewallHA/ManagedLoadbalancerHA typename toggles ha_enabled
- Integration with device data shapes used by BaseDeviceTransform
"""

from __future__ import annotations

from transforms.helpers.ha import HA_LINK_ADDRESSES, get_ha, inline_addresses

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
    def test_empty_capabilities_returns_none(self) -> None:
        assert get_ha([]) is None

    def test_none_capabilities_returns_none(self) -> None:
        assert get_ha(None) is None

    def test_no_managed_ha_typename_returns_none(self) -> None:
        caps = [{"typename": "ManagedBGP"}, {"typename": "ManagedOSPF"}]
        assert get_ha(caps) is None

    def test_managed_mlag_not_treated_as_ha(self) -> None:
        caps = [{"typename": "ManagedMLAG", "name": "some-mlag", "domain_id": 1}]
        assert get_ha(caps) is None


# ---------------------------------------------------------------------------
# get_ha — domain field extraction
# ---------------------------------------------------------------------------


class TestGetHaDomainExtraction:
    def test_returns_name(self) -> None:
        result = get_ha([_ha_cap(name="DC1-FW-HA")])
        assert result is not None
        assert result["name"] == "DC1-FW-HA"

    def test_returns_group_id(self) -> None:
        result = get_ha([_ha_cap(group_id=5)])
        assert result is not None
        assert result["group_id"] == 5

    def test_returns_mode(self) -> None:
        result = get_ha([_ha_cap(mode="active-active")])
        assert result is not None
        assert result["mode"] == "active-active"

    def test_returns_priority(self) -> None:
        result = get_ha([_ha_cap(priority=110)])
        assert result is not None
        assert result["priority"] == 110

    def test_returns_preempt_true(self) -> None:
        result = get_ha([_ha_cap(preempt=True)])
        assert result is not None
        assert result["preempt"] is True

    def test_returns_preempt_false(self) -> None:
        result = get_ha([_ha_cap(preempt=False)])
        assert result is not None
        assert result["preempt"] is False

    def test_returns_ha_timer(self) -> None:
        result = get_ha([_ha_cap(ha_timer="aggressive")])
        assert result is not None
        assert result["ha_timer"] == "aggressive"

    def test_returns_ha_protocol(self) -> None:
        result = get_ha([_ha_cap(ha_protocol="clusterxl")])
        assert result is not None
        assert result["ha_protocol"] == "clusterxl"

    def test_returns_device_names(self) -> None:
        result = get_ha([_ha_cap(devices=["dc1-fw-01", "dc1-fw-02"])])
        assert result is not None
        assert result["devices"] == ["dc1-fw-01", "dc1-fw-02"]

    def test_empty_capabilities_list_gives_empty_devices(self) -> None:
        cap = {"typename": "ManagedFirewallHA", "name": "X", "group_id": 1, "capabilities": []}
        result = get_ha([cap])
        assert result is not None
        assert result["devices"] == []


# ---------------------------------------------------------------------------
# get_ha — defaults when fields are missing
# ---------------------------------------------------------------------------


class TestGetHaDefaults:
    def _minimal_cap(self) -> dict:
        """ManagedFirewallHA cap with only the mandatory typename — all optional fields absent."""
        return {"typename": "ManagedFirewallHA", "name": "MIN-HA"}

    def test_default_priority_is_100(self) -> None:
        result = get_ha([self._minimal_cap()])
        assert result is not None
        assert result["priority"] == 100

    def test_default_preempt_is_false(self) -> None:
        result = get_ha([self._minimal_cap()])
        assert result is not None
        assert result["preempt"] is False

    def test_default_ha_timer_is_standard(self) -> None:
        result = get_ha([self._minimal_cap()])
        assert result is not None
        assert result["ha_timer"] == "standard"

    def test_default_mode_is_active_passive(self) -> None:
        result = get_ha([self._minimal_cap()])
        assert result is not None
        assert result["mode"] == "active-passive"

    def test_devices_none_capabilities_key_gives_empty_list(self) -> None:
        cap = {"typename": "ManagedFirewallHA", "name": "MIN-HA", "capabilities": None}
        result = get_ha([cap])
        assert result is not None
        assert result["devices"] == []


# ---------------------------------------------------------------------------
# get_ha — HA link detection
# ---------------------------------------------------------------------------


class TestGetHaLink:
    def test_ha_link_found_by_role(self) -> None:
        ifaces = [_uplink_iface("Ethernet1/1"), _ha_iface("Management0")]
        result = get_ha([_ha_cap()], ifaces)
        assert result is not None
        assert result["ha_link"] == "Management0"

    def test_no_ha_role_returns_none_ha_link(self) -> None:
        ifaces = [_uplink_iface("Ethernet1/1"), _uplink_iface("Ethernet1/2")]
        result = get_ha([_ha_cap()], ifaces)
        assert result is not None
        assert result["ha_link"] is None

    def test_ha_link_none_when_no_interfaces_passed(self) -> None:
        result = get_ha([_ha_cap()])
        assert result is not None
        assert result["ha_link"] is None

    def test_ha_link_none_when_empty_interfaces(self) -> None:
        result = get_ha([_ha_cap()], [])
        assert result is not None
        assert result["ha_link"] is None

    def test_first_ha_interface_wins_when_multiple(self) -> None:
        ifaces = [
            _ha_iface("Management0"),
            _ha_iface("Management1"),
        ]
        result = get_ha([_ha_cap()], ifaces)
        assert result is not None
        assert result["ha_link"] == "Management0"

    def test_ha_link_not_confused_with_mlag_peer_role(self) -> None:
        ifaces = [{"name": "Port-Channel100", "role": "mlag-peer"}]
        result = get_ha([_ha_cap()], ifaces)
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

    def test_non_ha_caps_before_ha_are_skipped(self) -> None:
        caps = [
            {"typename": "ManagedBGP"},
            {"typename": "ManagedMLAG"},
            _ha_cap(name="MY-HA", group_id=3),
        ]
        result = get_ha(caps)
        assert result is not None
        assert result["name"] == "MY-HA"

    def test_non_ha_caps_after_ha_are_ignored(self) -> None:
        caps = [
            _ha_cap(name="REAL-HA", group_id=1),
            {"typename": "ManagedBGP"},
        ]
        result = get_ha(caps)
        assert result is not None
        assert result["name"] == "REAL-HA"


# ---------------------------------------------------------------------------
# get_ha — devices come from cap["capabilities"] (not cap["devices"])
# ---------------------------------------------------------------------------


class TestGetHaDevices:
    def test_devices_sourced_from_capabilities_key(self) -> None:
        """ManagedFirewallHA stores peer devices under 'capabilities', not 'devices'."""
        cap = {
            "typename": "ManagedFirewallHA",
            "name": "DC1-FW-HA",
            "group_id": 1,
            "capabilities": [{"name": "dc1-fw-01"}, {"name": "dc1-fw-02"}],
        }
        result = get_ha([cap])
        assert result is not None
        assert result["devices"] == ["dc1-fw-01", "dc1-fw-02"]

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
        assert "wrong-fw-01" not in result["devices"]
        assert result["devices"] == ["dc1-fw-01", "dc1-fw-02"]

    def test_single_device_in_capabilities(self) -> None:
        result = get_ha([_ha_cap(devices=["dc1-fw-01"])])
        assert result is not None
        assert result["devices"] == ["dc1-fw-01"]

    def test_three_devices_in_capabilities(self) -> None:
        result = get_ha([_ha_cap(devices=["fw-01", "fw-02", "fw-03"])])
        assert result is not None
        assert result["devices"] == ["fw-01", "fw-02", "fw-03"]


# ---------------------------------------------------------------------------
# get_ha — ManagedLoadbalancerHA is also matched
# ---------------------------------------------------------------------------


class TestGetHaLBHA:
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
# Real firewall HA scenario
# ---------------------------------------------------------------------------


class TestDC1FirewallHAScenario:
    """Reflects a typical DC1 active-passive firewall HA pair."""

    def _fw_caps(self) -> list[dict]:
        return [
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

    def _fw_interfaces(self) -> list[dict]:
        return [
            {"name": "Ethernet1/1", "role": "uplink"},
            {"name": "Ethernet1/2", "role": "uplink"},
            {"name": "Management0", "role": "ha"},
        ]

    def test_ha_domain_extracted(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert result["name"] == "DC1-FW-HA"
        assert result["group_id"] == 1

    def test_ha_mode_extracted(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert result["mode"] == "active-passive"

    def test_ha_priority_extracted(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert result["priority"] == 110

    def test_ha_preempt_extracted(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert result["preempt"] is True

    def test_ha_protocol_extracted(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert result["ha_protocol"] == "clusterxl"

    def test_ha_link_is_management0(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert result["ha_link"] == "Management0"

    def test_both_peer_devices_present(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None
        assert "dc1-fw-01" in result["devices"]
        assert "dc1-fw-02" in result["devices"]

    def test_bgp_cap_before_ha_does_not_block_extraction(self) -> None:
        result = get_ha(self._fw_caps(), self._fw_interfaces())
        assert result is not None

    def test_both_peers_share_same_ha_node_same_result(self) -> None:
        """Both HA peers reference the same ManagedFirewallHA node — result is identical."""
        fw2_caps = [
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
        result_fw1 = get_ha(self._fw_caps(), self._fw_interfaces())
        result_fw2 = get_ha(fw2_caps, self._fw_interfaces())
        assert result_fw1 == result_fw2


# ---------------------------------------------------------------------------
# get_capabilities HA flag
# ---------------------------------------------------------------------------


class TestGetCapabilitiesHAFlag:
    def test_ha_enabled_when_firewall_ha_present(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedFirewallHA"},
                ]
            }
        )
        assert result["ha_enabled"] is True

    def test_ha_enabled_when_lb_ha_present(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedLoadbalancerHA"},
                ]
            }
        )
        assert result["ha_enabled"] is True

    def test_ha_disabled_without_managed_ha(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedBGP"},
                ]
            }
        )
        assert result["ha_enabled"] is False

    def test_ha_enabled_alongside_bgp(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedBGP"},
                    {"typename": "ManagedFirewallHA"},
                ]
            }
        )
        assert result["ha_enabled"] is True
        assert result["bgp_enabled"] is True

    def test_ha_enabled_does_not_affect_mlag_flag(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedFirewallHA"},
                ]
            }
        )
        assert result["ha_enabled"] is True
        assert result["mlag_enabled"] is False

    def test_ha_disabled_with_empty_capabilities(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities({"capabilities": []})
        assert result["ha_enabled"] is False

    def test_ha_disabled_with_managed_mlag_only(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedMLAG"},
                ]
            }
        )
        assert result["ha_enabled"] is False

    def test_ha_enabled_when_proxy_ha_present(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedProxyHA"},
                ]
            }
        )
        assert result["ha_enabled"] is True

    def test_ha_enabled_when_cloud_proxy_ha_present(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedCloudProxy"},
                ]
            }
        )
        assert result["ha_enabled"] is True


# ---------------------------------------------------------------------------
# get_ha — ManagedProxyHA and ManagedCloudProxy are matched
# ---------------------------------------------------------------------------


class TestGetHaProxyHA:
    def test_managed_proxy_ha_typename_is_matched(self) -> None:
        cap = {
            "typename": "ManagedProxyHA",
            "name": "DC3-PRX-HA",
            "group_id": 3,
            "mode": "active-passive",
            "capabilities": [{"name": "dc3-prx-01"}, {"name": "dc3-prx-02"}],
        }
        result = get_ha([cap])
        assert result is not None

    def test_managed_cloud_proxy_ha_typename_is_matched(self) -> None:
        cap = {
            "typename": "ManagedCloudProxy",
            "name": "DC3-CPRX-HA",
            "group_id": 5,
            "mode": "active-passive",
            "capabilities": [{"name": "dc3-cprx-01"}, {"name": "dc3-cprx-02"}],
        }
        result = get_ha([cap])
        assert result is not None

    def test_devices_extracted_from_capabilities_key_for_proxy_ha(self) -> None:
        cap = {
            "typename": "ManagedProxyHA",
            "name": "DC3-PRX-HA",
            "group_id": 3,
            "capabilities": [{"name": "dc3-prx-01"}, {"name": "dc3-prx-02"}],
        }
        result = get_ha([cap])
        assert result is not None
        assert result["devices"] == ["dc3-prx-01", "dc3-prx-02"]

    def test_proxy_type_is_not_in_get_ha_return_value(self) -> None:
        """get_ha() returns only generic HA fields; proxy_type is proxy-specific."""
        cap = {
            "typename": "ManagedProxyHA",
            "name": "DC3-PRX-HA",
            "group_id": 3,
            "proxy_type": "explicit",
            "proxy_vendor": "haproxy",
            "capabilities": [{"name": "dc3-prx-01"}, {"name": "dc3-prx-02"}],
        }
        result = get_ha([cap])
        assert result is not None
        assert "proxy_type" not in result

    def test_proxy_ha_name_extracted(self) -> None:
        cap = {
            "typename": "ManagedProxyHA",
            "name": "DC3-PRX-HA",
            "group_id": 3,
            "capabilities": [],
        }
        result = get_ha([cap])
        assert result is not None
        assert result["name"] == "DC3-PRX-HA"


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


class TestInlineAddresses:
    """The pair's virtual IP and the secondary's own address on an inline-terminated segment."""

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

    @staticmethod
    def _segment(**overrides: object) -> dict:
        segment = {
            "terminate_inline": True,
            "inline_service": {"name": "fw-ha"},
            "gateway": {"address": "10.1.0.1/24"},
            "interface_capabilities": [
                {"device": {"name": "fw-01"}, "ip_address": {"address": "10.1.0.2/24"}},
                {"device": {"name": "fw-02"}, "ip_address": {"address": "10.1.0.3/24"}},
            ],
        }
        segment.update(overrides)
        return segment

    def test_virtual_ip_is_gateway_and_standby_is_secondary(self) -> None:
        """Gateway becomes the virtual IP; the secondary member's address is the standby."""
        assert inline_addresses([self._segment()], self._ha()) == {
            "virtual_ip": "10.1.0.1/24",
            "standby_ip": "10.1.0.3/24",
        }

    def test_segment_not_terminated_by_this_pair_has_none(self) -> None:
        """Another pair's segment, or one not terminated inline, gives no addresses."""
        none = {"virtual_ip": None, "standby_ip": None}
        assert inline_addresses([self._segment(inline_service={"name": "lb-ha"})], self._ha()) == none
        assert inline_addresses([self._segment(terminate_inline=False)], self._ha()) == none
        assert inline_addresses([self._segment()], None) == none
