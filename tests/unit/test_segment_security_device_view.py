"""Unit tests for the EOS MSS-G device view (transforms/helpers/group_policy/ _segment_security_build),
its dispatcher (group_policy_device_view) and the EOS leaf / border-leaf templates that render it.

The fleet is the smoke fixture's (tests/smoke/fixture_builders/group_policy.py ``_gpo_fleet_roots``) on 7280R3 VTEPs at
4.36.2F: DC10 (enforcing) with a leaf, an access leaf and a border leaf in the EOS row, a legacy DC12, NONPROD
(web-np, app-np, db-np; web-np reaches the internet, db-np is stretched to DC12) and PROD (pay-p, microsegmented,
whose contracts name component groups, and portal-p).
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from jinja2 import Template

from tests.smoke.fixture_builders.common import run_transform
from tests.smoke.fixture_builders.group_policy import (
    _GPO_EOS,
    _GPO_NXOS,
    _GPO_PAY_DB_TAGS,
    GPO_FIXTURES,
    _gp_device,
    _gp_memory_gb,
    _gp_rule,
    _gp_segment,
    _gpo_golden_leaf_data,
    build_gpo_device_data,
    gpo_roots,
    gpo_segment,
    gpo_view,
)
from transforms.common import BaseDeviceTransform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.helpers.group_policy import (
    _BUILDERS,
    GPO_CONFIG_KEYS,
    GPO_RENDERERS,
    MSSG_MAX_SEGMENTS,
    group_policy_device_view,
)
from transforms.helpers.vxlan import get_interfaces
from utils.data_cleaning import clean_data
from utils.fabric_roles import DC_VTEP_ROLES

LEAF_SEGMENTS = ("web-np", "app-np", "pay-p", "portal-p")
VRFS = ("NONPROD", "PROD")
VTEPS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "Leaf": ("leaf", "dc1-leaf-01", LEAF_SEGMENTS),
    "BorderLeaf": ("border-leaf", "dc1-border-leaf-01", ("web-np", "app-np", "db-np", "pay-p", "portal-p")),
    "AccessLeaf": ("access-leaf", "dc1-access-leaf-01", ("app-np", "db-np", "portal-p")),
}
FORWARD_ALL = "policy-forward-all"
DROP_ALL = "policy-drop-all"


def _raw(
    role: str = "leaf", name: str = "dc1-leaf-01", segments: tuple[str, ...] = LEAF_SEGMENTS, **kwargs: Any
) -> dict:
    """The raw query response of an EOS VTEP of DC10, with the fleet roots."""
    kwargs.setdefault("fleet", _GPO_EOS)
    kwargs.setdefault("version", "4.36.2F")
    return build_gpo_device_data(device_name=name, role=role, segments=segments, **kwargs)


def _roots(raw: dict | None = None) -> dict[str, Any]:
    """The cleaned fleet roots (everything but the device root)."""
    return gpo_roots(raw or _raw())


def _view(roots: dict[str, Any], **overrides: Any) -> dict[str, Any] | None:
    arguments: dict[str, Any] = {
        "device_name": "dc1-leaf-01",
        "role": "leaf",
        "platform": "arista_eos",
        "activations": (),
        "vrfs": VRFS,
    }
    return gpo_view("segment_security", roots, **{**arguments, **overrides})


def _good(roots: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    view = _view(roots or _roots(), **overrides)
    assert view is not None
    return view


def _clean_segment(**kwargs: Any) -> dict[str, Any]:
    """A cleaned ``_gp_segment`` (a local NONPROD segment of DC10 unless told otherwise)."""
    kwargs.setdefault("namespace", ("NONPROD", "non_prod"))
    kwargs.setdefault("zone", "NONPROD-ZONE")
    segment = _gp_segment(**kwargs)
    return clean_data({"x": {"edges": [{"node": segment}]}})["x"][0]


def _add_segment(
    roots: dict[str, Any],
    name: str,
    prefix: str | None,
    *,
    group_id: int,
    rules: list[dict] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    segment = _clean_segment(
        seg_id=f"seg-{name}", name=name, owner="C020", prefix=prefix, rules=rules, tag=(name, group_id), **kwargs
    )
    roots["ManagedNetworkSegment"].append(segment)
    return segment


def _segments(view: dict[str, Any], vrf: str) -> dict[str, dict[str, Any]]:
    return {segment["name"]: segment for item in view["vrfs"] if item["name"] == vrf for segment in item["segments"]}


def _from(view: dict[str, Any], vrf: str, dst: str, src: str) -> str | None:
    """The policy of ``from src`` under the destination ``dst`` (None: no such line)."""
    return next((item["policy"] for item in _segments(view, vrf)[dst]["froms"] if item["source"] == src), None)


def _policy_lines(view: dict[str, Any], name: str) -> list[tuple[str, str | None, str | None]]:
    """(protocol or app-match-all, port side, ports) of each line of a custom policy, in order."""
    applications = {item["name"]: item for item in view["applications"]}
    field_sets = {item["name"]: item["ports"] for item in view["field_sets"]}
    policy = next(item for item in view["policies"] if item["name"] == name)
    found: list[tuple[str, str | None, str | None]] = []
    for line in policy["lines"]:
        application = applications.get(line["application"])
        if application is None:
            found.append((line["application"], None, None))
        else:
            found.append(
                (application["protocol"], application["port_side"], field_sets.get(application["field_set"] or ""))
            )
    return found


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:10]


def _block(rendered: str) -> str:
    """The segment security block of a render (the rest of the config has its own route-maps, ``match interface`` ...)."""
    start = rendered.index("! Segment security (Arista MSS-G)")
    return rendered[start : rendered.index("! End of configuration")]


def _code(text: str) -> list[str]:
    """The config lines of a text, comments dropped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("!")]


class TestGate:
    """Anything but a VTEP of a reference fabric of a kind the platform renders gets None."""

    def test_vtep_of_a_reference_fabric_renders(self) -> None:
        view = _good()
        assert [item["name"] for item in view["vrfs"]] == ["NONPROD", "PROD"]

    @pytest.mark.parametrize("role", sorted(DC_VTEP_ROLES))
    def test_every_data_center_vtep_role_renders(self, role: str) -> None:
        assert _view(_roots(), role=role) is not None

    @pytest.mark.parametrize("role", ["spine", "super-spine", "hyper-spine", "l2-leaf", "edge", "firewall", ""])
    def test_other_roles_render_nothing(self, role: str) -> None:
        assert _view(_roots(), role=role) is None

    def test_a_site_with_a_vtep_outside_its_row_is_no_reference_fabric(self) -> None:
        assert _view(_roots(_raw(leaf_type="DCS-7050CX3-32C-R"))) is None

    def test_a_release_below_the_floor_is_no_reference_fabric(self) -> None:
        assert _view(_roots(_raw(version="4.30.1F"))) is None

    def test_the_nxos_kind_renders_nothing_here(self) -> None:
        """A VXLAN GPO site: the NX-OS view applies, not this one (and vice versa)."""
        raw = _raw(fleet=_GPO_NXOS, version="10.6(3)F")
        assert _view(_roots(raw), platform="cisco_nxos") is None
        assert _view(_roots(raw)) is None
        assert (
            gpo_view(
                "vxlan_gpo",
                _roots(),
                device_name="dc1-leaf-01",
                role="leaf",
                platform="arista_eos",
                vrfs=VRFS,
                activations=[],
            )
            is None
        )

    @pytest.mark.parametrize("platform", ["cisco_nxos", "sonic", ""])
    def test_a_platform_without_a_renderer_renders_nothing(self, platform: str) -> None:
        assert _view(_roots(), platform=platform) is None

    def test_no_profile_row_is_no_reference_fabric(self) -> None:
        roots = _roots()
        roots["DcimGroupPolicyProfile"] = []
        assert _view(roots) is None

    def test_no_fleet_roots_render_nothing(self) -> None:
        assert _view({}) is None

    def test_device_in_no_data_center_renders_nothing(self) -> None:
        assert _view(_roots(), device_name="ghost-01") is None

    def test_device_in_two_data_centers_renders_nothing(self) -> None:
        roots = _roots()
        leaf = next(vtep for pod in roots["TopologyPod"] for vtep in pod["devices"] if vtep["name"] == "dc1-leaf-01")
        roots["TopologyDataCenter"][1]["devices"] = [*roots["TopologyDataCenter"][1]["devices"], leaf]
        assert _view(roots) is None

    def test_a_site_mixing_group_policy_kinds_renders_nothing(self) -> None:
        roots = _roots()
        nxos = _gp_device("dc1-nx-01", "leaf", "N9K-C9364C-GX", "10.6(3)F", _gp_memory_gb(), "cisco_nxos")
        roots["TopologyDataCenter"][0]["devices"].append(clean_data({"x": {"edges": [{"node": nxos}]}})["x"][0])
        assert _view(roots) is None

    @pytest.mark.parametrize("vrfs", [[], ["OTHER"]])
    def test_a_vtep_without_a_contract_vrf_renders_nothing(self, vrfs: list[str]) -> None:
        """No tenant VRF on the device: nothing to define, so the feature is not enabled (it halves the LPM table)."""
        assert _view(_roots(), vrfs=vrfs) is None

    def test_a_refusal_only_fires_on_a_rendering_reference_device(self) -> None:
        """61 segments are refused on an EOS VTEP, not on a device that renders none."""
        roots = _roots()
        for index in range(MSSG_MAX_SEGMENTS):
            _add_segment(roots, f"bulk-{index}", f"10.99.{index}.0/24", group_id=1000 + index)
        for overrides in ({"role": "spine"}, {"device_name": "ghost-01"}, {"platform": "sonic"}, {"vrfs": []}):
            assert _view(roots, **overrides) is None
        with pytest.raises(ValueError, match="refusing unprotected config"):
            _view(roots)
        non_reference = copy.deepcopy(roots)
        non_reference["TopologyPod"][0]["devices"][0]["device_type"]["name"] = "DCS-7050CX3-32C-R"
        assert _view(non_reference) is None


class TestDispatcher:
    """One dispatcher: the gate and the contracts once, then the builder of the kind."""

    def test_every_kind_with_a_renderer_has_a_builder_and_a_config_key(self) -> None:
        assert set(GPO_RENDERERS) == set(_BUILDERS) == set(GPO_CONFIG_KEYS)
        assert GPO_RENDERERS["segment_security"] == {"arista_eos"}
        assert GPO_CONFIG_KEYS == {"vxlan_gpo": "group_policy", "segment_security": "segment_security"}

    def test_the_eos_kind_comes_back_as_segment_security(self) -> None:
        found = group_policy_device_view(
            _roots(), device_name="dc1-leaf-01", role="leaf", platform="arista_eos", activations=[], vrfs=VRFS
        )
        assert found is not None
        assert found[0] == "segment_security"
        assert found[1] == _view(_roots())

    def test_the_nxos_kind_comes_back_as_vxlan_gpo(self) -> None:
        raw = _raw(fleet=_GPO_NXOS, version="10.6(3)F", host_ports={"Ethernet11": _GPO_PAY_DB_TAGS})
        roots = _roots(raw)
        interfaces = clean_data(raw)["DcimDevice"][0]["interfaces"]
        arguments: dict[str, Any] = {
            "device_name": "dc1-leaf-01",
            "role": "leaf",
            "platform": "cisco_nxos",
            "activations": [{"vlan_id": 110, "segment": {"id": "seg-web-np"}}],
            "vrfs": VRFS,
            "interfaces": interfaces,
        }
        found = group_policy_device_view(roots, **arguments)
        assert found is not None
        assert found[0] == "vxlan_gpo"
        assert set(found[1]) == {"class_maps", "groups", "vrfs"}
        assert gpo_view("vxlan_gpo", roots, **{**arguments, "platform": "arista_eos"}) is None

    @pytest.mark.parametrize(
        ("cls", "name"),
        [(cls, name) for cls, name, _build in GPO_FIXTURES if "nxos" in name],
    )
    def test_nxos_output_is_the_committed_golden(self, root_dir: Path, cls: type, name: str) -> None:
        """The dispatcher refactor does not change a byte of any NX-OS group policy config."""
        build = next(build for _cls, fixture, build in GPO_FIXTURES if fixture == name)
        golden = (root_dir / "tests/smoke/configs" / name / "output.txt").read_text()
        assert run_transform(cls, build()) == golden

    @staticmethod
    def _context(raw: dict, root_dir: Path) -> dict[str, Any]:
        transform = Leaf.__new__(Leaf)
        transform.root_directory = str(root_dir)
        device, roots, platform = transform._device_and_platform(copy.deepcopy(raw))
        device["segment_deployments"] = transform._device_activations(device)
        return transform._template_context(device, platform or "", roots)

    def test_the_context_key_follows_the_kind(self, root_dir: Path) -> None:
        eos = self._context(_raw(), root_dir)
        assert "segment_security" in eos and "group_policy" not in eos
        nxos = self._context(_raw(fleet=_GPO_NXOS, version="10.6(3)F"), root_dir)
        assert "group_policy" in nxos and "segment_security" not in nxos

    def test_an_eos_vtep_builds_its_interfaces_once_without_native_vlans(self, root_dir: Path) -> None:
        """The NX-OS host-port trunking is not applied to EOS (its view has no component ports)."""
        with patch("transforms.common.get_interfaces", wraps=get_interfaces) as spy:
            self._context(_raw(host_ports={"Ethernet11": _GPO_PAY_DB_TAGS}), root_dir)
        spy.assert_called_once()
        assert spy.call_args.kwargs["native_vlans"] is None

    def test_a_non_reference_device_gets_neither_key(self, root_dir: Path) -> None:
        config = self._context(_raw(leaf_type="DCS-7050CX3-32C-R"), root_dir)
        assert "segment_security" not in config and "group_policy" not in config


class TestPrefixes:
    """A segment is classified by its gateway prefix: IPv4, else IPv6; none, no segment."""

    def test_an_ipv4_prefix_makes_an_ipv4_match_list(self) -> None:
        view = _good()
        assert {"name": "web-np", "family": "ipv4", "prefix": "10.10.1.0/24"} in view["match_lists"]
        segment = _segments(view, "NONPROD")["web-np"]
        assert (segment["family"], segment["match_list"], segment["prefix"]) == ("ipv4", "web-np", "10.10.1.0/24")

    def test_an_ipv6_only_segment_makes_an_ipv6_match_list(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["gateway"]["ip_prefix"]["prefix"] = "2001:db8:10::/64"
        view = _good(roots)
        assert {"name": "web-np", "family": "ipv6", "prefix": "2001:db8:10::/64"} in view["match_lists"]
        assert _segments(view, "NONPROD")["web-np"]["family"] == "ipv6"

    def test_a_vrf_holding_a_segment_without_a_prefix_is_not_rendered_at_all(self) -> None:
        """EOS enforces box-wide: a segment that cannot be classified would be dropped to wholesale."""
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["gateway"]["ip_prefix"]["prefix"] = None
        view = _good(roots)
        assert [item["name"] for item in view["vrfs"]] == ["PROD"]
        assert [item["vrf"] for item in view["skipped"]] == ["NONPROD"]
        assert "app-np" in view["skipped"][0]["reason"]
        assert all(item["name"] not in ("web-np", "app-np", "db-np") for item in view["match_lists"])

    def test_every_vrf_skipped_still_explains_itself(self) -> None:
        roots = _roots()
        for seg_id in ("seg-app-np", "seg-pay-p"):
            gpo_segment(roots, seg_id)["gateway"]["ip_prefix"]["prefix"] = None
        view = _good(roots)
        assert view["vrfs"] == [] and view["match_lists"] == []
        assert [item["vrf"] for item in view["skipped"]] == ["NONPROD", "PROD"]

    def test_two_segments_of_one_vrf_on_one_prefix_are_refused(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["gateway"]["ip_prefix"]["prefix"] = "10.10.1.0/24"
        with pytest.raises(ValueError, match=r"share the prefix 10\.10\.1\.0/24 in VRF NONPROD.*refusing unprotected"):
            _view(roots)

    def test_a_prefix_that_is_no_network_is_refused(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["gateway"]["ip_prefix"]["prefix"] = "not-a-prefix"
        with pytest.raises(ValueError, match="refusing unprotected config"):
            _view(roots)

    def test_nested_prefixes_of_one_vrf_are_rendered_with_a_documented_not_lab_verified_comment(self) -> None:
        """Spec 2.5: a /16 and a /24 inside one VRF are expected to be longest-prefix, which nothing documents."""
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["gateway"]["ip_prefix"]["prefix"] = "10.10.0.0/16"
        segments = _segments(_good(roots), "NONPROD")
        assert len(segments["app-np"]["comments"]) == 2
        assert "10.10.3.0/24 of segment db-np" in segments["app-np"]["comments"][0]
        assert "10.10.1.0/24 of segment web-np" in segments["app-np"]["comments"][1]
        assert "10.10.0.0/16 of segment app-np" in segments["web-np"]["comments"][0]
        assert "10.10.0.0/16 of segment app-np" in segments["db-np"]["comments"][0]
        assert all(
            text.startswith("documented-not-lab-verified") for item in segments.values() for text in item["comments"]
        )

    def test_disjoint_prefixes_carry_no_overlap_comment(self) -> None:
        assert all(segment["comments"] == [] for segment in _segments(_good(), "NONPROD").values())

    def test_a_from_naming_a_source_of_another_vrf_is_commented_not_dropped_silently(self) -> None:
        """The builder never produces it from the rows (a row pairs groups of one VRF), but a view that did must show it."""
        import transforms.helpers.group_policy.eos as module

        original = module.segment_security_view

        def crossing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            view = original(*args, **kwargs)
            web = next(item for item in view["segments"] if item["name"] == "web-np")
            web["policies"].append({"from": "portal-p", "entries": [], "rules": ["stray-rule"]})
            return view

        with patch.object(module, "segment_security_view", crossing):
            view = _good()
        comments = _segments(view, "NONPROD")["web-np"]["comments"]
        assert comments == ["not rendered (source in another VRF): from portal-p (rules: stray-rule)"]
        assert _from(view, "NONPROD", "web-np", "portal-p") is None


class TestCatalogPolicy:
    """Which policy a (forward, reply) pair of entry sets gets: forward-all only from the FORWARD set."""

    @staticmethod
    def _catalog() -> Any:
        from transforms.helpers.group_policy import _MssgCatalog

        return _MssgCatalog("test")

    def test_a_forward_any_is_the_built_in(self) -> None:
        assert self._catalog().policy({("any", None, None)}, {("tcp", 22, 22)}) == FORWARD_ALL

    def test_a_reply_any_widens_nothing(self) -> None:
        catalog = self._catalog()
        name = catalog.policy({("tcp", 5432, 5432)}, {("any", None, None)})
        assert name is not None and name != FORWARD_ALL
        assert [line["action"] for line in catalog.policies[name]["lines"]] == ["forward stateless", "drop stateless"]
        assert catalog.policies[name]["text"] == "tcp/5432"

    def test_nothing_forward_and_only_a_reply_any_is_no_policy(self) -> None:
        assert self._catalog().policy(set(), {("any", None, None)}) is None
        assert self._catalog().policy(set(), set()) is None

    def test_a_reply_only_application_is_marked_reply_even_for_a_whole_protocol(self) -> None:
        catalog = self._catalog()
        name = catalog.policy(set(), {("icmp", None, None), ("tcp", 22, 22)})
        assert name is not None and catalog.policies[name]["text"] == "reply icmp; reply tcp/22"


class TestDefinitions:
    """Every segment a ``from`` line names is defined on the device, remote ones included; at most 60."""

    def test_a_remote_source_is_defined_and_named_by_a_from_line(self) -> None:
        roots = _roots()
        rule = _gp_rule("remote-to-app-tcp-443", "seg-app-np", 443)
        _add_segment(roots, "remote-np", "10.50.1.0/24", group_id=77, rules=[rule], sites=("dc12",))
        view = _good(roots)
        segments = _segments(view, "NONPROD")
        assert segments["remote-np"]["remote"] is True
        assert segments["web-np"]["remote"] is False
        assert _from(view, "NONPROD", "app-np", "remote-np") is not None
        assert {"name": "remote-np", "family": "ipv4", "prefix": "10.50.1.0/24"} in view["match_lists"]

    def test_a_remote_destination_of_a_local_source_is_defined_with_its_policy(self) -> None:
        roots = _roots()
        rule = _gp_rule("web-to-remote-tcp-22", "seg-remote-np", 22)
        gpo_segment(roots, "seg-web-np")["security_policy"]["rules"].append(
            clean_data({"x": {"edges": [{"node": rule}]}})["x"][0]
        )
        _add_segment(roots, "remote-np", "10.50.1.0/24", group_id=77, sites=("dc12",))
        view = _good(roots)
        assert _segments(view, "NONPROD")["remote-np"]["remote"] is True
        assert _from(view, "NONPROD", "remote-np", "web-np") is not None

    def test_an_unrelated_remote_segment_is_not_defined_without_a_fallback(self) -> None:
        """No row, no fallback in its VRF: the default drop already treats it as a source in no segment."""
        roots = _roots()
        _add_segment(roots, "stage-a", "10.60.1.0/24", group_id=80, namespace=("STAGE", "prod"))
        _add_segment(roots, "stage-b", "10.60.2.0/24", group_id=81, namespace=("STAGE", "prod"), sites=("dc12",))
        view = _good(roots, vrfs=("STAGE",))
        assert list(_segments(view, "STAGE")) == ["stage-a"]

    def test_a_vrf_with_a_fallback_defines_every_segment_of_the_vrf_and_drops_those_without_a_row(self) -> None:
        """The fallback also catches a source in a segment with no from line (spec 3.14): it gets an explicit drop."""
        roots = _roots()
        _add_segment(roots, "silent-np", "10.50.9.0/24", group_id=78, sites=("dc12",))
        view = _good(roots)
        web = _segments(view, "NONPROD")["web-np"]
        assert web["fallback"] is not None
        assert _from(view, "NONPROD", "web-np", "silent-np") == DROP_ALL
        assert _segments(view, "NONPROD")["silent-np"]["froms"] == [{"source": "silent-np", "policy": FORWARD_ALL}]

    def test_the_reply_of_a_flow_left_closed_is_not_rendered(self) -> None:
        """portal-p -> pay-web is left closed (a component destination): its service leg must not open portal-p."""
        view = _good()
        assert any("portal-p -> web_pay_demo_local" in text for text in view["unsupported"])
        portal = _segments(view, "PROD")["portal-p"]
        assert portal["fallback"] is None
        assert [item["source"] for item in portal["froms"]] == ["portal-p"]
        assert not any("reply tcp/443" == policy["text"] for policy in view["policies"])

    def test_the_self_of_a_microsegmented_segment_without_a_row_is_dropped_under_a_fallback(self) -> None:
        """intra deny + a fallback: the segment's own traffic must not fall to the fallback."""
        roots = _roots()
        rule = _gp_rule("pay-to-internet-tcp-443", "seg-internet", 443)
        gpo_segment(roots, "seg-pay-p")["security_policy"]["rules"].append(
            clean_data({"x": {"edges": [{"node": rule}]}})["x"][0]
        )
        view = _good(roots)
        pay = _segments(view, "PROD")["pay-p"]
        assert pay["fallback"] is not None
        assert _from(view, "PROD", "pay-p", "pay-p") == DROP_ALL

    def test_sixty_segments_render_and_sixty_one_are_refused(self) -> None:
        roots = _roots()
        for index in range(MSSG_MAX_SEGMENTS - 5):
            _add_segment(roots, f"bulk-{index}", f"10.99.{index}.0/24", group_id=1000 + index)
        view = _good(roots)
        assert len(view["match_lists"]) == MSSG_MAX_SEGMENTS
        _add_segment(roots, "bulk-last", "10.98.0.0/24", group_id=2000)
        with pytest.raises(ValueError, match=r"61 segments.*exceed the 60.*refusing unprotected config"):
            _view(roots)

    def test_a_skipped_vrf_does_not_count_against_the_limit(self) -> None:
        roots = _roots()
        for index in range(MSSG_MAX_SEGMENTS):
            segment = _add_segment(
                roots, f"bulk-{index}", f"10.99.{index}.0/24", group_id=1000 + index, namespace=("BULK", "prod")
            )
            segment["gateway"]["ip_prefix"]["prefix"] = None
        view = _good(roots, vrfs=("NONPROD", "PROD", "BULK"))
        assert [item["vrf"] for item in view["skipped"]] == ["BULK"]
        assert len(view["match_lists"]) == 5


class TestPolicies:
    """Policies under the destination: intra, forward, replies, fallback; built-ins and custom ones."""

    def test_an_intra_segment_permit_is_a_from_self_forward_all(self) -> None:
        view = _good()
        for name in ("web-np", "app-np", "db-np"):
            assert _from(view, "NONPROD", name, name) == FORWARD_ALL
        assert _from(view, "PROD", "portal-p", "portal-p") == FORWARD_ALL

    def test_a_microsegmented_segment_gets_no_intra_permit(self) -> None:
        view = _good()
        assert _from(view, "PROD", "pay-p", "pay-p") is None

    def test_an_any_row_is_the_built_in_forward_all(self) -> None:
        """db-np -> app-np is permit-all: app-np takes everything from db-np."""
        view = _good()
        assert _from(view, "NONPROD", "app-np", "db-np") == FORWARD_ALL
        assert all(item["name"] != FORWARD_ALL for item in view["policies"])

    def test_a_permit_all_reply_does_not_widen_the_forward_direction(self) -> None:
        """app-np -> db-np is tcp/5432 only and db-np -> app-np is permit-all: the reply of the latter is not
        mirrored (a stateless device matches it by source port), so db-np takes tcp/5432 from app-np and nothing else."""
        view = _good()
        name = _from(view, "NONPROD", "db-np", "app-np")
        assert name is not None and name != FORWARD_ALL
        assert _policy_lines(view, name) == [("tcp", "destination", "5432"), ("app-match-all", None, None)]

    def test_a_permit_all_reply_does_not_widen_the_fallback(self) -> None:
        """db-np -> UNCLASSIFIED is permit-all (its rule db-np-to-app-np-any): db-np's fallback stays the tcp/5432 leg."""
        fallback = _segments(_good(), "NONPROD")["db-np"]["fallback"]
        assert fallback is not None and fallback != FORWARD_ALL
        assert _policy_lines(_good(), fallback) == [("tcp", "destination", "5432"), ("app-match-all", None, None)]

    def test_the_reply_of_a_permit_all_row_is_listed_as_not_expressible_with_its_rules(self) -> None:
        texts = _good()["unsupported"]
        reason = "the reply of a permit-all flow cannot be mirrored by source port on a stateless device"
        assert any(text.startswith("db-np -> app-np: " + reason) and "db-np-to-app-np-any" in text for text in texts)
        assert any(text.startswith("db-np -> UNCLASSIFIED: " + reason) for text in texts)

    def test_a_reply_only_whole_protocol_application_is_marked_reply(self) -> None:
        """db-np -> web-np icmp: web-np answers an icmp it never asked for, so the policy comment says reply."""
        view = _good()
        name = _from(view, "NONPROD", "db-np", "web-np")
        policy = next(item for item in view["policies"] if item["name"] == name)
        assert policy["text"] == "reply icmp"

    def test_a_port_specific_row_is_a_custom_policy_ending_in_a_drop_of_everything_else(self) -> None:
        view = _good()
        name = _from(view, "NONPROD", "app-np", "web-np")
        assert name is not None and name.startswith("MSSG-POL-")
        assert _policy_lines(view, name) == [
            ("tcp", "destination", "443, 8000-8010"),
            ("udp", "source", "5000"),
            ("app-match-all", None, None),
        ]
        policy = next(item for item in view["policies"] if item["name"] == name)
        assert [(line["seq"], line["action"]) for line in policy["lines"]] == [
            (10, "forward stateless"),
            (20, "forward stateless"),
            (30, "drop stateless"),
        ]
        assert policy["lines"][-1]["application"] == "app-match-all"

    def test_the_reply_is_a_mirrored_policy_on_the_source_port_at_the_source_segment(self) -> None:
        """web-np -> app-np on tcp/443,8000-8010: the answer comes back from those ports to web-np."""
        view = _good()
        name = _from(view, "NONPROD", "web-np", "app-np")
        assert name is not None
        assert _policy_lines(view, name) == [
            ("tcp", "source", "443, 8000-8010"),
            ("udp", "destination", "5000"),
            ("app-match-all", None, None),
        ]

    def test_a_forward_only_flow_gets_its_reply_policy_and_nothing_else(self) -> None:
        roots = _roots()
        rule = _gp_rule("web-to-stage-tcp-22", "seg-stage", 22)
        gpo_segment(roots, "seg-web-np")["security_policy"]["rules"].append(
            clean_data({"x": {"edges": [{"node": rule}]}})["x"][0]
        )
        _add_segment(roots, "stage", "10.10.7.0/24", group_id=90)
        view = _good(roots)
        forward = _from(view, "NONPROD", "stage", "web-np")
        reply = _from(view, "NONPROD", "web-np", "stage")
        assert forward is not None and reply is not None and forward != reply
        assert _policy_lines(view, forward) == [("tcp", "destination", "22"), ("app-match-all", None, None)]
        assert _policy_lines(view, reply) == [("tcp", "source", "22"), ("app-match-all", None, None)]

    def test_icmp_is_a_port_less_application_both_ways(self) -> None:
        view = _good()
        name = _from(view, "NONPROD", "web-np", "db-np")
        assert name is not None
        assert ("icmp", None, None) in _policy_lines(view, name)
        applications = [item for item in view["applications"] if item["protocol"] == "icmp"]
        assert len(applications) == 1 and applications[0]["field_set"] is None

    def test_a_port_less_tcp_row_is_the_protocol_without_a_field_set(self) -> None:
        roots = _roots()
        rule = _gp_rule("web-to-stage-tcp", "seg-stage", None)
        gpo_segment(roots, "seg-web-np")["security_policy"]["rules"].append(
            clean_data({"x": {"edges": [{"node": rule}]}})["x"][0]
        )
        _add_segment(roots, "stage", "10.10.7.0/24", group_id=90)
        view = _good(roots)
        name = _from(view, "NONPROD", "stage", "web-np")
        assert name is not None
        assert _policy_lines(view, name) == [("tcp", None, None), ("app-match-all", None, None)]
        assert not [item for item in view["field_sets"] if item["ports"] == "None"]

    def test_the_fallback_of_a_destination_is_its_unclassified_entries_and_the_replies_of_its_outbound(self) -> None:
        """web-np: UNCLASSIFIED -> web-np (the legs of its inbound flows) and the replies of web-np -> internet."""
        view = _good()
        fallback = _segments(view, "NONPROD")["web-np"]["fallback"]
        assert fallback is not None
        assert _policy_lines(view, fallback) == [
            ("icmp", None, None),
            ("tcp", "source", "443, 8000-8010"),
            ("udp", "destination", "5000"),
            ("app-match-all", None, None),
        ]

    def test_a_permit_all_unclassified_leg_makes_the_fallback_the_built_in(self) -> None:
        assert _segments(_good(), "NONPROD")["app-np"]["fallback"] == FORWARD_ALL

    def test_a_segment_without_any_row_has_no_policies_block(self) -> None:
        pay = _segments(_good(), "PROD")["pay-p"]
        assert pay["froms"] == [] and pay["fallback"] is None

    def test_rows_naming_a_component_are_unsupported_and_open_nothing(self) -> None:
        """D4: the web component's rows are listed, and the microsegmented segment is not opened by them."""
        view = _good()
        assert len([text for text in view["unsupported"] if "component group" in text]) == 3
        assert any("portal-p -> web_pay_demo_local" in text for text in view["unsupported"])
        assert any("web_pay_demo_local -> db_pay_demo_local" in text for text in view["unsupported"])
        assert _from(view, "PROD", "pay-p", "portal-p") is None
        assert _segments(view, "PROD")["pay-p"]["fallback"] is None

    def test_unsupported_rows_of_a_vrf_the_device_does_not_carry_are_not_listed(self) -> None:
        texts = _good(vrfs=("NONPROD",))["unsupported"]
        assert texts and not any("component group" in text for text in texts)


class TestEnforce:
    def test_an_enforcing_data_center_enables_the_feature(self) -> None:
        view = _good()
        assert view["enforce"] is True and view["staged"] is None

    def test_a_staged_data_center_keeps_everything_but_the_enable(self) -> None:
        enforcing = _good()
        staged = _good(_roots(_raw(enforce=False)))
        assert staged["enforce"] is False
        assert "DC10 stages group policy (group_policy_enforce is false)" in staged["staged"]
        assert {**staged, "enforce": True, "staged": None} == enforcing


class TestDeterminism:
    def test_the_same_data_gives_the_same_view_whatever_the_order(self) -> None:
        roots = _roots()
        shuffled = copy.deepcopy(roots)
        generator = random.Random(7)
        generator.shuffle(shuffled["ManagedNetworkSegment"])
        for segment in shuffled["ManagedNetworkSegment"]:
            generator.shuffle(segment["security_policy"]["rules"])
        for pod in shuffled["TopologyPod"]:
            generator.shuffle(pod["devices"])
        assert _view(shuffled) == _view(roots)

    def test_names_are_the_hash_of_their_content(self) -> None:
        view = _good()
        field_sets = {item["ports"]: item["name"] for item in view["field_sets"]}
        assert field_sets["5432"] == "MSSG-FS-" + _hash(["5432"])
        assert field_sets["443, 8000-8010"] == "MSSG-FS-" + _hash(["443", "8000-8010"])
        by_name = {item["name"]: item for item in view["applications"]}
        assert "MSSG-APP-" + _hash("tcp", "destination", ["443", "8000-8010"]) in by_name
        assert "MSSG-APP-" + _hash("tcp", "source", ["443", "8000-8010"]) in by_name
        assert "MSSG-APP-" + _hash("icmp") in by_name
        for policy in view["policies"]:
            applied = [line["application"] for line in policy["lines"][:-1]]
            assert policy["name"] == "MSSG-POL-" + _hash(applied)

    def test_two_vteps_of_the_data_center_render_the_same_objects_and_segments(self) -> None:
        roots = _roots()
        leaf = _good(roots)
        border = _good(roots, device_name="dc1-border-leaf-01", role="border-leaf")
        assert leaf == border

    def test_a_vrf_cut_does_not_rename_what_remains(self) -> None:
        roots = _roots()
        full = _good(roots)
        part = _good(roots, vrfs=("NONPROD",))
        assert part["vrfs"] == [item for item in full["vrfs"] if item["name"] == "NONPROD"]


class TestRefusals:
    @pytest.mark.parametrize("name", ["unclassified", "Fallback", "UNAWARE"])
    def test_a_tag_named_like_a_pseudo_group_is_refused(self, name: str) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["name"] = name
        with pytest.raises(ValueError, match="reserved pseudo-group.*refusing unprotected config"):
            _view(roots)

    def test_two_segments_sharing_a_cli_name_are_refused(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["name"] = "web.np"
        gpo_segment(roots, "seg-app-np")["security_tag"]["name"] = "web_np"
        with pytest.raises(ValueError, match="share the name 'web_np'.*refusing unprotected config"):
            _view(roots)

    def test_a_port_range_ending_before_it_starts_is_refused(self) -> None:
        roots = _roots()
        rule = clean_data({"x": {"edges": [{"node": _gp_rule("bad-range", "seg-app-np", 9000, port_end=8000)}]}})["x"][
            0
        ]
        gpo_segment(roots, "seg-web-np")["security_policy"]["rules"].append(rule)
        with pytest.raises(ValueError, match="ends before it starts.*refusing unprotected config"):
            _view(roots)

    def test_a_protocol_with_no_application_is_refused(self) -> None:
        roots = _roots()
        rule = clean_data({"x": {"edges": [{"node": _gp_rule("gre", "seg-app-np", None, protocol="gre")}]}})["x"][0]
        gpo_segment(roots, "seg-web-np")["security_policy"]["rules"].append(rule)
        with pytest.raises(ValueError, match="no segment security application.*refusing unprotected config"):
            _view(roots)


class TestRendering:
    """The leaf and border-leaf EOS templates render the view; a device without one is byte-identical."""

    @staticmethod
    def _render(cls: type, **kwargs: Any) -> str:
        role, name, segments = VTEPS[cls.__name__]
        return run_transform(cls, _raw(role=role, name=name, segments=segments, **kwargs))

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    def test_the_block_is_in_spec_order(self, cls: type) -> None:
        rendered = self._render(cls)
        order = [
            "match-list input prefix-ipv4 app-np",
            "application traffic recognition",
            "   field-set l4-port",
            "router segment-security",
            "   policy MSSG-POL-",
            "\n   vrf NONPROD\n      segment app-np",
            "      segment web-np",
        ]
        positions = [rendered.index(text) for text in order]
        assert positions == sorted(positions)
        assert rendered.index("Vlan") < positions[0]
        assert rendered.rindex("router segment-security") < rendered.index("! End of configuration")

    def test_the_leaf_block_is_the_documented_shape(self) -> None:
        rendered = self._render(Leaf)
        assert "match-list input prefix-ipv4 web-np\n   match prefix-ipv4 10.10.1.0/24\n" in rendered
        assert "   field-set l4-port MSSG-FS-" + _hash(["443", "8000-8010"]) + "\n      443, 8000-8010\n" in rendered
        assert "application traffic recognition\n" in rendered
        assert re.search(r"      protocol tcp destination port field-set MSSG-FS-\w+\n", rendered)
        assert re.search(r"      protocol icmp\n", rendered)
        assert (
            "      segment app-np\n         definition\n            match prefix-ipv4 app-np\n         policies\n"
        ) in rendered
        assert "            from db-np policy policy-forward-all\n" in rendered
        assert re.search(r"            fallback policy (policy-forward-all|MSSG-POL-\w+)\n", rendered)
        assert re.search(r"      10 application MSSG-APP-\w+ action forward stateless\n", rendered)
        assert re.search(r"      \d+ application app-match-all action drop stateless\n", rendered)

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    def test_field_sets_are_inside_the_application_traffic_recognition_block(self, cls: type) -> None:
        """AVD renders `field-set l4-port` indented inside `application traffic recognition`, ports 6 spaces, after the applications."""
        lines = self._render(cls).splitlines()
        start = lines.index("application traffic recognition")
        end = lines.index("router segment-security")
        field_set_lines = [index for index, line in enumerate(lines) if "field-set l4-port" in line and "!" not in line]
        assert field_set_lines
        assert not [line for line in lines if line.startswith("field-set l4-port")]
        assert all(start < index < end for index in field_set_lines)
        last_application = max(index for index, line in enumerate(lines) if line.startswith("   application l4 "))
        for index in field_set_lines:
            assert index > last_application
            assert lines[index].startswith("   field-set l4-port MSSG-FS-")
            assert re.fullmatch(r"      \d[\d, -]*", lines[index + 1])
        assert lines[end - 1] == "!"
        assert all(line.startswith("   ") for line in lines[start + 1 : end - 1])

    def test_a_view_with_field_sets_but_no_applications_still_renders_the_block(self) -> None:
        """The block guard is `applications or field_sets`, so a field-set is never emitted outside a block."""
        view = {
            "enforce": True,
            "staged": None,
            "avd_only": True,
            "vrfs": [{"name": "V", "segments": []}],
            "skipped": [],
            "unsupported": [],
            "match_lists": [],
            "field_sets": [{"name": "FS1", "ports": "80, 443"}],
            "applications": [],
            "policies": [],
        }
        template = Path(__file__).parents[2] / "templates" / "configs" / "common" / "arista_eos_segment_security.j2"
        rendered = Template(template.read_text()).render(segment_security=view)
        assert "application traffic recognition\n   !\n" in rendered
        assert "\n   field-set l4-port FS1\n      80, 443\n" in rendered

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    def test_the_switch_is_the_last_line_of_the_mode(self, cls: type) -> None:
        """Spec 3.16: configured with the feature shut, switched LAST, inside the same router segment-security block."""
        for rendered, line in (
            (self._render(cls), "   no shutdown"),
            (self._render(cls, enforce=False), "   shutdown"),
        ):
            block = _block(rendered)
            assert block.rstrip().endswith(line + "\n!")
            assert block.rindex("      segment ") < block.rindex(line + "\n")
            assert block.count("\nrouter segment-security\n") == 1
            assert not block.split("\nrouter segment-security\n")[1].startswith("   shutdown")
            assert not block.split("\nrouter segment-security\n")[1].startswith("   no shutdown")

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    def test_enforcing_enables_and_staged_shuts_down(self, cls: type) -> None:
        """A staged data center keeps every segment, policy and application: only the switch (and a comment) differ."""
        enforcing, staged = self._render(cls), self._render(cls, enforce=False)
        assert "\n   no shutdown\n!\n" in enforcing
        assert "\n   shutdown\n!\n" in staged
        assert "no shutdown" not in _block(staged)
        assert "stages group policy (group_policy_enforce is false)" in staged
        assert "stages group policy" not in enforcing
        without_notice = "".join(line for line in staged.splitlines(keepends=True) if "stages group policy" not in line)
        assert without_notice.replace("\n   shutdown\n!\n", "\n   no shutdown\n!\n") == enforcing

    def test_an_ipv6_prefix_renders_a_prefix_ipv6_match_list_end_to_end(self) -> None:
        """T1: flip web-np to an IPv6 gateway prefix and render: match-list, its prefix and the segment definition."""
        raw = _raw()
        for edge in raw["ManagedNetworkSegment"]["edges"]:
            if edge["node"]["id"] == "seg-web-np":
                edge["node"]["gateway"]["node"]["ip_prefix"]["node"]["prefix"]["value"] = "2001:db8:10::/64"
        block = _block(run_transform(Leaf, raw))
        assert "match-list input prefix-ipv6 web-np\n   match prefix-ipv6 2001:db8:10::/64\n" in block
        assert "      segment web-np\n         definition\n            match prefix-ipv6 web-np\n" in block
        assert "match-list input prefix-ipv4 web-np" not in block
        assert "match-list input prefix-ipv4 app-np\n   match prefix-ipv4 10.10.2.0/24\n" in block

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    def test_the_header_states_the_lab_gate_and_the_limits_of_the_feature(self, cls: type) -> None:
        """C3: AVD-only lines may be rejected, what MSS-G does not filter, the broader stateless reply."""
        header = _block(self._render(cls)).split("\nmatch-list ")[0]
        for text in (
            "WARNING: custom policies, field-sets, l4 applications and fallback policy are documented by AVD only",
            "this release may REJECT them",
            "a whole-config push (configure replace, session commit) fails on an invalid line and the leaf loses ALL",
            "keep group_policy_enforce false and verify in a lab first",
            "MSS-G only filters traffic directed to a segment",
            "restricted by the SVI ACL and PBR (L0) only",
            "a stateless reply matches the port as ANY source port, so it is broader than the contract",
            "no segment policy policy-drop-all default is never rendered",
            "deny by default stays off",
            "remote prefixes reached through the L3 VNI or as EVPN host routes",
            "Multi-Site, border gateway and DCI traffic (spec 5.5)",
            "anycast virtual IP as a switch-owned address",
            "the VRF must exist (spec 1.13); name limits (spec 2.3); first match in ascending sequence order (spec 3.8)",
            "whether the fallback policy also catches a source in a segment with no from line (spec 3.14)",
        ):
            assert text in header, text

    def test_a_config_with_only_built_in_policies_carries_no_avd_warning(self) -> None:
        """The warning is about the AVD-only lines: a view without any (only forward-all) does not claim them."""
        with patch("transforms.helpers.group_policy.eos._MssgCatalog.policy", return_value=FORWARD_ALL):
            with patch("transforms.helpers.group_policy.eos._fallback_entries", return_value=(set(), set())):
                rendered = self._render(Leaf)
        assert "! WARNING" not in rendered
        assert "field-set l4-port" not in rendered

    def test_nested_prefixes_render_the_overlap_comment_on_both_segments(self) -> None:
        raw = _raw()
        for edge in raw["ManagedNetworkSegment"]["edges"]:
            if edge["node"]["id"] == "seg-app-np":
                edge["node"]["gateway"]["node"]["ip_prefix"]["node"]["prefix"]["value"] = "10.10.0.0/16"
        block = _block(run_transform(Leaf, raw))
        assert "      ! documented-not-lab-verified: this prefix overlaps 10.10.0.0/16 of segment app-np" in block
        assert block.index("! documented-not-lab-verified: this prefix overlaps") < block.index("      segment web-np")

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    def test_every_unverified_line_says_so(self, cls: type) -> None:
        rendered = self._render(cls)
        lines = rendered.splitlines()
        for index, line in enumerate(lines):
            if line.strip().startswith("field-set l4-port") or line == "application traffic recognition":
                assert lines[index - 1].strip().startswith("! documented-not-lab-verified")
            if line.strip().startswith("fallback policy"):
                assert any("documented-not-lab-verified" in earlier for earlier in lines[index - 8 : index])
        assert "documented-not-lab-verified: custom policies are AVD's model" in rendered
        assert (
            "documented-not-lab-verified: whether the lookup sees the inner IP of VXLAN-decapsulated traffic"
            in rendered
        )

    @pytest.mark.parametrize("cls", [Leaf, BorderLeaf, AccessLeaf])
    @pytest.mark.parametrize("enforce", [True, False])
    def test_forbidden_lines_are_never_rendered(self, cls: type, enforce: bool) -> None:
        """No default-forward switch, redirect, log, interface classifier or uRPF (spec 3.5, 3.4, 2.7, 1.6)."""
        code = "\n".join(_code(_block(self._render(cls, enforce=enforce))))
        assert "no segment policy" not in code
        assert "redirect" not in code
        assert "match interface" not in code
        assert "match covered" not in code
        assert "verify unicast" not in code
        assert not re.search(r"\blog\b", code)

    def test_no_eos_template_configures_urpf(self, root_dir: Path) -> None:
        """The manual: MSS-Group and uRPF do not work together (uRPF wins and the segments are removed)."""
        for template in (root_dir / "templates/configs").rglob("arista_eos*.j2"):
            assert "verify unicast" not in template.read_text(), template

    def test_the_included_template_is_closed_so_it_adds_no_blank_line(self, root_dir: Path) -> None:
        source = (root_dir / "templates/configs/common/arista_eos_segment_security.j2").read_text()
        assert source.rstrip("\n").endswith("{%- endif -%}")

    def test_a_skipped_vrf_and_unsupported_rows_are_comments_only(self) -> None:
        raw = _raw()
        segment = next(
            edge["node"] for edge in raw["ManagedNetworkSegment"]["edges"] if edge["node"]["id"] == "seg-app-np"
        )
        segment["gateway"]["node"]["ip_prefix"]["node"]["prefix"]["value"] = None
        rendered = run_transform(Leaf, raw)
        assert "! VRF NONPROD is not rendered: segment(s) app-np have no gateway prefix" in rendered
        assert "match-list input prefix-ipv4 web-np" not in rendered
        assert "! not expressible (left closed): portal-p -> web_pay_demo_local" in rendered
        assert "   vrf PROD\n" in _block(rendered) and "   vrf NONPROD\n" not in _block(rendered)

    def test_a_vtep_without_a_contract_vrf_renders_no_block(self) -> None:
        rendered = run_transform(Leaf, _raw(segments=()))
        assert "segment-security" not in rendered and "match-list" not in rendered

    def test_re_render_is_byte_identical(self) -> None:
        assert self._render(Leaf) == self._render(Leaf)


class TestByteIdentity:
    """A device outside the renderer's reach renders exactly its existing golden config."""

    def _golden(self, root_dir: Path) -> str:
        return (root_dir / "tests/smoke/configs/leaf_arista_eos_ebgp_ibgp/output.txt").read_text()

    def test_a_non_reference_eos_site_is_the_existing_golden(self, root_dir: Path) -> None:
        data = _gpo_golden_leaf_data(fleet=_GPO_EOS, version="4.36.2F", leaf_type="DCS-7050CX3-32C-R")
        assert "DcimGroupPolicyProfile" in data
        assert run_transform(Leaf, data) == self._golden(root_dir)

    def test_a_reference_leaf_carrying_no_contract_vrf_is_the_existing_golden(self, root_dir: Path) -> None:
        data = _gpo_golden_leaf_data(fleet=_GPO_EOS, version="4.36.2F")
        assert run_transform(Leaf, data) == self._golden(root_dir)

    def test_the_base_transform_has_no_segment_security_without_the_fleet_roots(self, root_dir: Path) -> None:
        data = _gpo_golden_leaf_data(fleet=_GPO_EOS, version="4.36.2F")
        bare = {key: value for key, value in data.items() if key in ("DcimDevice",)}
        assert run_transform(Leaf, bare) == self._golden(root_dir)
        assert issubclass(Leaf, BaseDeviceTransform)
