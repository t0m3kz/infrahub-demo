"""Unit tests for the Catalyst 9000 / IOS-XE EVPN-VXLAN platform (``cisco_xe``).

There is no platform allow-list: the template ``templates/configs/<template_subdir>/<platform>.j2`` is the gate
(``BaseDeviceTransform._load_template``), so a role without a cisco_xe template raises ``jinja2.TemplateNotFound``.
The leaf, access-leaf and border-leaf roles have one, and ``BaseDeviceTransform._refuse_cisco_xe_unrendered``
refuses what those templates do not render (PBR, MLAG, Multi-Site, non-EVPN VXLAN). The segment ACLs and the inter-VRF
statics are rendered (section 6, transforms/helpers/cisco_xe.py); what IOS-XE cannot express in them is refused there.

Byte-exact rendering is covered by tests/smoke/test_configs_smoke.py (the ``*_cisco_xe_*`` goldens); this file
covers the wiring, the IOS-XE-versus-NX-OS dialect, the refusal guard and the unchanged behaviour of the other
roles and platforms. The office switches' LISP goldens (``access_switch_/distribution_switch_cisco_xe_lisp_*``) are
covered in section 5: the guard's refusals that still apply there; their rendering is tests/unit/test_office_lisp_transform.py.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any
from unittest.mock import patch

import jinja2
import pytest

from tests.smoke.test_configs_smoke import _run_transform
from tests.unit.cisco_xe_platform_helpers import _golden_input, _make_transform
from transforms.common import BaseDeviceTransform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.distribution_switch import DistributionSwitch
from transforms.config.edge import Edge
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR
from transforms.helpers.vxlan import get_vxlan_config

BGP_VARIANTS = ["ebgp_ebgp", "ebgp_ibgp", "ospf_ibgp"]
FABRIC_ROLES = ["leaf", "access-leaf", "border-leaf"]

# (golden directory prefix, transform class, device_role) of the three roles with a cisco_xe fabric template.
FABRIC_TRANSFORMS: list[tuple[str, type[BaseDeviceTransform], str]] = [
    ("leaf", Leaf, "leaf"),
    ("access_leaf", AccessLeaf, "access-leaf"),
    ("border_leaf", BorderLeaf, "border-leaf"),
]

# (golden directory prefix, transform class, device_role) of an office's LISP switches, which share the leaf template.
OFFICE_TRANSFORMS: list[tuple[str, type[BaseDeviceTransform], str]] = [
    ("access_switch", AccessSwitch, "access-switch"),
    ("distribution_switch", DistributionSwitch, "distribution-switch"),
]

# Roles that have no cisco_xe fabric template and so are untouched by the refusal guard.
NON_FABRIC_TRANSFORMS: list[tuple[type[BaseDeviceTransform], str]] = [
    (Spine, "spine"),
    (SuperSpine, "super-spine"),
    (L2Leaf, "l2-leaf"),
    (Edge, "edge"),
]

NXOS_ONLY_LINES = [
    "feature nv overlay",
    "nv overlay evpn",
    "vn-segment",
    "fabric forwarding",
    "suppress-arp",
    "vrf member",
    "vrf context",
]

# Top-level IOS-XE EVPN constructs; each section that holds one must carry a `! unverified-no-lab:` line.
IOSXE_EVPN_CONSTRUCTS = [
    r"^vrf definition",
    r"^l2vpn evpn",
    r"^vlan configuration",
    r"^interface nve1",
    r"^ address-family ipv4 vrf",
    r"^ mac-address",
]

# The keys the guard refuses, with the label it reports for each (acls and vrf_routes are rendered, see section 6).
REFUSED_KEYS = ["leaf_pbr_rules", "mlag"]
RENDERED_KEYS = ["acls", "vrf_routes"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@cache
def _render(prefix: str, variant: str) -> str:
    """Render the cisco_xe golden input of ``<prefix>_cisco_xe_<variant>`` with its transform."""
    transform_cls = next(cls for pfx, cls, _role in FABRIC_TRANSFORMS if pfx == prefix)
    return _run_transform(transform_cls, _golden_input(f"{prefix}_cisco_xe_{variant}"))


def _clean_config() -> dict[str, Any]:
    """A template context with nothing the cisco_xe templates cannot render."""
    return {
        "acls": [],
        "leaf_pbr_rules": [],
        "vrf_routes": [],
        "mlag": None,
        "vxlan": {"enabled": True, "evpn": {"enabled": True}, "multisite": None},
    }


def _vxlan_device_data() -> dict[str, Any]:
    return {"interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.3/32"}]}], "capabilities": []}


def _activation() -> dict[str, Any]:
    """A cleaned, routed SegmentDeployment (L2 VNI 10100, L3 VNI 50001) as clean_data() returns it."""
    return {
        "vlan_id": 100,
        "vni": 10100,
        "status": "active",
        "segment": {
            "name": "Owner - production - seg-100",
            "customer_name": "seg-100",
            "arp_suppression": True,
            "gateway": {"address": "10.100.0.1/24", "ip_prefix": {"ip_namespace": {"name": "PROD", "l3_vni": 50001}}},
        },
    }


def _with_platform(data: dict[str, Any], netmiko_device_type: str) -> dict[str, Any]:
    """Re-point the first device of a golden input to another netmiko platform."""
    device = data["DcimDevice"]["edges"][0]["node"]
    device["platform"]["node"]["netmiko_device_type"]["value"] = netmiko_device_type
    return data


# ---------------------------------------------------------------------------
# 1. Platform wiring: _PLATFORM_VXLAN_KEYS
# ---------------------------------------------------------------------------


class TestPlatformVxlanKeys:
    """get_vxlan_config adds each platform's own keys to the platform-agnostic VXLAN config."""

    @staticmethod
    def _config(platform: str) -> dict[str, Any]:
        config = get_vxlan_config(_vxlan_device_data(), platform, device_role="leaf", activations=[_activation()])
        assert config is not None
        return config

    def test_cisco_xe_gets_nve1_and_static_replication_without_nxos_features(self) -> None:
        """cisco_xe: nve1 + static ingress replication, and none of the NX-OS feature list."""
        config = self._config("cisco_xe")

        assert config["nve_interface"] == "nve1"
        assert config["replication_type"] == "static"
        assert "features" not in config
        assert "interface" not in config

    def test_cisco_nxos_keeps_features_and_has_no_replication_type(self) -> None:
        """cisco_nxos is unchanged: feature list and nve1, no replication_type."""
        config = self._config("cisco_nxos")

        assert config["nve_interface"] == "nve1"
        assert {"nv overlay", "vn-segment-vlan-based", "nve"} <= set(config["features"])
        assert "replication_type" not in config

    @pytest.mark.parametrize("platform", ["arista_eos", "sonic", "dell_sonic"])
    def test_other_platforms_have_no_replication_type_or_features(self, platform: str) -> None:
        """No other platform picks up the cisco_xe keys."""
        config = self._config(platform)

        assert "replication_type" not in config
        assert "features" not in config
        assert "nve_interface" not in config

    def test_unknown_platform_gets_no_platform_specific_keys(self) -> None:
        """A platform without an entry keeps the platform-agnostic config only (unchanged behaviour)."""
        config = self._config("juniper_junos")

        for key in ("nve_interface", "interface", "replication_type", "features"):
            assert key not in config

    def test_cisco_xe_config_is_the_agnostic_config_plus_its_two_keys(self) -> None:
        """cisco_xe adds exactly nve_interface and replication_type on top of an unknown platform's config."""
        base = self._config("juniper_junos")
        xe = self._config("cisco_xe")

        assert set(xe) - set(base) == {"nve_interface", "replication_type"}
        assert {key: value for key, value in xe.items() if key in base} == base


# ---------------------------------------------------------------------------
# 2. Rendering: the IOS-XE dialect of the leaf, access-leaf and border-leaf golden inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", BGP_VARIANTS)
@pytest.mark.parametrize(("prefix", "transform_cls", "role"), FABRIC_TRANSFORMS)
class TestCiscoXeRendering:
    """The three fabric roles render the same IOS-XE EVPN-VXLAN dialect over every underlay/overlay BGP variant."""

    def test_renders_the_iosxe_evpn_vxlan_constructs(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, variant: str
    ) -> None:
        """L2VPN EVPN instances, VLAN configuration, NVE host-reachability, ingress replication, VRF stitching."""
        output = _render(prefix, variant)

        for expected in (
            "l2vpn evpn instance",
            "vlan configuration",
            "member evpn-instance",
            "interface nve1",
            "host-reachability protocol bgp",
            "vrf definition",
            "address-family l2vpn evpn",
        ):
            assert expected in output, expected
        assert re.search(r"^ member vni \d+ ingress-replication$", output, re.M)
        assert re.search(r"^ member vni \d+ vrf \S+$", output, re.M)
        assert re.search(r"^  route-target (import|export) \S+ stitching$", output, re.M)

    def test_interface_addresses_use_dotted_masks(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, variant: str
    ) -> None:
        """IOS-XE takes `ip address A.B.C.D M.M.M.M`, never the CIDR form of NX-OS/EOS."""
        output = _render(prefix, variant)

        assert " ip address 10.1.0.1 255.255.255.254\n" in output
        assert " ip address 10.0.0.1 255.255.255.255\n" in output
        assert not re.search(r"^ ip address \S+/\d+", output, re.M)

    def test_vlan100_gateway_svi_carries_the_anycast_mac(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, variant: str
    ) -> None:
        """The anycast gateway MAC is an interface-level `mac-address` in IOS-XE dotted form."""
        output = _render(prefix, variant)

        assert re.search(r"^interface Vlan100\n(?: .*\n)*? mac-address 001c\.7300\.dc01\n", output, re.M)

    @pytest.mark.parametrize("nxos_line", NXOS_ONLY_LINES)
    def test_has_no_nxos_constructs(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, variant: str, nxos_line: str
    ) -> None:
        """None of the NX-OS dialect leaks into the IOS-XE config."""
        assert nxos_line not in _render(prefix, variant)

    def test_every_iosxe_evpn_section_is_marked_unverified_no_lab(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, variant: str
    ) -> None:
        """No lab verified these blocks: each section holding an IOS-XE EVPN construct says so."""
        output = _render(prefix, variant)
        sections = re.split(r"\n!\n!\n", output)

        holding = [s for s in sections if any(re.search(p, s, re.M) for p in IOSXE_EVPN_CONSTRUCTS)]
        assert len(holding) >= 6
        for section in holding:
            assert re.search(r"^! unverified-no-lab:", section, re.M), section[:80]

    def test_header_names_the_role(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, variant: str
    ) -> None:
        """The role in the header comes from the transform's role, the hostname from the device."""
        output = _render(prefix, variant)

        assert f"! Cisco IOS-XE (Catalyst 9000) EVPN-VXLAN {role}\n" in output
        assert re.search(r"^hostname \S+$", output, re.M)


@pytest.mark.parametrize("variant", BGP_VARIANTS)
@pytest.mark.parametrize("prefix", ["access_leaf", "border_leaf"])
def test_access_and_border_leaf_render_the_leaf_config_but_for_the_header(prefix: str, variant: str) -> None:
    """The access-leaf and border-leaf golden inputs describe the same fabric: only the identity lines differ."""
    leaf = _render("leaf", variant).splitlines()
    other = _render(prefix, variant).splitlines()

    assert len(leaf) == len(other)
    differing = [i for i, (a, b) in enumerate(zip(leaf, other, strict=True)) if a != b]
    assert differing, "the role header must differ"
    # Only the "! <name> Configuration", "! Cisco IOS-XE ... <role>" and "hostname" lines.
    assert all(i < 6 for i in differing)
    assert leaf[6:] == other[6:]


# ---------------------------------------------------------------------------
# 3. Refusal path: BaseDeviceTransform._refuse_cisco_xe_unrendered
# ---------------------------------------------------------------------------


class TestRefuseCiscoXeUnrendered:
    """cisco_xe leaf/access-leaf/border-leaf refuse what their EVPN-VXLAN templates cannot render."""

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_clean_config_passes(self, role: str) -> None:
        """Nothing outside what the templates render: no error."""
        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", _clean_config())

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_empty_config_passes(self, role: str) -> None:
        """A device without VXLAN or services (an empty context) is not refused."""
        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", {})

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_vxlan_none_passes(self, role: str) -> None:
        """`vxlan: None` (no overlay on the device) is not refused."""
        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", {**_clean_config(), "vxlan": None})

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    @pytest.mark.parametrize("key", REFUSED_KEYS)
    def test_refuses_each_unrendered_service(self, role: str, key: str) -> None:
        """A truthy leaf_pbr_rules / mlag is named in the error."""
        config = {**_clean_config(), key: [{"vlan_id": 100}] if key != "mlag" else {"domain_id": 1}}

        with pytest.raises(ValueError) as excinfo:
            _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

        assert str(excinfo.value) == f"cisco_xe {role} cannot render: {key}; refusing incomplete config"

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    @pytest.mark.parametrize("key", RENDERED_KEYS)
    def test_acls_and_vrf_routes_are_not_refused(self, role: str, key: str) -> None:
        """The templates render the segment ACLs and the exchange statics: the guard lets them through."""
        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", {**_clean_config(), key: [{"vlan_id": 100}]})

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_refuses_multisite(self, role: str) -> None:
        """An EVPN Multi-Site border gateway cannot be rendered."""
        config = _clean_config()
        config["vxlan"]["multisite"] = {"site_id": 1}

        with pytest.raises(ValueError) as excinfo:
            _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

        assert str(excinfo.value) == f"cisco_xe {role} cannot render: multisite; refusing incomplete config"

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    @pytest.mark.parametrize("evpn", [None, {}, {"enabled": False}])
    def test_refuses_vxlan_without_an_evpn_bgp_process(self, role: str, evpn: dict | None) -> None:
        """VXLAN enabled but no EVPN BGP process to advertise it: refuse."""
        config = {**_clean_config(), "vxlan": {"enabled": True, "evpn": evpn}}

        with pytest.raises(ValueError) as excinfo:
            _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

        assert (
            str(excinfo.value)
            == f"cisco_xe {role} cannot render: VXLAN without an EVPN BGP process; refusing incomplete config"
        )

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_disabled_vxlan_without_evpn_passes(self, role: str) -> None:
        """VXLAN not enabled: a missing EVPN process is irrelevant."""
        config = {**_clean_config(), "vxlan": {"enabled": False, "evpn": None}}

        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    @pytest.mark.parametrize("key", REFUSED_KEYS)
    def test_lisp_config_without_vxlan_still_refuses_each_unrendered_service(self, role: str, key: str) -> None:
        """A LISP device (`lisp` context, `vxlan: None`) is refused for the same services as an EVPN one."""
        config = {**_clean_config(), "vxlan": None, "lisp": {"enabled": True}}
        config[key] = [{"vlan_id": 100}] if key != "mlag" else {"domain_id": 1}

        with pytest.raises(ValueError) as excinfo:
            _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

        assert str(excinfo.value) == f"cisco_xe {role} cannot render: {key}; refusing incomplete config"

    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_lisp_config_without_vxlan_or_evpn_process_is_not_refused(self, role: str) -> None:
        """A LISP device has no EVPN BGP process by design: the missing-EVPN refusal needs VXLAN enabled."""
        config = {**_clean_config(), "vxlan": None, "lisp": {"enabled": True}}

        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

    def test_lists_every_unsupported_feature_in_a_fixed_order(self) -> None:
        """Several problems at once: all reported, services first, then multisite, then the EVPN gap.

        The rendered acls and vrf_routes are in the context too and are not reported."""
        config = {
            "acls": [{"vlan_id": 100}],
            "leaf_pbr_rules": [{"vlan_id": 100}],
            "vrf_routes": [{"prefix": "0.0.0.0/0"}],
            "mlag": {"domain_id": 1},
            "vxlan": {"enabled": True, "evpn": None, "multisite": {"site_id": 1}},
        }

        with pytest.raises(ValueError) as excinfo:
            _make_transform("border-leaf")._refuse_cisco_xe_unrendered("cisco_xe", config)

        assert str(excinfo.value) == (
            "cisco_xe border-leaf cannot render: leaf_pbr_rules, mlag, multisite, "
            "VXLAN without an EVPN BGP process; refusing incomplete config"
        )

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "cisco_ios", "nokia_sros"])
    @pytest.mark.parametrize("role", FABRIC_ROLES)
    def test_guard_is_specific_to_cisco_xe(self, platform: str, role: str) -> None:
        """The same truthy keys that refuse a cisco_xe leaf do not raise on any other platform."""
        config = {
            "acls": [{"vlan_id": 100}],
            "leaf_pbr_rules": [{"vlan_id": 100}],
            "vrf_routes": [{"prefix": "0.0.0.0/0"}],
            "mlag": {"domain_id": 1},
            "vxlan": {"enabled": True, "evpn": None, "multisite": {"site_id": 1}},
        }

        _make_transform(role)._refuse_cisco_xe_unrendered(platform, config)

    @pytest.mark.parametrize("role", ["spine", "super-spine", "l2-leaf", "edge", "firewall", ""])
    def test_roles_without_a_cisco_xe_fabric_template_are_not_checked(self, role: str) -> None:
        """Spine, super-spine, l2-leaf and edge (an IOS template) keep their behaviour: the guard returns silently."""
        config = {**_clean_config(), "acls": [{"vlan_id": 100}], "mlag": {"domain_id": 1}}

        _make_transform(role)._refuse_cisco_xe_unrendered("cisco_xe", config)

    def test_tor_shares_the_leaf_template_and_is_refused_like_a_leaf(self) -> None:
        """ToR renders the cisco_xe leaf template, so it refuses what that template cannot render."""
        config = {**_clean_config(), "leaf_pbr_rules": [{"vlan_id": 100}]}

        with pytest.raises(ValueError, match="cisco_xe tor cannot render: leaf_pbr_rules"):
            _make_transform("tor")._refuse_cisco_xe_unrendered("cisco_xe", config)

    def test_border_spine_is_refused_even_with_a_clean_config(self) -> None:
        """BorderSpine shares border_leafs/cisco_xe.j2, which renders no route-reflector role: always refused."""
        with pytest.raises(ValueError, match="cisco_xe border-spine cannot render the route-reflector role"):
            _make_transform("border-spine")._refuse_cisco_xe_unrendered("cisco_xe", _clean_config())

    def test_border_spine_is_not_refused_on_another_platform(self) -> None:
        """The border-spine refusal is specific to cisco_xe."""
        _make_transform("border-spine")._refuse_cisco_xe_unrendered("cisco_nxos", _clean_config())


@pytest.mark.parametrize(("prefix", "transform_cls", "role"), FABRIC_TRANSFORMS)
class TestCiscoXeRefusalThroughTransform:
    """The guard runs inside transform(), before the template renders."""

    @pytest.mark.parametrize("key", REFUSED_KEYS)
    def test_transform_refuses_unrendered_service(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, key: str
    ) -> None:
        """A context carrying an unrenderable service makes transform() raise instead of returning a partial config."""
        real = BaseDeviceTransform._template_context

        def with_service(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {**real(self, *args, **kwargs), key: [{"vlan_id": 100}]}

        with patch.object(BaseDeviceTransform, "_template_context", with_service):
            with pytest.raises(ValueError, match=rf"cisco_xe {role} cannot render: {key}; refusing incomplete config"):
                _run_transform(transform_cls, _golden_input(f"{prefix}_cisco_xe_ebgp_ibgp"))

    def test_golden_input_is_not_refused(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """The golden inputs need nothing the templates lack, so the guard lets them through."""
        output = _run_transform(transform_cls, _golden_input(f"{prefix}_cisco_xe_ospf_ibgp"))

        assert "interface nve1" in output


# ---------------------------------------------------------------------------
# 4. Unsupported platform / role unchanged: the template file is the gate
# ---------------------------------------------------------------------------


class TestUnsupportedCombinationsUnchanged:
    """A role or platform without a template still fails with TemplateNotFound, never with the cisco_xe ValueError."""

    @pytest.mark.parametrize(
        ("transform_cls", "missing"),
        [
            (Spine, "spines/cisco_xe.j2"),
            (SuperSpine, "super_spines/cisco_xe.j2"),
            (L2Leaf, "l2_leafs/cisco_xe.j2"),
        ],
    )
    def test_role_without_cisco_xe_template_raises_template_not_found(
        self, transform_cls: type[BaseDeviceTransform], missing: str
    ) -> None:
        """Spine, SuperSpine and L2Leaf on a cisco_xe device: no template, so TemplateNotFound."""
        with pytest.raises(jinja2.TemplateNotFound, match=re.escape(missing)):
            _run_transform(transform_cls, _golden_input("leaf_cisco_xe_ebgp_ibgp"))

    @pytest.mark.parametrize(
        ("prefix", "transform_cls", "subdir"),
        [("leaf", Leaf, "leafs"), ("access_leaf", AccessLeaf, "leafs"), ("border_leaf", BorderLeaf, "border_leafs")],
    )
    def test_fabric_role_on_unsupported_platform_raises_template_not_found(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], subdir: str
    ) -> None:
        """A juniper_junos leaf-family device is still a missing template, not a cisco_xe refusal."""
        data = _with_platform(_golden_input(f"{prefix}_cisco_xe_ebgp_ibgp"), "juniper_junos")

        with pytest.raises(jinja2.TemplateNotFound, match=f"{subdir}/juniper_junos.j2"):
            _run_transform(transform_cls, data)

    def test_unsupported_platform_with_unrenderable_service_is_still_template_not_found(self) -> None:
        """The guard is keyed on the platform: a non-cisco_xe device reaches the template lookup, not the ValueError."""
        real = BaseDeviceTransform._template_context

        def with_acls(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {**real(self, *args, **kwargs), "acls": [{"vlan_id": 100}]}

        data = _with_platform(_golden_input("leaf_cisco_xe_ebgp_ibgp"), "juniper_junos")
        with patch.object(BaseDeviceTransform, "_template_context", with_acls):
            with pytest.raises(jinja2.TemplateNotFound):
                _run_transform(Leaf, data)

    @pytest.mark.parametrize(("transform_cls", "role"), NON_FABRIC_TRANSFORMS)
    def test_non_fabric_role_declares_its_own_role(self, transform_cls: type[BaseDeviceTransform], role: str) -> None:
        """These transforms are outside the guarded roles (so the guard cannot change their outcome)."""
        assert transform_cls.device_role == role
        assert role not in BaseDeviceTransform._CISCO_XE_FABRIC_ROLES

    @pytest.mark.parametrize(
        ("transform_cls", "role"),
        [(Leaf, "leaf"), (ToR, "tor"), (AccessLeaf, "access-leaf"), (BorderLeaf, "border-leaf")],
    )
    def test_fabric_transforms_use_the_guarded_roles(self, transform_cls: type[BaseDeviceTransform], role: str) -> None:
        """The guard's role set matches the device_role of the transforms that render a cisco_xe fabric template."""
        assert transform_cls.device_role == role
        assert role in BaseDeviceTransform._CISCO_XE_FABRIC_ROLES

    def test_tor_renders_the_leaf_template_on_cisco_xe(self) -> None:
        """ToR is a leaf-tier VTEP: it renders the cisco_xe leaf template."""
        rendered = _run_transform(ToR, _golden_input("leaf_cisco_xe_ebgp_ibgp"))

        assert "interface nve1" in rendered

    def test_border_spine_on_cisco_xe_is_refused_through_transform(self) -> None:
        """BorderSpine must not silently render the border-leaf template without its route-reflector role."""
        with pytest.raises(ValueError, match="route-reflector"):
            _run_transform(BorderSpine, _golden_input("leaf_cisco_xe_ebgp_ibgp"))


# ---------------------------------------------------------------------------
# 5. Office LISP site: the guard still runs on the access-switch / distribution-switch goldens
# ---------------------------------------------------------------------------

OFFICE_LISP_VARIANT = "lisp_ospf"


@pytest.mark.parametrize(("prefix", "transform_cls", "role"), OFFICE_TRANSFORMS)
class TestCiscoXeLispRefusalThroughTransform:
    """The guard runs inside transform() on a LISP device: the services its templates lack are refused."""

    @pytest.mark.parametrize("key", REFUSED_KEYS)
    def test_lisp_transform_refuses_unrendered_service(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str, key: str
    ) -> None:
        """leaf_pbr_rules / mlag on a LISP device make transform() raise, not drop them."""
        real = BaseDeviceTransform._template_context

        def with_service(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {**real(self, *args, **kwargs), key: [{"vlan_id": 100}]}

        with patch.object(BaseDeviceTransform, "_template_context", with_service):
            with pytest.raises(ValueError, match=rf"cisco_xe {role} cannot render: {key}; refusing incomplete config"):
                _run_transform(transform_cls, _golden_input(f"{prefix}_cisco_xe_{OFFICE_LISP_VARIANT}"))
