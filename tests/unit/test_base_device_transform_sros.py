"""Unit tests for the SR OS rendering of BaseDeviceTransform (ACL binding, IRBs, policy blocks)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.unit.base_device_transform_helpers import _device_data, _make_transform, _render_leaf
from transforms.common import BaseDeviceTransform

# ---------------------------------------------------------------------------
# SR OS: segment ACL binding (SAP ingress filter per VLAN of every port)
# ---------------------------------------------------------------------------


def _sros_vxlan(vlan_ids: list[int]) -> dict:
    """A minimal VXLAN context with one L2 VNI mapping (vni = 10000 + vlan) per VLAN."""
    return {
        "enabled": True,
        "vtep": {"ipv4": "10.0.0.1"},
        "evpn": {"enabled": True, "rd_format": None},
        "anycast_gateway": None,
        "l3_vni_mappings": [],
        "l2_vni_mappings": [{"vlan_id": v, "vni": 10000 + v, "route_target": None} for v in vlan_ids],
    }


class TestSrosAclBinding:
    def test_trunk_port_gets_a_sap_and_filter_for_every_vlan(self) -> None:
        """A trunk carrying two VLANs binds each VLAN's ip-filter on its own SAP."""
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100, 200]}],
            acls=[{"vlan_id": 100, "name": "ACL-100", "rules": []}, {"vlan_id": 200, "name": "ACL-200", "rules": []}],
            vxlan=_sros_vxlan([100, 200]),
        )

        assert 'vpls "vpls-10100" sap 1/1/10:100 ingress ip-filter "100"' in rendered
        assert 'vpls "vpls-10200" sap 1/1/10:200 ingress ip-filter "200"' in rendered

    def test_vlan_without_acl_still_gets_a_sap_without_filter(self) -> None:
        """A non-first VLAN of a trunk is bridged even when no ACL protects it."""
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100, 200]}],
            vxlan=_sros_vxlan([100, 200]),
        )

        assert 'vpls "vpls-10200" sap 1/1/10:200 create' in rendered
        assert "ingress ip-filter" not in rendered

    def test_port_only_joins_the_vpls_of_its_own_vlans(self) -> None:
        """A port that does not carry the VLAN gets no SAP in that VLAN's service."""
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100]}],
            vxlan=_sros_vxlan([100, 200]),
        )

        assert "sap 1/1/10:100 create" in rendered
        assert "sap 1/1/10:200" not in rendered

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf"])
    def test_leaf_refuses_acl_for_vlan_without_l2_vni_service(self, role: str) -> None:
        """A VLAN-only segment ACL has no vpls to attach to: refuse instead of leaving it unbound."""
        transform = _make_transform(role)

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 200, "name": "ACL-200"}]):
            with pytest.raises(ValueError, match=r"cannot bind the segment ACL for VLANs \[200\]"):
                transform._extra_config(_device_data(), "nokia_sros")

    def test_leaf_accepts_acl_for_vlan_with_l2_vni_service(self) -> None:
        """An ACL whose VLAN has an L2 VNI mapping is bound by the template, so no refusal."""
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]),
            patch("transforms.common.get_vxlan_config", return_value=_sros_vxlan([100])),
        ):
            config = transform._extra_config(_device_data(), "nokia_sros")

        assert config["acls"] == [{"vlan_id": 100, "name": "ACL-100"}]

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos"])
    def test_other_platforms_accept_vlan_only_acl(self, platform: str) -> None:
        """Only the SR OS template binds ACLs through VNI services; other leaf templates are unaffected."""
        transform = _make_transform("leaf")

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 200, "name": "ACL-200"}]):
            config = transform._extra_config(_device_data(), platform)

        assert config["acls"] == [{"vlan_id": 200, "name": "ACL-200"}]


# ---------------------------------------------------------------------------
# SR OS: IRB list (anycast gateway) computed once in Python
# ---------------------------------------------------------------------------


def _gateway_vlan(vlan_id: int, **extra: object) -> dict:
    return {"vlan_id": vlan_id, "name": f"VLAN{vlan_id}", "gateway_ip": f"10.0.{vlan_id}.1/24", **extra}


def _sros_vxlan_with_l3(vlan_ids: list[int], l3_vni: int | None = 50001, extra_l3: tuple[int, ...] = ()) -> dict:
    """The SR OS VXLAN context whose VLANs sit in the VRF of ``l3_vni`` (a vprn service) plus ``extra_l3`` vprns."""
    vxlan = _sros_vxlan(vlan_ids)
    for mapping in vxlan["l2_vni_mappings"]:
        mapping["l3_vni"] = l3_vni
    vxlan["l3_vni_mappings"] = [
        {"vrf_name": f"VRF{vni}", "l3_vni": vni} for vni in ([l3_vni] if l3_vni else []) + list(extra_l3)
    ]
    return vxlan


class TestSrosIrbs:
    def test_vni_and_vprn_come_from_the_vxlan_mapping_of_the_vlan(self) -> None:
        """vpls = the L2 VNI, vprn = the VRF's L3 VNI; the gateway addresses ride along."""
        vlan = _gateway_vlan(100, gateway_ipv6="2001:db8::1/64")

        irbs = BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [vlan], _sros_vxlan_with_l3([100]))

        assert irbs == [
            {
                "vlan_id": 100,
                "vni": 10100,
                "vprn": 50001,
                "gateway_ip": "10.0.100.1/24",
                "gateway_ipv6": "2001:db8::1/64",
            }
        ]

    def test_transit_vlan_explicit_vnis_win_over_the_mapping(self) -> None:
        """A transit VLAN carries its own vni and l3_vni (transforms/common.py sets them)."""
        vlan = _gateway_vlan(3001, vni=64001, l3_vni=50003)

        irbs = BaseDeviceTransform._sros_irbs(
            "border-leaf", "nokia_sros", [vlan], _sros_vxlan_with_l3([3001], extra_l3=(50003,))
        )

        assert [(irb["vni"], irb["vprn"]) for irb in irbs] == [(64001, 50003)]

    def test_namespace_without_l3_vni_is_refused_not_given_a_vprn_numbered_like_the_vpls(self) -> None:
        """No L3 VNI mapping means no vprn: the IRB would reference a service nothing creates."""
        vlan = {"vlan_id": 100, "gateway_ip": "10.0.0.1/24", "gateway_ipv6": None, "vrf": None}
        vxlan = {"l2_vni_mappings": [{"vlan_id": 100, "vni": 10100, "l3_vni": None}], "l3_vni_mappings": []}

        with pytest.raises(
            ValueError, match=r"cannot render the gateway of VLAN 100 \(no vpls/vprn to attach the IRB to\)"
        ):
            BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [vlan], vxlan)

    def test_a_vprn_that_no_l3_vni_mapping_creates_is_refused(self) -> None:
        """A VLAN naming an L3 VNI of another VRF than the device's mappings creates is refused too."""
        with pytest.raises(ValueError, match=r"cannot render the gateway of VLAN 100"):
            BaseDeviceTransform._sros_irbs(
                "leaf", "nokia_sros", [_gateway_vlan(100, l3_vni=50009)], _sros_vxlan_with_l3([100])
            )

    def test_vlan_without_gateway_gets_no_irb(self) -> None:
        irbs = BaseDeviceTransform._sros_irbs(
            "leaf", "nokia_sros", [{"vlan_id": 100, "name": "L2ONLY"}], _sros_vxlan_with_l3([100])
        )

        assert irbs == []

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf", "border-leaf", "border-spine"])
    def test_gateway_vlan_without_vni_is_refused(self, role: str) -> None:
        """A VLAN-only gateway segment has no vpls: the IRB would reference a service nothing creates."""
        with pytest.raises(
            ValueError,
            match=rf"nokia_sros {role} cannot render the gateway of VLAN 200 "
            r"\(no vpls/vprn to attach the IRB to\); refusing incomplete config",
        ):
            BaseDeviceTransform._sros_irbs(
                role, "nokia_sros", [_gateway_vlan(100), _gateway_vlan(200)], _sros_vxlan_with_l3([100])
            )

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "dell_sonic"])
    def test_other_platforms_compute_no_irbs_and_never_refuse(self, platform: str) -> None:
        assert BaseDeviceTransform._sros_irbs("leaf", platform, [_gateway_vlan(200)], _sros_vxlan([100])) == []

    def test_without_vxlan_nothing_is_rendered_so_nothing_is_refused(self) -> None:
        assert BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [_gateway_vlan(200)], None) == []

    def test_extra_config_refuses_a_vlan_only_gateway_segment(self) -> None:
        """The refusal runs in the real context build, for the vlans the template would render."""
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_vlans", return_value=[_gateway_vlan(200)]),
            patch("transforms.common.get_vxlan_config", return_value=_sros_vxlan_with_l3([100])),
        ):
            with pytest.raises(ValueError, match=r"cannot render the gateway of VLAN 200"):
                transform._extra_config(_device_data(), "nokia_sros")

    def test_extra_config_passes_the_irb_list_to_the_template(self) -> None:
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_vlans", return_value=[_gateway_vlan(100)]),
            patch("transforms.common.get_vxlan_config", return_value=_sros_vxlan_with_l3([100])),
        ):
            config = transform._extra_config(_device_data(), "nokia_sros")

        assert [(irb["vlan_id"], irb["vni"], irb["vprn"]) for irb in config["sros_irbs"]] == [(100, 10100, 50001)]

    def test_template_iterates_the_irb_list(self) -> None:
        """The template renders exactly the IRBs it is given (no VNI fallback of its own)."""
        vlan = _gateway_vlan(100)
        vxlan = {**_sros_vxlan_with_l3([100]), "anycast_gateway": {"enabled": True, "mac": "00:00:5e:00:01:01"}}

        rendered = _render_leaf(
            "nokia_sros",
            vlans=[vlan, _gateway_vlan(200)],
            vxlan=vxlan,
            sros_irbs=BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [vlan], vxlan),
        )

        assert 'configure service vprn 50001 interface "int-vpls-10100" vpls "vpls-10100"' in rendered
        assert 'configure service vprn 50001 interface "int-vpls-10100" mac "00:00:5e:00:01:01"' in rendered
        assert 'ipv4 primary-address "10.0.100.1/24"' in rendered
        assert "int-vpls-200" not in rendered


# ---------------------------------------------------------------------------
# Border guards: the refusal in _refuse_unprotected_tenant_svis makes them redundant
# ---------------------------------------------------------------------------


class TestPolicyBlocksFollowTheContext:
    """The leaf templates render ACLs/PBR from the context alone; borders get none because they are refused first."""

    _ACL = {"vlan_id": 100, "name": "ACL-100", "rules": []}
    _PBR = {"vlan_id": 100, "fw_nexthop": "10.0.0.2", "bypass_prefixes": []}

    def test_nxos_leaf_renders_acls_and_pbr_from_the_context(self) -> None:
        rendered = _render_leaf("cisco_nxos", name="test-leaf", acls=[self._ACL], leaf_pbr_rules=[self._PBR])

        assert "! Zero-Trust ACLs" in rendered
        assert "feature pbr" in rendered
        assert "! Leaf PBR" in rendered

    def test_nxos_border_template_renders_only_from_the_context(self) -> None:
        """The template renders policy only from the context; the refusal tests prove the removed guards redundant."""
        rendered = _render_leaf("cisco_nxos", name="test-bl", nxos_border=True, acls=[], leaf_pbr_rules=[])

        assert "Zero-Trust" not in rendered
        assert "feature pbr" not in rendered
        assert "Leaf PBR" not in rendered

    def test_sros_leaf_renders_ip_filters_and_binds_them(self) -> None:
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100]}],
            acls=[self._ACL],
            vxlan=_sros_vxlan([100]),
        )

        assert "configure filter ip-filter 100 admin-state enable" in rendered
        assert 'sap 1/1/10:100 ingress ip-filter "100"' in rendered

    def test_sros_border_template_renders_only_from_the_context(self) -> None:
        """The template renders filters only from the context; the refusal tests prove the removed guards redundant."""
        rendered = _render_leaf(
            "nokia_sros",
            sros_border=True,
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100]}],
            acls=[],
            vxlan=_sros_vxlan([100]),
        )

        assert "ip-filter" not in rendered
        assert "Zero-Trust" not in rendered
