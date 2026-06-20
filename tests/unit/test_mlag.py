"""Unit tests for MLAG configuration helpers.

Covers:
- get_mlag() — domain extraction, peer-link detection, missing-capability path
- get_capabilities() MLAG flag — ManagedMLAG typename toggles mlag_enabled
- Integration with device data shapes used by BaseDeviceTransform
"""

from __future__ import annotations

from transforms.helpers.mlag import get_mlag

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mlag_cap(
    *,
    name: str = "DC1-POD1-L1-L2-MLAG",
    domain_id: int = 1,
    reload_delay: int = 300,
    reload_delay_non_mlag: int = 330,
    devices: list[str] | None = None,
) -> dict:
    return {
        "typename": "ManagedMLAG",
        "name": name,
        "domain_id": domain_id,
        "reload_delay": reload_delay,
        "reload_delay_non_mlag": reload_delay_non_mlag,
        "devices": [{"name": d} for d in (devices or ["DC1-POD1-L1", "DC1-POD1-L2"])],
    }


def _peer_link_iface(name: str = "Port-Channel100") -> dict:
    return {"name": name, "role": "mlag-peer"}


def _uplink_iface(name: str = "Ethernet1/1") -> dict:
    return {"name": name, "role": "uplink"}


# ---------------------------------------------------------------------------
# get_mlag — core scenarios
# ---------------------------------------------------------------------------


class TestGetMlagNoDomain:
    def test_empty_capabilities_returns_none(self) -> None:
        assert get_mlag([]) is None

    def test_none_capabilities_returns_none(self) -> None:
        assert get_mlag(None) is None

    def test_no_mlag_typename_returns_none(self) -> None:
        caps = [{"typename": "ManagedBGP"}, {"typename": "ManagedOSPF"}]
        assert get_mlag(caps) is None


class TestGetMlagDomainExtraction:
    def test_returns_domain_name(self) -> None:
        result = get_mlag([_mlag_cap(name="DC1-POD1-L1-L2-MLAG")])
        assert result is not None
        assert result["name"] == "DC1-POD1-L1-L2-MLAG"

    def test_returns_domain_id(self) -> None:
        result = get_mlag([_mlag_cap(domain_id=42)])
        assert result is not None
        assert result["domain_id"] == 42

    def test_returns_reload_delays(self) -> None:
        result = get_mlag([_mlag_cap(reload_delay=200, reload_delay_non_mlag=250)])
        assert result is not None
        assert result["reload_delay"] == 200
        assert result["reload_delay_non_mlag"] == 250

    def test_default_reload_delays_when_missing(self) -> None:
        cap = {"typename": "ManagedMLAG", "name": "TEST-MLAG", "domain_id": 1}
        result = get_mlag([cap])
        assert result is not None
        assert result["reload_delay"] == 300
        assert result["reload_delay_non_mlag"] == 330

    def test_returns_device_names(self) -> None:
        result = get_mlag([_mlag_cap(devices=["DC1-POD1-L1", "DC1-POD1-L2"])])
        assert result is not None
        assert result["devices"] == ["DC1-POD1-L1", "DC1-POD1-L2"]

    def test_empty_devices_list(self) -> None:
        cap = {"typename": "ManagedMLAG", "name": "X", "domain_id": 1, "devices": []}
        result = get_mlag([cap])
        assert result is not None
        assert result["devices"] == []


class TestGetMlagPeerLink:
    def test_peer_link_found_by_role(self) -> None:
        ifaces = [_uplink_iface("Ethernet1/1"), _peer_link_iface("Port-Channel100")]
        result = get_mlag([_mlag_cap()], ifaces)
        assert result is not None
        assert result["peer_link"] == "Port-Channel100"

    def test_no_mlag_peer_role_returns_none_peer_link(self) -> None:
        ifaces = [_uplink_iface("Ethernet1/1"), _uplink_iface("Ethernet1/2")]
        result = get_mlag([_mlag_cap()], ifaces)
        assert result is not None
        assert result["peer_link"] is None

    def test_peer_link_none_when_no_interfaces_passed(self) -> None:
        result = get_mlag([_mlag_cap()])
        assert result is not None
        assert result["peer_link"] is None

    def test_peer_link_none_when_empty_interfaces(self) -> None:
        result = get_mlag([_mlag_cap()], [])
        assert result is not None
        assert result["peer_link"] is None

    def test_first_peer_link_wins_when_multiple(self) -> None:
        ifaces = [
            _peer_link_iface("Port-Channel100"),
            _peer_link_iface("Port-Channel200"),
        ]
        result = get_mlag([_mlag_cap()], ifaces)
        assert result is not None
        assert result["peer_link"] == "Port-Channel100"

    def test_mlag_peer_ethernets_also_detected(self) -> None:
        """Ethernet member interfaces with mlag-peer role also qualify."""
        ifaces = [
            {"name": "Ethernet1/33", "role": "mlag-peer"},
            {"name": "Ethernet1/34", "role": "mlag-peer"},
        ]
        result = get_mlag([_mlag_cap()], ifaces)
        assert result is not None
        assert result["peer_link"] == "Ethernet1/33"


class TestGetMlagFirstCapabilityWins:
    def test_first_mlag_capability_returned_when_multiple(self) -> None:
        caps = [
            _mlag_cap(name="FIRST-MLAG", domain_id=1),
            _mlag_cap(name="SECOND-MLAG", domain_id=2),
        ]
        result = get_mlag(caps)
        assert result is not None
        assert result["name"] == "FIRST-MLAG"
        assert result["domain_id"] == 1

    def test_non_mlag_caps_before_mlag_are_skipped(self) -> None:
        caps = [
            {"typename": "ManagedBGP"},
            _mlag_cap(name="MY-MLAG", domain_id=5),
        ]
        result = get_mlag(caps)
        assert result is not None
        assert result["name"] == "MY-MLAG"


# ---------------------------------------------------------------------------
# Real pod topology: DC1-POD1 L1↔L2 MLAG pair
# ---------------------------------------------------------------------------


class TestDC1Pod1MLAGScenario:
    """Reflects the actual DC1-POD1-L1-L2-MLAG domain created in 06_lag_mlag.yml."""

    def _l1_caps(self) -> list[dict]:
        return [
            {"typename": "ManagedBGP"},
            _mlag_cap(
                name="DC1-POD1-L1-L2-MLAG",
                domain_id=1,
                devices=["DC1-POD1-L1", "DC1-POD1-L2"],
            ),
        ]

    def _l1_interfaces(self) -> list[dict]:
        return [
            {"name": "Ethernet1/1", "role": "uplink"},
            {"name": "Ethernet1/2", "role": "uplink"},
            {"name": "Ethernet1/33", "role": "mlag-peer"},
            {"name": "Ethernet1/34", "role": "mlag-peer"},
            {"name": "Port-Channel100", "role": "mlag-peer"},
        ]

    def test_mlag_domain_extracted(self) -> None:
        result = get_mlag(self._l1_caps(), self._l1_interfaces())
        assert result is not None
        assert result["name"] == "DC1-POD1-L1-L2-MLAG"
        assert result["domain_id"] == 1

    def test_peer_link_is_port_channel100(self) -> None:
        result = get_mlag(self._l1_caps(), self._l1_interfaces())
        assert result is not None
        assert result["peer_link"] == "Ethernet1/33"

    def test_peer_devices_include_both_leaves(self) -> None:
        result = get_mlag(self._l1_caps(), self._l1_interfaces())
        assert result is not None
        assert "DC1-POD1-L1" in result["devices"]
        assert "DC1-POD1-L2" in result["devices"]

    def test_bgp_cap_before_mlag_does_not_block_extraction(self) -> None:
        result = get_mlag(self._l1_caps(), self._l1_interfaces())
        assert result is not None

    def test_l2_same_domain_same_result(self) -> None:
        """Both peers share the same ManagedMLAG node — result is identical."""
        l2_caps = [
            {"typename": "ManagedBGP"},
            _mlag_cap(
                name="DC1-POD1-L1-L2-MLAG",
                domain_id=1,
                devices=["DC1-POD1-L1", "DC1-POD1-L2"],
            ),
        ]
        result_l1 = get_mlag(self._l1_caps(), self._l1_interfaces())
        result_l2 = get_mlag(l2_caps, self._l1_interfaces())
        assert result_l1 == result_l2


class TestDC1Pod2MLAGScenario:
    """Reflects DC1-POD2-L1-L2-MLAG domain (domain_id=2)."""

    def test_domain_id_2_extracted(self) -> None:
        caps = [_mlag_cap(name="DC1-POD2-L1-L2-MLAG", domain_id=2, devices=["DC1-POD2-L1", "DC1-POD2-L2"])]
        result = get_mlag(caps, [_peer_link_iface("Port-Channel100")])
        assert result is not None
        assert result["domain_id"] == 2
        assert result["name"] == "DC1-POD2-L1-L2-MLAG"

    def test_peer_link_port_channel100(self) -> None:
        caps = [_mlag_cap(name="DC1-POD2-L1-L2-MLAG", domain_id=2)]
        result = get_mlag(caps, [_peer_link_iface("Port-Channel100")])
        assert result is not None
        assert result["peer_link"] == "Port-Channel100"


# ---------------------------------------------------------------------------
# get_capabilities MLAG flag
# ---------------------------------------------------------------------------


class TestGetCapabilitiesMLAGFlag:
    def test_mlag_enabled_when_managed_mlag_present(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "device_capabilities": [
                    {"typename": "ManagedMLAG"},
                ]
            }
        )
        assert result["mlag_enabled"] is True

    def test_mlag_disabled_without_managed_mlag(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "device_capabilities": [
                    {"typename": "ManagedBGP"},
                ]
            }
        )
        assert result["mlag_enabled"] is False

    def test_mlag_enabled_alongside_bgp(self) -> None:
        from transforms.common import get_capabilities

        result = get_capabilities(
            {
                "device_capabilities": [
                    {"typename": "ManagedBGP"},
                    {"typename": "ManagedMLAG"},
                ]
            }
        )
        assert result["mlag_enabled"] is True
        assert result["bgp_enabled"] is True
