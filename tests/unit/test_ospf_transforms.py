"""Unit tests for OSPF configuration helpers.

Covers:
- get_ospf() — process extraction, router_id/process_id/reference_bandwidth passthrough
- the OSPF partials' interface authentication per platform, and refuse_unrenderable_ospf_auth()
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from transforms.config.spine import Spine
from transforms.helpers.ospf import get_ospf, refuse_unrenderable_ospf_auth


def _ospf_service(
    *,
    process_id: str = "1",
    router_id: str = "10.0.0.1/32",
    reference_bandwidth: int = 100000,
) -> dict:
    return {
        "typename": "ManagedOSPF",
        "process_id": process_id,
        "router_id": {"address": router_id},
        "reference_bandwidth": reference_bandwidth,
    }


class TestGetOspfNoServices:
    def test_empty_capabilities_returns_empty_list(self) -> None:
        assert get_ospf([]) == []

    def test_no_ospf_typename_returns_empty_list(self) -> None:
        caps = [{"typename": "ManagedBGP"}, {"typename": "ManagedMLAG"}]
        assert get_ospf(caps) == []


class TestGetOspfExtraction:
    def test_returns_process_id(self) -> None:
        result = get_ospf([_ospf_service(process_id="42")])
        assert result[0]["process_id"] == "42"

    def test_returns_router_id_address_without_prefix_stripping(self) -> None:
        """router_id in the returned config is the raw address (prefix stripped in templates)."""
        result = get_ospf([_ospf_service(router_id="10.0.0.5/32")])
        assert result[0]["router_id"] == "10.0.0.5/32"

    def test_returns_reference_bandwidth(self) -> None:
        result = get_ospf([_ospf_service(reference_bandwidth=40000)])
        assert result[0]["reference_bandwidth"] == 40000

    def test_multiple_services_sorted_by_process_id(self) -> None:
        services = [
            _ospf_service(process_id="2", router_id="10.0.0.2/32"),
            _ospf_service(process_id="1", router_id="10.0.0.1/32"),
        ]
        result = get_ospf(services)
        assert [c["process_id"] for c in result] == ["1", "2"]

    def test_non_ospf_capabilities_ignored(self) -> None:
        caps = [
            {"typename": "ManagedBGP"},
            _ospf_service(process_id="1"),
        ]
        result = get_ospf(caps)
        assert len(result) == 1
        assert result[0]["process_id"] == "1"


class TestGetOspfReferenceBandwidthFromInterfaces:
    """reference_bandwidth is derived from the fastest physical interface when available."""

    def test_derived_from_fastest_interface_overrides_schema_default(self) -> None:
        interfaces = [
            {"interface_type": "100gbase-x-qsfp28"},
            {"interface_type": "25gbase-x-sfp28"},
        ]
        result = get_ospf([_ospf_service(reference_bandwidth=100000)], interfaces)
        assert result[0]["reference_bandwidth"] == 100000  # 100 Gbps -> 100000 Mbps

    def test_400g_interface_overrides_schema_default(self) -> None:
        interfaces = [{"interface_type": "400gbase-x-qsfpdd"}]
        result = get_ospf([_ospf_service(reference_bandwidth=100000)], interfaces)
        assert result[0]["reference_bandwidth"] == 400000

    def test_no_interfaces_falls_back_to_schema_value(self) -> None:
        result = get_ospf([_ospf_service(reference_bandwidth=40000)], [])
        assert result[0]["reference_bandwidth"] == 40000

    def test_none_interfaces_falls_back_to_schema_value(self) -> None:
        result = get_ospf([_ospf_service(reference_bandwidth=40000)], None)
        assert result[0]["reference_bandwidth"] == 40000

    def test_interfaces_without_recognizable_speed_fall_back_to_schema_value(self) -> None:
        interfaces = [{"interface_type": "other"}, {"interface_type": None}]
        result = get_ospf([_ospf_service(reference_bandwidth=40000)], interfaces)
        assert result[0]["reference_bandwidth"] == 40000


# ---------------------------------------------------------------------------
# OSPF interface authentication: rendered per platform, or refused
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "configs"


def _ospf_interface(name: str = "Ethernet1", mode: str | None = "sha1", password: str | None = "s3cr3t") -> dict:
    """A template interface (get_interfaces shape) with an OSPF authentication mode and key."""
    return {
        "name": name,
        "ospf": {
            "area": 0,
            "mode": "peer_to_peer",
            "metric": None,
            "process_id": 1,
            "authentication_mode": mode,
            "password": password,
        },
    }


def _render_partial(source: str, interface: dict) -> list[str]:
    """Render an OSPF partial (via ``source``) for one interface; return its stripped lines."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)))
    rendered = env.from_string(source).render(
        ospf=[{"process_id": 1, "router_id": "10.0.0.1/32", "reference_bandwidth": 100000}],
        interfaces=[interface],
    )
    return [line.strip() for line in rendered.splitlines()]


class TestOspfAuthenticationRendering:
    def test_eos_renders_sha1_as_a_sha1_message_digest_key(self) -> None:
        """EOS takes SHA-1 natively: a message-digest key of type sha1, never the plain-text key."""
        lines = _render_partial(
            "{% from 'common/arista_eos_ospf.j2' import render_ospf %}{{ render_ospf(ospf, interfaces) }}",
            _ospf_interface(mode="sha1"),
        )
        assert "ip ospf authentication message-digest" in lines
        assert "ip ospf message-digest-key 1 sha1 s3cr3t" in lines
        assert not any(line.startswith("ip ospf authentication-key") for line in lines)

    def test_eos_md5_and_plain_text_render_as_before(self) -> None:
        """md5 stays an md5 message-digest key; no mode stays the plain-text key."""
        source = "{% from 'common/arista_eos_ospf.j2' import render_ospf %}{{ render_ospf(ospf, interfaces) }}"
        assert "ip ospf message-digest-key 1 md5 s3cr3t" in _render_partial(source, _ospf_interface(mode="md5"))
        assert "ip ospf authentication-key s3cr3t" in _render_partial(source, _ospf_interface(mode=None))

    def test_sros_renders_md5_as_a_message_digest_key(self) -> None:
        """SR OS used to render every key as authentication-type password, whatever the mode."""
        lines = _render_partial("{% include 'common/nokia_sros_ospf.j2' %}", _ospf_interface(mode="md5"))
        assert 'configure router "Base" ospf 1 interface "Ethernet1" authentication-type message-digest' in lines
        assert 'configure router "Base" ospf 1 interface "Ethernet1" message-digest-key 1 md5 "s3cr3t"' in lines
        assert not any("authentication-type password" in line for line in lines)

    def test_sros_without_a_mode_keeps_the_password_key(self) -> None:
        """No mode is still the plain password key on SR OS."""
        lines = _render_partial("{% include 'common/nokia_sros_ospf.j2' %}", _ospf_interface(mode=None))
        assert 'configure router "Base" ospf 1 interface "Ethernet1" authentication-type password' in lines
        assert 'configure router "Base" ospf 1 interface "Ethernet1" authentication-key "s3cr3t"' in lines


class TestRefuseUnrenderableOspfAuth:
    @pytest.mark.parametrize("platform", ["cisco_nxos", "cisco_ios", "cisco_xe", "nokia_sros", "sonic", "dell_sonic"])
    def test_sha1_is_refused_where_the_partial_cannot_render_it(self, platform: str) -> None:
        """NX-OS/IOS/SR OS need a key chain for HMAC-SHA and SONiC renders no OSPF interface: refuse."""
        with pytest.raises(ValueError, match=r"Ethernet1 \(sha1\)"):
            refuse_unrenderable_ospf_auth(platform, [_ospf_interface(mode="sha1")])

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_md5_is_refused_on_sonic(self, platform: str) -> None:
        """SONiC renders no OSPF interface, so no explicit mode can be honoured."""
        with pytest.raises(ValueError, match="md5"):
            refuse_unrenderable_ospf_auth(platform, [_ospf_interface(mode="md5")])

    @pytest.mark.parametrize(
        ("platform", "mode"),
        [
            ("arista_eos", "sha1"),
            ("arista_eos", "md5"),
            ("cisco_nxos", "md5"),
            ("cisco_xe", "md5"),
            ("nokia_sros", "md5"),
        ],
    )
    def test_rendered_modes_pass(self, platform: str, mode: str) -> None:
        """A mode the platform's partial renders is not refused."""
        refuse_unrenderable_ospf_auth(platform, [_ospf_interface(mode=mode)])

    @pytest.mark.parametrize("platform", ["cisco_nxos", "sonic"])
    def test_no_mode_or_no_password_is_not_refused(self, platform: str) -> None:
        """A plain-text key, and a mode without a key (nothing rendered), are left to the template."""
        refuse_unrenderable_ospf_auth(
            platform,
            [
                _ospf_interface("Ethernet1", mode=None),
                _ospf_interface("Ethernet2", mode="sha1", password=None),
                {"name": "Ethernet3"},
            ],
        )

    def test_device_transform_refuses_an_nxos_sha1_interface(self) -> None:
        """The base context build runs the check, so the artifact fails instead of shipping a plain-text key."""
        transform = Spine.__new__(Spine)
        device = {
            "name": "spine-01",
            "role": "spine",
            "capabilities": [],
            "interfaces": [
                {
                    "name": "Ethernet1/1",
                    "typename": "DcimPhysicalInterface",
                    "capabilities": [
                        {
                            "typename": "RoutingOSPFInterface",
                            "mode": "peer_to_peer",
                            "authentication_mode": "sha1",
                            "password": {"password": "s3cr3t"},
                            "peering": {"ospf_area": {"area": 0}, "ospf_process": []},
                        }
                    ],
                }
            ],
        }
        with pytest.raises(ValueError, match=r"cisco_nxos cannot render the OSPF authentication mode of Ethernet1/1"):
            transform._build_config(device, "cisco_nxos")
