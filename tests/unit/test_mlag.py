"""Unit tests for MLAG configuration helpers."""

from __future__ import annotations

from ipaddress import AddressValueError
from pathlib import Path

import jinja2
import pytest

from transforms.helpers.mlag import get_mlag, get_sonic_mlag_config

# Helpers


def _mlag_cap(
    *,
    name: str = "DC1-POD1-L1-L2-MLAG",
    domain_id: int = 1,
    reload_delay: int = 300,
    reload_delay_non_mlag: int = 330,
    devices: list[str] | None = None,
    interfaces: list[dict] | None = None,
) -> dict:
    return {
        "typename": "ManagedMLAG",
        "name": name,
        "domain_id": domain_id,
        "reload_delay": reload_delay,
        "reload_delay_non_mlag": reload_delay_non_mlag,
        "devices": [{"name": d} for d in (devices or ["DC1-POD1-L1", "DC1-POD1-L2"])],
        "interfaces": interfaces or [],
    }


def _on(device: str, *interfaces: dict) -> list[dict]:
    """Interfaces as the domain's ``interfaces`` edge returns them: each with the device it sits on."""
    return [{**interface, "device": {"name": device}} for interface in interfaces]


def _peer_link_iface(name: str = "Port-Channel100") -> dict:
    return {"name": name, "role": "mlag-peer"}


def _domain_with(*interfaces: dict, device: str = "DC1-POD1-L1") -> list[dict]:
    """A domain capability that lists ``interfaces`` on ``device``."""
    return [_mlag_cap(interfaces=_on(device, *interfaces))]


def _uplink_iface(name: str = "Ethernet1/1") -> dict:
    return {"name": name, "role": "uplink"}


# get_mlag — core scenarios


class TestGetMlagNoDomain:
    @pytest.mark.parametrize(
        "caps",
        [
            pytest.param([], id="empty"),
            pytest.param(None, id="none"),
            pytest.param([{"typename": "ManagedBGP"}, {"typename": "ManagedOSPF"}], id="no-mlag-typename"),
        ],
    )
    def test_no_mlag_capability_returns_none(self, caps: list[dict] | None) -> None:
        assert get_mlag(caps) is None


class TestGetMlagDomainExtraction:
    @pytest.mark.parametrize(
        ("cap", "expected"),
        [
            pytest.param(_mlag_cap(name="DC1-POD1-L1-L2-MLAG"), {"name": "DC1-POD1-L1-L2-MLAG"}, id="name"),
            pytest.param(_mlag_cap(domain_id=42), {"domain_id": 42}, id="domain-id"),
            pytest.param(
                _mlag_cap(reload_delay=200, reload_delay_non_mlag=250),
                {"reload_delay": 200, "reload_delay_non_mlag": 250},
                id="reload-delays",
            ),
            pytest.param(
                {"typename": "ManagedMLAG", "name": "TEST-MLAG", "domain_id": 1},
                {"reload_delay": 300, "reload_delay_non_mlag": 330},
                id="default-reload-delays",
            ),
            pytest.param(
                _mlag_cap(devices=["DC1-POD1-L1", "DC1-POD1-L2"]),
                {"devices": ["DC1-POD1-L1", "DC1-POD1-L2"]},
                id="device-names",
            ),
            pytest.param(
                {"typename": "ManagedMLAG", "name": "X", "domain_id": 1, "devices": []},
                {"devices": []},
                id="no-devices",
            ),
        ],
    )
    def test_returns_the_domain_fields(self, cap: dict, expected: dict) -> None:
        result = get_mlag([cap])
        assert result is not None
        assert {key: result[key] for key in expected} == expected

    def test_control_addresses_come_from_mlag_control_svi(self) -> None:
        """Control IPs come from the role=mlag-control SVI; management IPs stay separate."""
        cap = _mlag_cap()
        cap["devices"] = [
            {"name": "leaf-1", "role": "leaf", "primary_address": {"address": "192.0.2.1/32"}},
            {"name": "leaf-2", "role": "leaf", "primary_address": {"address": "192.0.2.2/32"}},
        ]
        cap["interfaces"] = [
            *_on("leaf-1", {"name": "Port-Channel100", "role": "mlag-peer"}),
            *_on("leaf-1", {"name": "Vlan4094", "role": "mlag-control", "ip_address": {"address": "fd00:2400::/127"}}),
            *_on("leaf-2", {"name": "Vlan4094", "role": "mlag-control", "ip_address": {"address": "fd00:2400::1/127"}}),
        ]
        result = get_mlag([cap], device_name="leaf-2")
        assert result is not None
        assert result["control_interface"] == "Vlan4094"
        assert result["local_address"] == "fd00:2400::1/127"
        assert result["local_ip"] == "fd00:2400::1"
        assert result["peer_ip"] == "fd00:2400::"
        assert result["local_management_ip"] == "192.0.2.2"
        assert result["peer_management_ip"] == "192.0.2.1"

    def test_virtual_peer_link_loopback_carries_control_address(self) -> None:
        """Without a control SVI, the addressed virtual peer-link loopback is the control interface."""
        cap = _mlag_cap()
        cap["devices"] = [{"name": f"leaf-{idx}", "role": "leaf"} for idx in (1, 2)]
        cap["interfaces"] = [
            interface
            for idx in (1, 2)
            for interface in _on(
                f"leaf-{idx}",
                {"name": "Loopback100", "role": "mlag-peer", "ip_address": {"address": f"fd00:2400::{idx}/128"}},
            )
        ]
        result = get_mlag([cap], device_name="leaf-1")
        assert result is not None
        assert result["control_interface"] == "Loopback100"
        assert result["local_ip"] == "fd00:2400::1"
        assert result["peer_ip"] == "fd00:2400::2"

    def test_l2_leaf_uses_control_svi_not_management(self) -> None:
        """l2-leafs get a control SVI like any other MLAG pair — no management-IP fallback."""
        cap = _mlag_cap()
        cap["capabilities"] = [
            {
                "name": f"l2-{idx}",
                "role": "l2-leaf",
                "primary_address": {"address": f"192.0.2.{idx}/32"},
            }
            for idx in (1, 2)
        ]
        cap["interfaces"] = [
            interface
            for idx in (1, 2)
            for interface in _on(
                f"l2-{idx}",
                {"name": "Vlan4094", "role": "mlag-control", "ip_address": {"address": f"10.254.0.{idx - 1}/31"}},
            )
        ]
        del cap["devices"]
        result = get_mlag([cap], device_name="l2-1")
        assert result is not None
        assert result["local_ip"] == "10.254.0.0"
        assert result["peer_ip"] == "10.254.0.1"
        assert result["peer_management_ip"] == "192.0.2.2"

    def test_no_control_address_for_vpc(self) -> None:
        """vPC pairs have no control interface; only management IPs are populated."""
        cap = _mlag_cap()
        cap["devices"] = [{"name": f"nx-{idx}", "primary_address": {"address": f"192.0.2.{idx}/32"}} for idx in (1, 2)]
        result = get_mlag([cap], device_name="nx-1")
        assert result is not None
        assert result["control_interface"] is None
        assert result["local_ip"] is None
        assert result["peer_ip"] is None
        assert result["peer_management_ip"] == "192.0.2.2"


class TestGetMlagPeerLink:
    """The peer-link is read from the domain's own listed interfaces of the rendering device."""

    @pytest.mark.parametrize(
        ("caps", "expected"),
        [
            pytest.param(
                _domain_with(_uplink_iface("Ethernet1/1"), _peer_link_iface("Port-Channel100")),
                "Port-Channel100",
                id="listed-interface-of-the-device",
            ),
            pytest.param(
                _domain_with(
                    {"name": "Ethernet1/33", "role": "mlag-peer", "typename": "DcimPhysicalInterface"},
                    {"name": "Port-Channel100", "role": "mlag-peer", "typename": "DcimLAGInterface", "lag_id": 100},
                ),
                "Port-Channel100",
                id="port-channel-wins-over-ports",
            ),
            pytest.param(
                # A vPC authored without a port-channel: the lowest listed mlag-peer port is the peer-link.
                _domain_with(
                    {"name": "Ethernet1/34", "role": "mlag-peer"}, {"name": "Ethernet1/33", "role": "mlag-peer"}
                ),
                "Ethernet1/33",
                id="first-port-by-name-without-port-channel",
            ),
            pytest.param(_domain_with(_peer_link_iface(), device="DC1-POD1-L2"), None, id="the-peers-interface"),
            pytest.param(_domain_with(_uplink_iface("Ethernet1/1")), None, id="no-listed-peer-interface"),
            pytest.param([_mlag_cap()], None, id="nothing-listed"),
        ],
    )
    def test_peer_link(self, caps: list[dict], expected: str | None) -> None:
        result = get_mlag(caps, device_name="DC1-POD1-L1")
        assert result is not None
        assert result["peer_link"] == expected

    def test_device_interface_with_the_peer_role_is_not_found_unless_listed(self) -> None:
        """Nothing is found on the device by role: an unlisted mlag-peer port is not the peer-link."""
        result = get_mlag([_mlag_cap()], [_peer_link_iface("Port-Channel100")], device_name="DC1-POD1-L1")
        assert result is not None
        assert result["peer_link"] is None

    @pytest.mark.parametrize(
        "listed",
        [
            pytest.param({**_peer_link_iface(), "typename": "DcimLAGInterface", "lag_id": 100}, id="lag"),
            pytest.param(_peer_link_iface(), id="untyped"),
            pytest.param(_uplink_iface(), id="no-peer-link"),
        ],
    )
    def test_physical_or_no_peer_link_is_not_virtual(self, listed: dict) -> None:
        """A DcimLAGInterface peer-link (and an untyped one) is a physical port-channel; no peer-link is not virtual."""
        result = get_mlag(_domain_with(listed), device_name="DC1-POD1-L1")
        assert result is not None
        assert result["virtual_peer_link"] is False

    def test_virtual_interface_peer_link_is_flagged_virtual(self) -> None:
        """mlag_create: virtual leaves a member-less DcimVirtualInterface (loopback100) as the peer-link."""
        listed = _domain_with(
            {"name": "Ethernet1/33", "role": "mlag-peer", "typename": "DcimPhysicalInterface"},
            {**_peer_link_iface("loopback100"), "typename": "DcimVirtualInterface"},
        )
        result = get_mlag(listed, device_name="DC1-POD1-L1")
        assert result is not None
        assert result["peer_link"] == "loopback100"
        assert result["virtual_peer_link"] is True
        assert result["peer_link_lag_id"] is None
        assert result["peer_link_members"] == []

    def test_peer_link_members_come_from_the_listed_port_channel(self) -> None:
        listed = _domain_with(
            {
                **_peer_link_iface(),
                "typename": "DcimLAGInterface",
                "lag_id": 100,
                "lacp_mode": "active",
                "member_interfaces": [{"name": "Ethernet1/33"}, {"name": "Ethernet1/34"}],
            }
        )
        result = get_mlag(listed, device_name="DC1-POD1-L1")
        assert result is not None
        assert result["peer_link_members"] == ["Ethernet1/33", "Ethernet1/34"]

    def test_host_port_channels_exclude_the_listed_peer_link(self) -> None:
        """The domain's own port-channels (``mlag_domain``) are the host-facing ones, never a listed interface."""
        listed = _domain_with({**_peer_link_iface(), "typename": "DcimLAGInterface", "lag_id": 100})
        listed[0]["id"] = "dom-1"
        device_interfaces = [
            {"name": "Port-Channel100", "lag_id": 100, "mlag_domain": {"id": "dom-1"}, "member_interfaces": []},
            {
                "name": "Port-Channel10",
                "lag_id": 10,
                "mlag_domain": {"id": "dom-1"},
                "member_interfaces": [{"name": "Ethernet1/2"}],
            },
        ]
        result = get_mlag(listed, device_interfaces, device_name="DC1-POD1-L1")
        assert result is not None
        assert result["mclag_interfaces"] == [{"name": "Port-Channel10", "members": ["Ethernet1/2"]}]


def _sonic_mlag(**overrides: object) -> dict:
    return {
        "domain_id": 1,
        "control_interface": "Vlan4094",
        "local_address": "10.254.0.0/31",
        "local_ip": "10.254.0.0",
        "peer_ip": "10.254.0.1",
        "local_management_ip": "192.0.2.1",
        "peer_management_ip": "192.0.2.2",
        "peer_link": "PortChannel100",
        "peer_link_lag_id": 100,
        "peer_link_members": ["Ethernet33", "Ethernet34"],
        "mclag_interfaces": [{"name": "PortChannel101", "members": ["Ethernet10"]}],
        **overrides,
    }


class TestSonicMlagConfig:
    def test_domain_and_portchannel_members(self) -> None:
        """MCLAG_DOMAIN uses the control SVI IPv4 addresses, not management."""
        config = get_sonic_mlag_config(_sonic_mlag())
        assert config["MCLAG_DOMAIN"] == {
            "1": {"source_ip": "10.254.0.0", "peer_ip": "10.254.0.1", "peer_link": "PortChannel100"}
        }
        assert config["MCLAG_INTERFACE"] == {"1|PortChannel101": {"if_type": "PortChannel"}}
        assert config["PORTCHANNEL"] == {
            "PortChannel100": {"admin_status": "up"},
            "PortChannel101": {"admin_status": "up"},
        }
        assert config["PORTCHANNEL_MEMBER"] == {
            "PortChannel100|Ethernet33": {},
            "PortChannel100|Ethernet34": {},
            "PortChannel101|Ethernet10": {},
        }

    def test_control_svi_tables(self) -> None:
        """The control SVI's VLAN, tagged peer-link membership and /31 are emitted."""
        config = get_sonic_mlag_config(_sonic_mlag())
        assert config["VLAN"] == {"Vlan4094": {"vlanid": "4094", "admin_status": "up"}}
        assert config["VLAN_MEMBER"] == {"Vlan4094|PortChannel100": {"tagging_mode": "tagged"}}
        assert config["VLAN_INTERFACE"] == {"Vlan4094": {}, "Vlan4094|10.254.0.0/31": {}}

    @pytest.mark.parametrize(
        ("config", "error", "match"),
        [
            pytest.param(
                # SONiC ICCP only accepts IPv4.
                _sonic_mlag(local_address="fd00:2400::/127", local_ip="fd00:2400::", peer_ip="fd00:2400::1"),
                AddressValueError,
                None,
                id="ipv6-control-address",
            ),
            pytest.param(
                {"domain_id": 1, "peer_link": "Loopback100"}, ValueError, "requires a domain", id="no-control"
            ),
            pytest.param(_sonic_mlag(peer_link_lag_id=None), ValueError, "requires a domain", id="virtual-peer-link"),
            pytest.param(_sonic_mlag(peer_ip="10.254.0.0"), ValueError, "distinct control IPs", id="same-peer-ips"),
            pytest.param(
                _sonic_mlag(peer_ip="10.254.0.0", domain_id=4096),
                ValueError,
                "between 1 and 4095",
                id="domain-id-range",
            ),
        ],
    )
    def test_an_unrenderable_domain_is_refused(self, config: dict, error: type[Exception], match: str | None) -> None:
        """An IPv6 control address, missing control addresses."""
        with pytest.raises(error, match=match):
            get_sonic_mlag_config(config)


def _render_common(template_name: str, **context: object) -> str:
    template_dir = Path(__file__).parents[2] / "templates" / "configs"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)))
    return env.get_template(template_name).render(**context)


def test_cisco_vpc_keepalive_uses_management_vrf() -> None:
    """vPC keepalive runs over the management IPs in the built-in management VRF."""
    rendered = _render_common(
        "common/cisco_nxos_vpc.j2",
        mlag={
            "domain_id": 1,
            "peer_link": "port-channel100",
            "local_management_ip": "192.0.2.1",
            "peer_management_ip": "192.0.2.2",
            "reload_delay": 300,
            "reload_delay_non_mlag": 330,
        },
    )
    assert "vpc peer-link" in rendered
    assert "peer-keepalive destination 192.0.2.2 source 192.0.2.1 vrf management" in rendered


_VPC_MLAG = {
    "domain_id": 1,
    "peer_link": "port-channel100",
    "peer_link_lag_id": 100,
    "peer_link_lacp_mode": "active",
    "peer_link_members": ["Ethernet1/33", "Ethernet1/34"],
    "local_management_ip": "192.0.2.1",
    "peer_management_ip": "192.0.2.2",
    "reload_delay": 300,
    "reload_delay_non_mlag": 330,
}


def test_cisco_vpc_renders_domain_before_the_physical_peer_link() -> None:
    """NX-OS rejects `vpc peer-link` until the vPC domain exists: domain, then port-channel, then members."""
    rendered = _render_common("common/cisco_nxos_vpc.j2", mlag={**_VPC_MLAG, "virtual_peer_link": False})
    assert rendered == (
        "\n!\n! vPC Configuration\n!\n"
        "vpc domain 1\n"
        "  peer-keepalive destination 192.0.2.2 source 192.0.2.1 vrf management\n"
        "  delay restore 300\n"
        "  delay restore interface-vlan 330\n"
        "!\n"
        "interface port-channel100\n"
        "  description vPC Peer-Link\n"
        "  switchport mode trunk\n"
        "  no shutdown\n"
        "  vpc peer-link\n"
        "!\n"
        "interface Ethernet1/33\n"
        "  channel-group 100 mode active\n"
        "  no shutdown\n"
        "!\n"
        "interface Ethernet1/34\n"
        "  channel-group 100 mode active\n"
        "  no shutdown\n"
        "!"
    )


def test_cisco_vpc_virtual_peer_link_renders_only_a_comment() -> None:
    """A member-less virtual peer-link (loopback100) cannot take `vpc peer-link`: no vPC block, one comment."""
    rendered = _render_common(
        "common/cisco_nxos_vpc.j2",
        mlag={
            **_VPC_MLAG,
            "peer_link": "loopback100",
            "peer_link_lag_id": None,
            "peer_link_members": [],
            "virtual_peer_link": True,
        },
    )
    assert rendered.strip().splitlines() == [
        "!",
        "! vPC fabric peering (virtual peer-link loopback100, domain 1) is not rendered: "
        "no routed source/destination address, port-type fabric or TCAM carving in data",
        "!",
    ]
    for rejected in ("vpc domain", "vpc peer-link", "interface loopback100", "switchport"):
        assert rejected not in rendered


def test_cisco_vpc_without_mlag_renders_nothing() -> None:
    assert _render_common("common/cisco_nxos_vpc.j2", mlag=None) == ""


def test_arista_mlag_control_svi_and_peer_addresses() -> None:
    """Arista gets the IPv6 control address on Vlan4094, peer-address and a management heartbeat."""
    rendered = _render_common(
        "common/arista_eos_mlag.j2",
        mlag={
            "domain_id": 1,
            "peer_link": "Port-Channel100",
            "control_interface": "Vlan4094",
            "local_address": "fd00:2400::/127",
            "peer_ip": "fd00:2400::1",
            "peer_management_ip": "192.0.2.2",
            "reload_delay": 300,
            "reload_delay_non_mlag": 330,
        },
    )
    assert "interface Vlan4094\n   description MLAG Peer-Link SVI\n   ipv6 address fd00:2400::/127" in rendered
    assert "   peer-address fd00:2400::1\n" in rendered
    assert "   peer-address heartbeat 192.0.2.2\n" in rendered


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


# Real pod topology: DC1-POD1 L1-L2 MLAG pair


def test_dc1_pod1_mlag_scenario() -> None:
    """Reflects the actual DC1-POD1-L1-L2-MLAG domain created in 06_lag_mlag.yml."""
    caps = [
        {"typename": "ManagedBGP"},
        _mlag_cap(
            name="DC1-POD1-L1-L2-MLAG",
            domain_id=1,
            devices=["DC1-POD1-L1", "DC1-POD1-L2"],
            interfaces=_on(
                "DC1-POD1-L1",
                {"name": "Ethernet1/33", "role": "mlag-peer"},
                {"name": "Ethernet1/34", "role": "mlag-peer"},
                {"name": "Port-Channel100", "role": "mlag-peer", "lag_id": 100},
            ),
        ),
    ]
    interfaces = [
        {"name": "Ethernet1/1", "role": "uplink"},
        {"name": "Ethernet1/2", "role": "uplink"},
        {"name": "Ethernet1/33", "role": "mlag-peer"},
        {"name": "Ethernet1/34", "role": "mlag-peer"},
        {"name": "Port-Channel100", "role": "mlag-peer", "lag_id": 100},
    ]
    result = get_mlag(caps, interfaces, device_name="DC1-POD1-L1")
    assert result is not None
    assert result["name"] == "DC1-POD1-L1-L2-MLAG"
    assert result["domain_id"] == 1
    assert result["peer_link"] == "Port-Channel100"
    assert "DC1-POD1-L1" in result["devices"]
    assert "DC1-POD1-L2" in result["devices"]
