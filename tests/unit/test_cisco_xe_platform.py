"""Unit tests for the Catalyst 9000 / IOS-XE EVPN-VXLAN platform (``cisco_xe``).

There is no platform allow-list: the template ``templates/configs/<template_subdir>/<platform>.j2`` is the gate
(``BaseDeviceTransform._load_template``), so a role without a cisco_xe template raises ``jinja2.TemplateNotFound``.
The leaf, access-leaf and border-leaf roles have one, and ``BaseDeviceTransform._refuse_cisco_xe_unrendered``
refuses what those templates do not render (PBR, MLAG, Multi-Site, non-EVPN VXLAN). The segment ACLs and the inter-VRF
statics are rendered (section 6, transforms/helpers/cisco_xe.py); what IOS-XE cannot express in them is refused there.

Byte-exact rendering is covered by tests/smoke/test_configs_smoke.py (the ``*_cisco_xe_*`` goldens); this file
covers the wiring, the IOS-XE-versus-NX-OS dialect, the refusal guard and the unchanged behaviour of the other
roles and platforms. The ``*_cisco_xe_lisp_ospf_ibgp`` goldens (a fabric whose overlay_control_plane is ``lisp``) are
covered in section 5: the guard's refusals that still apply there, and the LISP rendering of those inputs.
"""

from __future__ import annotations

import copy
import json
import re
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jinja2
import pytest

from tests.smoke.test_configs_smoke import _run_transform
from transforms.common import BaseDeviceTransform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.edge import Edge
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR
from transforms.helpers.cisco_xe import get_cisco_xe_acls, get_cisco_xe_vrf_routes, refuse_unfiltered_ipv6
from transforms.helpers.vxlan import get_vxlan_config
from utils.data_cleaning import clean_data

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "smoke" / "configs"

BGP_VARIANTS = ["ebgp_ebgp", "ebgp_ibgp", "ospf_ibgp"]
FABRIC_ROLES = ["leaf", "access-leaf", "border-leaf"]

# (golden directory prefix, transform class, device_role) of the three roles with a cisco_xe fabric template.
FABRIC_TRANSFORMS: list[tuple[str, type[BaseDeviceTransform], str]] = [
    ("leaf", Leaf, "leaf"),
    ("access_leaf", AccessLeaf, "access-leaf"),
    ("border_leaf", BorderLeaf, "border-leaf"),
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


def _golden_input(directory: str) -> dict[str, Any]:
    """Load a smoke golden's input.json (a fresh deep copy; the transform mutates what it is given)."""
    return copy.deepcopy(_load_json(directory))


@cache
def _load_json(directory: str) -> dict[str, Any]:
    return json.loads((CONFIGS_DIR / directory / "input.json").read_text())


@cache
def _render(prefix: str, variant: str) -> str:
    """Render the cisco_xe golden input of ``<prefix>_cisco_xe_<variant>`` with its transform."""
    transform_cls = next(cls for pfx, cls, _role in FABRIC_TRANSFORMS if pfx == prefix)
    return _run_transform(transform_cls, _golden_input(f"{prefix}_cisco_xe_{variant}"))


def _make_transform(device_role: str) -> BaseDeviceTransform:
    """Instantiate BaseDeviceTransform bypassing InfrahubTransform __init__."""
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    transform.device_role = device_role
    transform.template_subdir = "leafs"
    transform.root_directory = "/fake/root"
    return transform


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
# 5. LISP overlay fabric: the *_cisco_xe_lisp_ospf_ibgp goldens
# ---------------------------------------------------------------------------

LISP_VARIANT = "lisp_ospf_ibgp"
LISP_MARKER = "! unverified-no-lab:"
# The map-servers of the golden inputs, in the sorted-by-device-name order the template renders them.
LISP_MAP_SERVER_ADDRESSES = ["10.255.0.1", "10.255.0.2"]


def _lisp_deployment(prefix: str) -> dict[str, Any]:
    """The cleaned `deployment` object of a LISP golden input (what the transforms read the LISP keys from)."""
    cleaned = clean_data(_golden_input(f"{prefix}_cisco_xe_{LISP_VARIANT}"))
    return cleaned["DcimDevice"][0]["deployment"]


@pytest.mark.parametrize(("prefix", "transform_cls", "role"), FABRIC_TRANSFORMS)
class TestCiscoXeLispGoldenInputs:
    """The LISP golden inputs carry the keys of queries/fragments/lisp_fabric.gql in their cleaned shape."""

    def test_input_carries_the_fabric_flag_map_servers_and_site_key(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """overlay_control_plane, lisp_map_servers[i].interfaces[j].ip_address.address and lisp_site_key.password."""
        deployment = _lisp_deployment(prefix)

        assert deployment["overlay_control_plane"] == "lisp"
        assert deployment["name"] == "DC-1"
        assert deployment["lisp_site_key"] == {"password": "s3cr3t"}
        servers = deployment["lisp_map_servers"]
        assert [server["name"] for server in servers] == ["dc1-border-leaf-02", "dc1-border-leaf-01"]
        assert [server["interfaces"][0]["ip_address"]["address"] for server in servers] == [
            "10.255.0.2/32",
            "10.255.0.1/32",
        ]

    def test_input_has_no_bgp_process(self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str) -> None:
        """A LISP fabric has no BGP overlay: the ManagedBGP capability is gone, only OSPF is left."""
        node = _golden_input(f"{prefix}_cisco_xe_{LISP_VARIANT}")["DcimDevice"]["edges"][0]["node"]

        kinds = {edge["node"].get("__typename") for edge in node["capabilities"]["edges"]}
        assert kinds == {"ManagedOSPF"}


@pytest.mark.parametrize(("prefix", "transform_cls", "role"), FABRIC_TRANSFORMS)
class TestCiscoXeLispRendering:
    """The three fabric roles render the LISP overlay, never the EVPN-VXLAN one, from the same inputs."""

    def test_renders_lisp_and_no_evpn_vxlan_or_bgp(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """`router lisp` with the RLOC and instance-ids; no l2vpn evpn, nve1, vlan configuration or router bgp."""
        output = _render(prefix, LISP_VARIANT)

        assert f"! Cisco IOS-XE (Catalyst 9000) LISP {role}\n" in output
        assert re.search(r"^router lisp$", output, re.M)
        assert " IPv4-interface Loopback0 priority 10 weight 10\n" in output
        assert " instance-id 50001\n" in output
        assert " instance-id 10100\n" in output
        assert re.search(r"^ lisp mobility VRF_A-IPV4$", output, re.M)
        for absent in ("l2vpn evpn", "nve1", "vlan configuration", "router bgp", "RM-CONN-2-BGP"):
            assert absent not in output, absent
        assert "router ospf 1" in output

    def test_every_edge_registers_with_every_map_server_in_name_order(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """The inputs list the map-servers out of order; the key and proxy-reply follow each, sorted by device name."""
        output = _render(prefix, LISP_VARIANT)

        for service in ("ipv4", "ethernet"):
            block = output.split(f" service {service}\n", 1)[1].split(f"  exit-service-{service}", 1)[0]
            registered = re.findall(r"^  etr map-server (\S+) key s3cr3t$", block, re.M)
            resolvers = re.findall(r"^  itr map-resolver (\S+)$", block, re.M)
            assert registered == LISP_MAP_SERVER_ADDRESSES
            assert resolvers == LISP_MAP_SERVER_ADDRESSES

    def test_every_lisp_command_is_under_an_unverified_no_lab_comment(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """No lab verified any of it: each LISP block (and the VRF / SVI lines it adds) opens with the marker.

        The gateway SVIs are one section: its marker above the first SVI covers every `lisp mobility` line under it.
        """
        lines = _render(prefix, LISP_VARIANT).splitlines()
        covered = False
        uncovered: list[str] = []
        in_lisp = False
        in_svi_section = False
        for line in lines:
            if line.lstrip().startswith(LISP_MARKER):
                covered = True
                continue
            if line == "! Gateway SVIs":
                in_svi_section = True
            elif line == "! Management":
                in_svi_section = False
            if line == "!" and not in_lisp and not in_svi_section:
                covered = False
            if line == "router lisp":
                in_lisp = True
            elif in_lisp and not line.startswith(" "):
                in_lisp = False
            lisp_line = in_lisp or line.startswith("vrf definition") or line.strip().startswith("lisp mobility")
            if lisp_line and not covered:
                uncovered.append(line)
        assert not uncovered, uncovered

    def test_only_the_border_leaf_is_a_map_server_with_a_site(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """The border leaf is the co-located map-server/resolver and registers the DC's EIDs; edges do neither."""
        output = _render(prefix, LISP_VARIANT)

        if role == "border-leaf":
            assert output.count("  map-server\n") == 2
            assert output.count("  map-resolver\n") == 2
            assert " site DC-1\n" in output
            assert "  authentication-key s3cr3t\n" in output
            assert "  eid-record instance-id 50001 10.100.0.0/24 accept-more-specifics\n" in output
            assert "  eid-record instance-id 10100 any-mac\n" in output
        else:
            assert "  map-server\n" not in output
            assert "  map-resolver\n" not in output
            assert " site " not in output
            assert "eid-record" not in output


def test_lisp_access_leaf_renders_the_leaf_config_but_for_the_header() -> None:
    """The access-leaf and leaf LISP inputs describe the same fabric: only the identity lines differ."""
    leaf = _render("leaf", LISP_VARIANT).splitlines()
    other = _render("access_leaf", LISP_VARIANT).splitlines()

    assert len(leaf) == len(other)
    differing = [i for i, (a, b) in enumerate(zip(leaf, other, strict=True)) if a != b]
    assert differing
    assert all(i < 6 for i in differing)


@pytest.mark.parametrize(("prefix", "transform_cls", "role"), FABRIC_TRANSFORMS)
class TestCiscoXeLispRefusalThroughTransform:
    """The guard still runs inside transform() on a LISP fabric: the services its templates lack are refused."""

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
                _run_transform(transform_cls, _golden_input(f"{prefix}_cisco_xe_{LISP_VARIANT}"))

    def test_lisp_transform_refuses_a_platform_without_a_lisp_renderer(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """The same input on a platform that cannot render LISP is a refusal, not an EVPN config."""
        data = _with_platform(_golden_input(f"{prefix}_cisco_xe_{LISP_VARIANT}"), "arista_eos")

        with pytest.raises(ValueError) as excinfo:
            _run_transform(transform_cls, data)

        assert str(excinfo.value) == f"arista_eos {role} cannot render the LISP overlay; refusing incomplete config"

    def test_lisp_transform_without_a_site_key_is_refused(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """No lisp_site_key on the deployment: nothing can register, so no config is rendered."""
        data = _golden_input(f"{prefix}_cisco_xe_{LISP_VARIANT}")
        del data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["lisp_site_key"]

        with pytest.raises(ValueError, match=f"cisco_xe {role} cannot render the LISP overlay: the fabric has no LISP"):
            _run_transform(transform_cls, data)

    def test_lisp_transform_without_a_map_server_is_refused(
        self, prefix: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """No border leaf to register with: refuse instead of rendering an unreachable ETR."""
        data = _golden_input(f"{prefix}_cisco_xe_{LISP_VARIANT}")
        data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["lisp_map_servers"] = {"edges": []}

        with pytest.raises(
            ValueError, match=f"cisco_xe {role} cannot render the LISP overlay: the fabric has no map-server"
        ):
            _run_transform(transform_cls, data)


# ---------------------------------------------------------------------------
# 6. Segment ACLs and inter-VRF statics: transforms/helpers/cisco_xe.py + the cisco_xe_acl / cisco_xe_vrf_static templates
# ---------------------------------------------------------------------------

ACL_EVPN = "leaf_cisco_xe_with_acl"
ACL_LISP = "leaf_cisco_xe_lisp_with_acl"
EXCHANGE_ACL = "leaf_cisco_xe_exchange_acl"


def _rule(**overrides: Any) -> dict[str, Any]:
    """One rule of the neutral ACL model (transforms/helpers/acl.py), permit tcp any any by default."""
    return {
        "seq": 10,
        "action": "permit",
        "protocol": "tcp",
        "src": "any",
        "src_port": None,
        "dst": "any",
        "dst_port": None,
        "established": False,
        "log": False,
        "name": "",
        "src_zone": None,
        "dst_zone": None,
        "src_customer": None,
        "src_environment": None,
        "dst_customer": None,
        "dst_environment": None,
        **overrides,
    }


def _acl(*rules: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": "ACL-VLAN100-IN", "vlan_id": 100, "segment_name": "seg", "rules": list(rules)}]


def _entries(*rules: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-remark lines of the IOS-XE ACL built from `rules`."""
    return [line for line in get_cisco_xe_acls(_acl(*rules), "leaf")[0]["lines"] if "remark" not in line]


def _first_policy_rule(data: dict[str, Any]) -> dict[str, Any]:
    """The raw first rule of the policy of the first segment carried by Ethernet10 of a golden input."""
    return _host_segment(data)["security_policy"]["node"]["rules"]["edges"][0]["node"]


def _host_segment(data: dict[str, Any]) -> dict[str, Any]:
    """The raw first segment node carried by Ethernet10 of a golden input."""
    interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
    return port["interface_capabilities"]["edges"][0]["node"]


class TestCiscoXeAclTranslation:
    """get_cisco_xe_acls: the neutral ACL model in IOS-XE `ip access-list extended` fields."""

    def test_action_protocol_and_any(self) -> None:
        """permit/deny, ip/tcp/udp/icmp and `any` pass straight through."""
        entries = _entries(
            _rule(action="deny", protocol="ip"), _rule(protocol="udp"), _rule(protocol="icmp", action="deny")
        )

        assert [(e["action"], e["protocol"], e["src"], e["dst"]) for e in entries] == [
            ("deny", "ip", "any", "any"),
            ("permit", "udp", "any", "any"),
            ("deny", "icmp", "any", "any"),
        ]

    @pytest.mark.parametrize(
        ("cidr", "expected"),
        [
            ("10.100.0.0/24", "10.100.0.0 0.0.0.255"),
            ("10.0.0.0/8", "10.0.0.0 0.255.255.255"),
            ("192.168.1.128/25", "192.168.1.128 0.0.0.127"),
            ("10.9.9.9/32", "host 10.9.9.9"),
            ("10.9.9.9", "host 10.9.9.9"),
            ("0.0.0.0/0", "any"),
        ],
    )
    def test_prefixes_become_wildcard_masks_host_or_any(self, cidr: str, expected: str) -> None:
        """A CIDR is `network wildcard`, a /32 is `host`, a /0 is `any`; both ends are translated."""
        entry = _entries(_rule(src=cidr, dst=cidr))[0]

        assert entry["src"] == expected
        assert entry["dst"] == expected

    def test_ports_and_ranges_keep_their_side(self) -> None:
        """`eq N` / `range N M` stay on the side the model put them on, `established` and `log` are carried."""
        entries = _entries(
            _rule(dst_port="eq 443"),
            _rule(protocol="udp", dst_port="range 5000 5010"),
            _rule(src_port="eq 5432", established=True, log=True),
        )

        assert [(e["src_port"], e["dst_port"], e["established"], e["log"]) for e in entries] == [
            (None, "eq 443", False, False),
            (None, "range 5000 5010", False, False),
            ("eq 5432", None, True, True),
        ]

    def test_lines_are_numbered_in_rule_order_and_remarks_get_their_own_number(self) -> None:
        """10, 20, ... in list order; the neutral seqs (consecutive ones here) cannot collide with a remark."""
        lines = get_cisco_xe_acls(
            _acl(
                _rule(seq=1, name="a", src_zone="prod", dst_zone="web"),
                _rule(seq=2, name="b"),
                _rule(seq=9990, name="implicit-deny-all", action="deny", protocol="ip"),
            ),
            "leaf",
        )[0]["lines"]

        assert [line["seq"] for line in lines] == [10, 20, 30, 40, 50, 60, 70]
        assert [line.get("remark") for line in lines] == [
            "Zone: prod -> web",
            "Rule: a",
            None,
            "Rule: b",
            None,
            "Rule: implicit-deny-all",
            None,
        ]

    def test_remarks_carry_zone_customer_and_rule_name_in_that_order(self) -> None:
        """Zone, customer/environment and the rule name each get a remark before their entry."""
        lines = get_cisco_xe_acls(
            _acl(
                _rule(
                    name="r1",
                    src_zone="prod",
                    dst_zone=None,
                    src_customer="Acme",
                    src_environment="prod",
                    dst_customer="Beta",
                )
            ),
            "leaf",
        )[0]["lines"]

        assert [line.get("remark") for line in lines] == [
            "Zone: prod -> any",
            "Customer: Acme/prod -> Beta/any",
            "Rule: r1",
            None,
        ]

    def test_a_remark_is_one_line_of_at_most_100_characters(self) -> None:
        """A rule name with a newline or an overlong name cannot start a second config line or exceed IOS's limit."""
        lines = get_cisco_xe_acls(_acl(_rule(name="evil\nip access-list extended X " + "y" * 200)), "leaf")[0]["lines"]

        remark = lines[0]["remark"]
        assert "\n" not in remark
        assert len(remark) == 100
        assert remark.startswith("Rule: evil ip access-list extended X ")

    def test_empty_input_gives_no_acls(self) -> None:
        """No ACLs in, none out."""
        assert get_cisco_xe_acls(None, "leaf") == []
        assert get_cisco_xe_acls([], "leaf") == []

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            (_rule(src="2001:db8::/32"), "'2001:db8::/32' is IPv6"),
            (_rule(dst="2001:db8::1"), "'2001:db8::1' is IPv6"),
            (_rule(dst="10.1.1.10/24"), "'10.1.1.10/24' is not an IPv4 network address"),
            (_rule(dst="web-servers"), "'web-servers' is not an IPv4 network address"),
            (_rule(protocol="gre"), "protocol 'gre'"),
            (_rule(action="reject"), "action 'reject'"),
            (_rule(dst_port="gt 1024"), "port match 'gt 1024'"),
            (_rule(dst_port="range 100 10"), "port match 'range 100 10' is out of range"),
            (_rule(dst_port="eq 70000"), "port match 'eq 70000' is out of range"),
            (_rule(protocol="icmp", dst_port="eq 80"), "a port match on protocol 'icmp'"),
            (_rule(protocol="udp", established=True), "`established` on protocol 'udp'"),
        ],
    )
    def test_a_rule_that_cannot_be_expressed_is_refused_not_dropped(self, rule: dict[str, Any], message: str) -> None:
        """Every case names the ACL and the rule and ends the way the other cisco_xe refusals do."""
        with pytest.raises(ValueError) as excinfo:
            get_cisco_xe_acls(_acl(_rule(name="ok"), {**rule, "name": "bad-rule"}), "leaf")

        text = str(excinfo.value)
        assert text.startswith("cisco_xe leaf cannot render ACL ACL-VLAN100-IN rule 'bad-rule': ")
        assert message in text
        assert text.endswith("; refusing incomplete config")


class TestCiscoXeVrfRoutes:
    """get_cisco_xe_vrf_routes: the exchange statics as `ip route vrf` arguments."""

    ROUTES = [
        {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4"},
        {"vrf": "INTERNET", "l3_vni": 50003, "prefix": "192.0.2.5/32", "nexthop": "100.66.32.4", "local_only": True},
    ]

    def test_dotted_masks_and_exported_flag(self) -> None:
        """/0 and /32 become 0.0.0.0 and 255.255.255.255; `local_only` is not exported."""
        routes = get_cisco_xe_vrf_routes(self.ROUTES, ["PROD", "INTERNET"], "leaf", lisp=False)

        assert [(r["vrf"], r["network"], r["mask"], r["nexthop"], r["exported"]) for r in routes] == [
            ("PROD", "0.0.0.0", "0.0.0.0", "100.66.0.4", True),
            ("INTERNET", "192.0.2.5", "255.255.255.255", "100.66.32.4", False),
        ]

    def test_no_routes(self) -> None:
        """No exchange statics in, none out."""
        assert get_cisco_xe_vrf_routes(None, [], "leaf", lisp=False) == []

    def test_a_vrf_the_device_does_not_define_is_refused(self) -> None:
        """The `ip route vrf` line would be rejected by the device."""
        with pytest.raises(ValueError, match="the VRF is not defined on this device; refusing incomplete config"):
            get_cisco_xe_vrf_routes(self.ROUTES, ["PROD"], "leaf", lisp=False)

    @pytest.mark.parametrize(
        "route",
        [
            {"vrf": "PROD", "prefix": "::/0", "nexthop": "100.66.0.4"},
            {"vrf": "PROD", "prefix": "0.0.0.0/0", "nexthop": "2001:db8::1"},
        ],
    )
    def test_ipv6_is_refused(self, route: dict[str, Any]) -> None:
        """An IPv6 prefix or next hop has no `ip route` form."""
        with pytest.raises(ValueError, match="cisco_xe leaf cannot render the VRF PROD static .*: IPv6"):
            get_cisco_xe_vrf_routes([route], ["PROD"], "leaf", lisp=False)

    def test_an_exported_static_is_refused_on_a_lisp_fabric(self) -> None:
        """LISP has no redistribution into the VRF: an exported static would stay local, silently."""
        with pytest.raises(ValueError, match="a LISP fabric cannot redistribute an exchange static"):
            get_cisco_xe_vrf_routes(self.ROUTES, ["PROD", "INTERNET"], "leaf", lisp=True)

    def test_a_local_only_static_is_rendered_on_a_lisp_fabric(self) -> None:
        """A static that is not exported needs no redistribution: LISP renders it as a plain `ip route`."""
        routes = get_cisco_xe_vrf_routes(self.ROUTES[1:], ["INTERNET"], "leaf", lisp=True)

        assert [r["exported"] for r in routes] == [False]


class TestCiscoXeIpv6SviIsRefused:
    """An IPv6 gateway SVI cannot be protected by the IPv4 segment ACL."""

    def test_an_ipv6_gateway_svi_with_an_acl_is_refused(self) -> None:
        """An `ip access-group` ACL never filters an IPv6 SVI: refuse rather than ship it open."""
        with pytest.raises(ValueError, match="the segment ACL of VLAN 300: its gateway SVI is IPv6"):
            refuse_unfiltered_ipv6(
                [
                    {"vlan_id": 100, "acl_name": "ACL-VLAN100-IN", "gateway_ipv6": None},
                    {"vlan_id": 300, "acl_name": "ACL-VLAN300-IN", "gateway_ipv6": "2001:db8::1/64"},
                ],
                "leaf",
            )

    def test_an_ipv6_svi_without_an_acl_and_ipv4_svis_pass(self) -> None:
        """No ACL on the IPv6 SVI (no security policy in the query): nothing to protect, nothing to refuse."""
        refuse_unfiltered_ipv6(
            [
                {"vlan_id": 100, "acl_name": "ACL-VLAN100-IN", "gateway_ipv6": None},
                {"vlan_id": 300, "acl_name": None, "gateway_ipv6": "2001:db8::1/64"},
            ],
            "leaf",
        )

    def test_through_transform(self) -> None:
        """An IPv6 gateway on the ACL-protected segment makes transform() raise."""
        data = _golden_input(ACL_EVPN)
        gateway = _host_segment(data)["gateway"]["node"]
        gateway["address"]["value"] = "2001:db8:100::1/64"
        gateway["ip_prefix"]["node"]["prefix"]["value"] = "2001:db8:100::/64"

        with pytest.raises(
            ValueError, match="cisco_xe leaf cannot render the segment ACL of VLAN 100: its gateway SVI"
        ):
            _run_transform(Leaf, data)


class TestCiscoXeAclRenderingThroughTransform:
    """The golden inputs with a security policy render ACLs on the EVPN and the LISP path."""

    @pytest.mark.parametrize("golden", [ACL_EVPN, ACL_LISP])
    def test_acl_is_defined_before_the_svi_that_binds_it(self, golden: str) -> None:
        """`ip access-list extended` precedes `interface Vlan<N>` and `ip access-group ... in` names the same ACL."""
        output = _run_transform(Leaf, _golden_input(golden))

        for vlan in (100, 200):
            name = f"ACL-VLAN{vlan}-IN"
            assert output.index(f"ip access-list extended {name}\n") < output.index(f"interface Vlan{vlan}\n")
            assert re.search(rf"^interface Vlan{vlan}\n(?: .*\n)*? ip access-group {name} in\n", output, re.M)

    @pytest.mark.parametrize("golden", [ACL_EVPN, ACL_LISP])
    def test_entries_use_wildcards_ports_remarks_and_the_closing_deny(self, golden: str) -> None:
        """The shapes IOS-XE writes differently from NX-OS/EOS: wildcard masks, host, ranges, established, remarks."""
        output = _run_transform(Leaf, _golden_input(golden))

        for expected in (
            " 20 permit tcp any any eq 443\n",
            " 70 permit udp any 10.200.0.0 0.0.0.255 range 5000 5010\n",
            " 90 permit icmp any any\n",
            " 110 permit tcp any host 10.9.9.9 eq 5432 log\n",
            " 130 deny ip any any log\n",
            " 50 remark Zone: any -> web\n",
            " 50 permit tcp 10.200.0.0 0.0.0.255 eq 8443 10.100.0.0 0.0.0.255 established\n",
        ):
            assert expected in output, expected
        assert not re.search(r"^ \d+ (permit|deny) .*\d+\.\d+\.\d+\.\d+/\d+", output, re.M)

    @pytest.mark.parametrize("golden", [ACL_EVPN, ACL_LISP])
    def test_the_acl_blocks_are_marked_unverified_no_lab(self, golden: str) -> None:
        """No lab has run an IOS-XE ACL: the ACL section and the SVI binding say so."""
        output = _run_transform(Leaf, _golden_input(golden))

        section = output.split("! Zero-Trust ACLs", 1)[1].split("ip access-list extended", 1)[0]
        assert "! unverified-no-lab:" in section
        assert "! unverified-no-lab: `ip access-group <name> in`" in output

    @pytest.mark.parametrize(
        ("golden", "plain"),
        [(ACL_EVPN, "leaf_cisco_xe_ebgp_ibgp"), (ACL_LISP, "leaf_cisco_xe_lisp_ospf_ibgp")],
    )
    def test_the_only_difference_from_the_policy_free_golden_is_the_acl(self, golden: str, plain: str) -> None:
        """Same hosted segments, with and without a security policy: dropping the ACL lines gives the plain config."""
        with_acl = _run_transform(Leaf, _golden_input(golden))
        stripped = re.sub(r"! Zero-Trust ACLs.*?(?=! Interfaces)", "", with_acl, flags=re.S)
        stripped = re.sub(
            r"^ ip access-group .*\n|^! unverified-no-lab: `ip access-group.*\n", "", stripped, flags=re.M
        )

        assert stripped == _run_transform(Leaf, _golden_input(plain))

    def test_the_lisp_path_still_renders_the_instances_of_the_hosted_segment(self) -> None:
        """Hosts behind an ACL are still registered: L3 instance-id for the VRF, L2 for the VNI, mobility on the SVI."""
        output = _run_transform(Leaf, _golden_input(ACL_LISP))

        assert re.search(r"^router lisp$", output, re.M)
        assert " instance-id 50001\n" in output
        assert " instance-id 10100\n" in output
        assert re.search(r"^ lisp mobility VRF_A-IPV4$", output, re.M)
        for absent in ("l2vpn evpn", "nve1", "router bgp"):
            assert absent not in output, absent

    @pytest.mark.parametrize("golden", [ACL_EVPN, ACL_LISP])
    def test_an_ipv6_rule_endpoint_is_refused_through_transform(self, golden: str) -> None:
        """A rule whose destination is an IPv6 prefix cannot go into an `ip access-list extended`."""
        data = _golden_input(golden)
        _first_policy_rule(data)["destination_ip_addresses"] = {
            "edges": [{"node": {"address": {"value": "2001:db8::/32"}}}]
        }

        with pytest.raises(ValueError) as excinfo:
            _run_transform(Leaf, data)

        assert str(excinfo.value).startswith("cisco_xe leaf cannot render ACL ACL-VLAN100-IN rule 'allow-https': ")
        assert "IPv6" in str(excinfo.value)

    def test_access_leaf_renders_the_acl_too(self) -> None:
        """The access-leaf shares the leaf template: the same input renders the same ACL block."""
        data = _golden_input(ACL_EVPN)
        data["DcimDevice"]["edges"][0]["node"]["role"]["value"] = "access-leaf"

        output = _run_transform(AccessLeaf, data)

        assert "ip access-list extended ACL-VLAN100-IN\n" in output
        assert " ip access-group ACL-VLAN100-IN in\n" in output

    def test_border_leaf_hosting_a_tenant_segment_is_still_refused_by_the_platform_independent_guard(self) -> None:
        """Every platform's border leaf refuses a routed tenant SVI with an ACL (_refuse_unprotected_tenant_svis)."""
        data = _golden_input(ACL_EVPN)
        data["DcimDevice"]["edges"][0]["node"]["role"]["value"] = "border-leaf"

        with pytest.raises(ValueError, match="cisco_xe border-leaf cannot render the SVI ACL for VLANs"):
            _run_transform(BorderLeaf, data)

    def test_pbr_is_still_refused_with_acls_present(self) -> None:
        """Lifting the ACL refusal does not lift PBR: a PBR rule on a device with ACLs still raises."""
        real = BaseDeviceTransform._template_context

        def with_pbr(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {**real(self, *args, **kwargs), "leaf_pbr_rules": [{"vlan_id": 100}]}

        with patch.object(BaseDeviceTransform, "_template_context", with_pbr):
            with pytest.raises(ValueError, match="cisco_xe leaf cannot render: leaf_pbr_rules"):
                _run_transform(Leaf, _golden_input(ACL_EVPN))

    def test_other_platforms_and_roles_get_no_cisco_xe_context(self) -> None:
        """The cisco_xe keys exist only on a cisco_xe fabric device; an IPv6 rule is not this guard's business elsewhere."""
        config = {"acls": _acl(_rule(src="2001:db8::/32"))}

        for platform in ("arista_eos", "cisco_nxos"):
            assert _make_transform("leaf")._cisco_xe_context(platform, config) == {}
        assert _make_transform("spine")._cisco_xe_context("cisco_xe", config) == {}


class TestCiscoXeVrfStaticRendering:
    """The exchange statics: `ip route vrf` lines, and for exported ones the prefix-list, route-map and redistribution."""

    def test_statics_are_rendered_with_dotted_masks(self) -> None:
        """The exchange statics of the golden (PROD 0/0 to the shared context VIP, the SNAT /32s in INTERNET)."""
        output = _run_transform(Leaf, _golden_input(EXCHANGE_ACL))

        for expected in (
            "ip route vrf INTERNET 192.0.2.5 255.255.255.255 100.66.32.4\n",
            "ip route vrf INTERNET 192.0.2.6 255.255.255.255 100.66.32.12\n",
            "ip route vrf PROD 0.0.0.0 0.0.0.0 100.66.0.4\n",
        ):
            assert expected in output, expected
        assert not re.search(r"^ip route vrf \S+ \S+/\d+", output, re.M)

    def test_exported_statics_are_redistributed_into_the_vrf_address_family_by_an_exact_match_route_map(self) -> None:
        """Exchange statics enter EVPN as type-5 routes, exact match only, only in the VRFs that have one."""
        output = _run_transform(Leaf, _golden_input(EXCHANGE_ACL))

        assert "ip prefix-list PL-VRF-STATIC-PROD seq 10 permit 0.0.0.0/0\n" in output
        assert (
            "route-map RM-VRF-STATIC-2-EVPN-PROD permit 10\n match ip address prefix-list PL-VRF-STATIC-PROD\n"
            in output
        )
        assert re.search(
            r"^ address-family ipv4 vrf PROD\n  advertise l2vpn evpn\n  redistribute connected\n"
            r"  redistribute static route-map RM-VRF-STATIC-2-EVPN-PROD\n exit-address-family$",
            output,
            re.M,
        )
        assert "RM-VRF-STATIC-2-EVPN-MANAGEMENT" not in output

    def test_a_local_only_static_is_neither_prefix_listed_nor_redistributed(self) -> None:
        """The edge-style `local_only` default route stays a static inside its VRF."""
        real = BaseDeviceTransform._template_context

        def local_only(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            config = real(self, *args, **kwargs)
            config["vrf_routes"] = [{**route, "local_only": True} for route in config["vrf_routes"]]
            return config

        with patch.object(BaseDeviceTransform, "_template_context", local_only):
            output = _run_transform(Leaf, _golden_input(EXCHANGE_ACL))

        assert "ip route vrf PROD 0.0.0.0 0.0.0.0 100.66.0.4\n" in output
        assert "PL-VRF-STATIC" not in output
        assert "redistribute static" not in output

    def test_a_static_in_an_undefined_vrf_is_refused_through_transform(self) -> None:
        """A static whose VRF the device does not define would be rejected by it: refuse instead."""
        real = BaseDeviceTransform._template_context

        def stray_vrf(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            config = real(self, *args, **kwargs)
            config["vrf_routes"] = [*config["vrf_routes"], {**config["vrf_routes"][0], "vrf": "GHOST"}]
            return config

        with patch.object(BaseDeviceTransform, "_template_context", stray_vrf):
            with pytest.raises(ValueError, match="the VRF GHOST static .*: the VRF is not defined on this device"):
                _run_transform(Leaf, _golden_input(EXCHANGE_ACL))

    def test_border_leaf_renders_the_same_statics_without_an_acl(self) -> None:
        """The border-leaf golden has the statics and no security policy: no ACL, no access-group."""
        output = _run_transform(BorderLeaf, _golden_input("border_leaf_cisco_xe_exchange_transit"))

        assert "ip route vrf PROD 0.0.0.0 0.0.0.0 100.66.0.4\n" in output
        assert "ip access-list extended" not in output
        assert "ip access-group" not in output
