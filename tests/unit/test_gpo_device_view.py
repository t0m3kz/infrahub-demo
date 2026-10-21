"""Unit tests for the NX-OS VXLAN GPO device view (transforms/helpers/group_policy/ _vxlan_gpo_build).

The fleet is the smoke fixture's (tests/smoke/generate_config_fixtures.py ``_gpo_fleet_roots``): DC10 with a
leaf, an access leaf and a border leaf in the NX-OS row, a legacy DC12, NONPROD (fully classifiable by VLAN,
db-np stretched to DC12) and PROD (a microsegmented segment whose contracts name component groups: the web
component sits on the leaf's trunk port Ethernet10, the db component, a VM, on its access port Ethernet11).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from graphql import parse
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    SelectionNode,
)

from generators.topology.endpoint import ENDPOINT_NIC_ROLES
from tests.smoke.generate_config_fixtures import (
    _GPO_EOS,
    _GPO_NXOS,
    _GPO_PAY_DB_TAGS,
    _edges,
    _gp_device,
    _gp_instance,
    _gp_memory_gb,
    _gp_nic,
    _gp_tag,
    _gpo_segment_nodes,
    _make_interface,
    _node,
    _v,
    build_gpo_device_data,
    gpo_roots,
    gpo_segment,
    gpo_view,
)
from tests.smoke.test_configs_smoke import _run_transform
from transforms.common import BaseDeviceTransform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.edge import Edge
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.tor import ToR
from transforms.helpers.group_policy import (
    UNAWARE,
    UNCLASSIFIED,
    ComponentClaim,
    class_map_name,
    component_coverage,
    data_center_contracts,
    segment_vrf,
    vxlan_gpo_view,
)
from transforms.helpers.vxlan import get_interfaces, get_vxlan_config
from utils.data_cleaning import clean_data
from utils.fabric_roles import DC_VTEP_ROLES

LEAF_ACTIVATIONS: list[dict[str, Any]] = [
    {"vlan_id": 110, "segment": {"id": "seg-web-np"}},
    {"vlan_id": 120, "segment": {"id": "seg-app-np"}},
    {"vlan_id": 210, "segment": {"id": "seg-pay-p"}},
    {"vlan_id": 220, "segment": {"id": "seg-portal-p"}},
]
VRFS = ("NONPROD", "PROD")


LEAF_SEGMENTS = ("web-np", "app-np", "pay-p", "portal-p")


def _raw(**kwargs: Any) -> dict:
    """The raw query response of the leaf of DC10 (Ethernet10 carries the four segments, the access port
    Ethernet11 only pay-p), with the fleet roots."""
    return build_gpo_device_data(
        device_name="dc1-leaf-01",
        role="leaf",
        segments=LEAF_SEGMENTS,
        host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
        **kwargs,
    )


@cache
def _leaf_interfaces() -> list[dict[str, Any]]:
    """The cleaned interfaces of that leaf (what ``BaseDeviceTransform._extra_config`` passes the view)."""
    return clean_data(_raw())["DcimDevice"][0]["interfaces"]


def _roots(raw: dict | None = None) -> dict[str, Any]:
    """The cleaned fleet roots (everything but the device root)."""
    return gpo_roots(raw or _raw())


def _view(roots: dict[str, Any], **overrides: Any) -> dict[str, Any] | None:
    arguments: dict[str, Any] = {
        "device_name": "dc1-leaf-01",
        "role": "leaf",
        "platform": "cisco_nxos",
        "activations": LEAF_ACTIVATIONS,
        "vrfs": VRFS,
        "interfaces": _leaf_interfaces(),
    }
    return gpo_view("vxlan_gpo", roots, **{**arguments, **overrides})


def _contract(view: dict[str, Any], vrf: str, src: str, dst: str) -> dict[str, Any]:
    return next(item for item in view["vrfs"][vrf]["contracts"] if (item["src_name"], item["dst_name"]) == (src, dst))


class TestGate:
    """Anything but a VTEP of a reference fabric of a kind the platform renders gets None."""

    def test_vtep_of_a_reference_fabric_renders(self) -> None:
        view = _view(_roots())
        assert view is not None
        assert set(view["vrfs"]) == {"NONPROD", "PROD"}

    @pytest.mark.parametrize("role", sorted(DC_VTEP_ROLES))
    def test_every_data_center_vtep_role_renders(self, role: str) -> None:
        assert _view(_roots(), role=role) is not None

    @pytest.mark.parametrize("role", ["spine", "super-spine", "hyper-spine", "l2-leaf", "edge", "firewall", ""])
    def test_other_roles_render_nothing(self, role: str) -> None:
        assert _view(_roots(), role=role) is None

    def test_a_site_with_a_vtep_outside_its_row_is_no_reference_fabric(self) -> None:
        raw = _raw(leaf_type="N9K-C9336C-FX2")
        assert _view(_roots(raw)) is None

    def test_a_release_below_the_floor_is_no_reference_fabric(self) -> None:
        assert _view(_roots(_raw(version="10.6(0)F"))) is None

    def test_a_site_of_a_kind_without_a_renderer_renders_nothing(self) -> None:
        """EOS segment_security: reference site, but no GPO."""
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01", role="leaf", segments=(), fleet=_GPO_EOS, version="4.36.2F"
        )
        assert _view(_roots(raw), platform="arista_eos") is None

    def test_a_platform_without_a_renderer_renders_nothing(self) -> None:
        for platform in ("arista_eos", "sonic", "nokia_sros", ""):
            assert _view(_roots(), platform=platform) is None

    def test_no_profile_row_is_no_reference_fabric(self) -> None:
        roots = _roots()
        roots["DcimGroupPolicyProfile"] = []
        assert _view(roots) is None

    def test_no_fleet_roots_render_nothing(self) -> None:
        """A device query without the fleet roots (an old fixture) is not a reference fabric, and does not raise."""
        assert _view({}) is None

    def test_device_in_no_data_center_renders_nothing(self) -> None:
        assert _view(_roots(), device_name="ghost-01") is None

    def test_device_in_two_data_centers_renders_nothing(self) -> None:
        roots = _roots()
        roots["TopologyDataCenter"][1]["devices"] = [*roots["TopologyDataCenter"][1]["devices"], _vtep("dc1-leaf-01")]
        assert _view(roots) is None

    def test_a_site_mixing_group_policy_kinds_renders_nothing(self) -> None:
        roots = _roots()
        eos = _gp_device("dc1-eos-01", "leaf", "DCS-7280SR3-48YC8", "4.36.2F", _gp_memory_gb(), "arista_eos")
        roots["TopologyDataCenter"][0]["devices"].append(clean_data({"x": {"edges": [{"node": eos}]}})["x"][0])
        assert _view(roots) is None

    @pytest.mark.parametrize("vrfs", [[], ["OTHER"]])
    def test_a_vtep_without_a_contract_vrf_only_enables_group_policy(self, vrfs: list[str]) -> None:
        """No tenant VRF on the device (the servers sit on the access leaves): still policy-aware, nothing to define."""
        assert _view(_roots(), vrfs=vrfs, activations=[]) == {"class_maps": [], "groups": [], "vrfs": {}}

    @pytest.mark.parametrize("role", sorted(DC_VTEP_ROLES))
    def test_every_vtep_role_without_a_contract_vrf_is_enabled(self, role: str) -> None:
        assert _view(_roots(), role=role, vrfs=[]) == {"class_maps": [], "groups": [], "vrfs": {}}

    @pytest.mark.parametrize("role", ["spine", "edge", "l2-leaf"])
    def test_a_non_vtep_without_a_contract_vrf_renders_nothing(self, role: str) -> None:
        assert _view(_roots(), role=role, vrfs=[]) is None

    def test_a_non_reference_fabric_without_a_contract_vrf_renders_nothing(self) -> None:
        assert _view(_roots(_raw(leaf_type="N9K-C9336C-FX2")), vrfs=[]) is None

    def test_an_eos_vtep_without_a_contract_vrf_renders_nothing(self) -> None:
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01", role="leaf", segments=(), fleet=_GPO_EOS, version="4.36.2F"
        )
        assert _view(_roots(raw), platform="arista_eos", vrfs=[]) is None

    def test_other_vrfs_group_data_does_not_refuse_an_enablement_only_vtep(self) -> None:
        """Nothing is rendered from the groups, so a collision among them is not this device's to refuse."""
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["security_tag"]["name"] = "web-np"
        assert _view(roots, vrfs=[]) == {"class_maps": [], "groups": [], "vrfs": {}}
        with pytest.raises(ValueError, match="refusing unprotected config"):
            _view(roots, vrfs=["NONPROD"])


def _vtep(name: str) -> dict[str, Any]:
    raw = _gp_device(name, "leaf", "N9K-C9364C-GX", "10.6(3)F", _gp_memory_gb())
    return clean_data({"x": {"edges": [{"node": raw}]}})["x"][0]


class TestScope:
    """The contract set is the data center's; only the selectors and the VRF cut are the device's."""

    def test_two_vteps_of_a_data_center_render_the_same_contracts_per_vrf(self) -> None:
        roots = _roots()
        leaf = _view(roots)
        access = _view(
            roots,
            device_name="dc1-access-leaf-01",
            role="access-leaf",
            activations=[{"vlan_id": 777, "segment": {"id": "seg-app-np"}}],
            vrfs=VRFS,
        )
        assert leaf is not None and access is not None
        assert leaf["vrfs"] == access["vrfs"]
        assert leaf["class_maps"] == access["class_maps"]

    def test_selectors_are_the_devices_own_vlans(self) -> None:
        roots = _roots()
        leaf = _view(roots)
        access = _view(roots, activations=[{"vlan_id": 777, "segment": {"id": "seg-app-np"}}])
        assert leaf is not None and access is not None
        assert {group["name"]: group["vlan_id"] for group in leaf["groups"]} == {
            "web-np": 110,
            "app-np": 120,
            "db-np": None,
            "pay-p": 210,
            "portal-p": 220,
            "web_pay_demo_local": None,
            "db_pay_demo_local": None,
        }
        assert {group["name"]: group["vlan_id"] for group in access["groups"] if group["vlan_id"]} == {"app-np": 777}

    def test_an_inline_terminated_segment_is_not_classified_by_vlan(self) -> None:
        roots = _roots()
        view = _view(
            roots,
            activations=[{"vlan_id": 110, "segment": {"id": "seg-web-np", "terminate_inline": True}}],
        )
        assert view is not None
        assert next(group for group in view["groups"] if group["name"] == "web-np")["vlan_id"] is None

    def test_only_the_vrfs_the_device_carries_are_rendered(self) -> None:
        view = _view(_roots(), vrfs=["NONPROD"])
        assert view is not None
        assert set(view["vrfs"]) == {"NONPROD"}
        names = {group["name"] for group in view["groups"]}
        assert names == {"web-np", "app-np", "db-np"}
        assert all("PROD" not in item["name"] for item in view["class_maps"])

    def test_a_rule_less_local_segment_is_still_defined_in_an_enforced_vrf(self) -> None:
        """An enforced VRF denies what no selector classified: a segment with no row needs its group."""
        roots = _roots()
        # app-np has no rules of its own and no rule names it as a destination: it has no row.
        gpo_segment(roots, "seg-app-np")["security_policy"]["rules"] = []
        for segment in roots["ManagedNetworkSegment"]:
            segment["security_policy"]["rules"] = [
                rule
                for rule in segment["security_policy"]["rules"]
                if (rule.get("destination_segment") or {}).get("id") != "seg-app-np"
            ]
        view = _view(roots)
        assert view is not None
        assert {contract["src_name"] for contract in view["vrfs"]["NONPROD"]["contracts"]}.isdisjoint({"app-np"})
        assert next(group for group in view["groups"] if group["name"] == "app-np")["vlan_id"] == 120

    def test_segment_vrf_is_the_vrf_name_of_the_l3_vni_mapping(self) -> None:
        """The VRF cut compares a group's vrf (segment_vrf) with the device's l3_vni_mappings vrf_name."""
        raw = build_gpo_device_data(
            device_name="dc1-border-leaf-01",
            role="border-leaf",
            segments=("web-np", "app-np", "db-np", "pay-p", "portal-p"),
        )
        transform = BorderLeaf.__new__(BorderLeaf)
        device, roots, platform = transform._device_and_platform(raw)
        activations = transform._device_activations(device)
        vxlan = get_vxlan_config(device, platform or "", device_role="border-leaf", activations=activations)
        assert vxlan is not None
        by_id = {segment["id"]: segment for segment in roots["ManagedNetworkSegment"]}
        carried = {segment_vrf(by_id[activation["segment"]["id"]]) for activation in activations}
        assert carried == {mapping["vrf_name"] for mapping in vxlan["l3_vni_mappings"]} == {"NONPROD", "PROD"}


class TestNames:
    def test_class_map_name_is_the_sha256_of_the_sorted_entry_set(self) -> None:
        entries = [
            {"protocol": "udp", "port_start": 53, "port_end": 53},
            {"protocol": "tcp", "port_start": 443, "port_end": 443},
        ]
        canonical = json.dumps([["tcp", 443, 443], ["udp", 53, 53]], separators=(",", ":"))
        assert class_map_name(entries) == "GPO-" + hashlib.sha256(canonical.encode()).hexdigest()[:10]
        assert class_map_name(list(reversed(entries))) == class_map_name(entries)

    def test_different_entry_sets_have_different_names(self) -> None:
        one = [{"protocol": "tcp", "port_start": 443, "port_end": 443}]
        two = [{"protocol": "tcp", "port_start": 443, "port_end": 444}]
        assert class_map_name(one) != class_map_name(two)

    def test_one_class_map_per_distinct_entry_set(self) -> None:
        view = _view(_roots())
        assert view is not None
        names = [item["name"] for item in view["class_maps"]]
        assert names == sorted(set(names))
        used = {contract["policy"] for vrf in view["vrfs"].values() for contract in vrf["contracts"]}
        assert used == set(names)
        shared = class_map_name([{"protocol": "tcp", "port_start": 5432, "port_end": 5432}])
        assert sum(1 for vrf in view["vrfs"].values() for c in vrf["contracts"] if c["policy"] == shared) >= 2

    def test_group_names_are_cli_safe(self) -> None:
        view = _view(_roots())
        assert view is not None
        assert all(item["name"].replace("-", "").replace("_", "").isalnum() for item in view["groups"])
        assert "web_pay_demo_local" in {item["name"] for item in view["groups"]}


class TestEntries:
    def test_tcp_ports_and_ranges_render_for_ipv4_and_ipv6(self) -> None:
        view = _view(_roots())
        assert view is not None
        lines = next(item for item in view["class_maps"] if item["entries"] == "tcp/443, tcp/8000-8010")["lines"]
        assert lines == [
            "match ipv4 tcp dport 443",
            "match ipv6 tcp dport 443",
            "match ipv4 tcp dport 8000 to 8010",
            "match ipv6 tcp dport 8000 to 8010",
        ]

    def test_forms_the_guide_does_not_show_come_from_one_table_and_are_marked(self) -> None:
        view = _view(_roots())
        assert view is not None
        by_entries = {item["entries"]: item for item in view["class_maps"]}
        assert by_entries["icmp"]["lines"] == ["match ip icmp"]
        assert by_entries["any"]["lines"] == ["match ip"]
        assert not by_entries["tcp/443"]["notes"]
        for entries in ("icmp", "any"):
            (note,) = by_entries[entries]["notes"]
            assert "is not in the guide;" in note
            assert "IPv6 for this entry is denied by the default deny (fail closed)" in note

    def test_a_port_less_tcp_rule_uses_the_table_form(self) -> None:
        roots = _roots()
        rule = gpo_segment(roots, "seg-web-np")["security_policy"]["rules"][0]
        rule["port_start"], rule["port_end"] = None, None
        view = _view(roots)
        assert view is not None
        port_less = next(item for item in view["class_maps"] if "match ip tcp" in item["lines"])
        assert port_less["entries"].split(", ")[0] == "tcp"
        # The guide's sample show running-config prints `match ip tcp`, so it is not "not in the guide".
        (note,) = port_less["notes"]
        assert "`match ip tcp` for tcp is only in the guide's sample show running-config;" in note
        assert "IPv6 for this entry is denied by the default deny (fail closed)" in note


class TestRefusals:
    """A row the device cannot number is refused, never rendered with a guess."""

    def test_a_group_without_a_group_id(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["group_id"] = None
        with pytest.raises(ValueError, match=r"group web-np has no group id.*refusing unprotected config"):
            _view(roots)

    def test_a_reserved_group_without_an_id(self) -> None:
        """db-np is stretched: its unaware half needs the profile's unaware group id."""
        roots = _roots()
        for profile in roots["DcimGroupPolicyProfile"]:
            if profile["platform"]["name"] == "cisco_nxos":
                profile["unaware_group_id"] = None
        with pytest.raises(ValueError, match=r"group unaware has no group id.*refusing unprotected config"):
            _view(roots)

    def test_two_groups_sharing_an_id(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["group_id"] = 41
        with pytest.raises(ValueError, match=r"share group id 41.*refusing unprotected config"):
            _view(roots)

    def test_a_group_on_a_reserved_id(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["group_id"] = 15
        with pytest.raises(ValueError, match=r"share group id 15"):
            _view(roots)

    def test_two_groups_sharing_a_cli_name(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["name"] = "a.b"
        gpo_segment(roots, "seg-app-np")["security_tag"]["name"] = "a_b"
        with pytest.raises(ValueError, match=r"share the group name 'a_b'.*refusing unprotected config"):
            _view(roots)

    def test_a_port_range_ending_before_it_starts(self) -> None:
        roots = _roots()
        rule = gpo_segment(roots, "seg-web-np")["security_policy"]["rules"][1]
        rule["port_start"], rule["port_end"] = 8010, 8000
        with pytest.raises(ValueError, match=r"8010-8000 ends before it starts.*refusing unprotected config"):
            _view(roots)

    @staticmethod
    def _without_unclassified_id(roots: dict[str, Any]) -> None:
        for profile in roots["DcimGroupPolicyProfile"]:
            if profile["platform"]["name"] == "cisco_nxos":
                profile["unclassified_group_id"] = None

    def test_a_contract_with_the_unclassified_group_needs_the_profiles_id(self) -> None:
        roots = _roots()
        self._without_unclassified_id(roots)
        with pytest.raises(ValueError, match=r"group unclassified has no group id.*refusing unprotected config"):
            _view(roots)

    def test_an_enforced_vrf_without_an_unclassified_group_id_is_refused(self) -> None:
        """`security enforce tag <unclassified id>` needs the id even when no contract row names UNCLASSIFIED.

        With no rule at all, NONPROD's only rows are the stretched db-np's unaware halves.
        """
        roots = _roots()
        for segment in roots["ManagedNetworkSegment"]:
            segment["security_policy"]["rules"] = []
        assert _view(roots) is not None
        self._without_unclassified_id(roots)
        with pytest.raises(ValueError, match=r"VRF NONPROD is enforced but.*refusing unprotected config"):
            _view(roots, vrfs=["NONPROD"])

    def test_a_tag_shared_by_two_segments_is_refused(self) -> None:
        """The views key a group by name: the later segment's selector would silently win."""
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["security_tag"]["name"] = "web-np"
        with pytest.raises(
            ValueError,
            match=r"group 'web-np' is the tag of segment web-np \(VRF NONPROD\) and of segment app-np \(VRF NONPROD\)"
            r".*refusing unprotected config",
        ):
            _view(roots)

    def test_a_tag_shared_by_two_vrfs_is_refused(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-portal-p")["security_tag"]["name"] = "web-np"
        with pytest.raises(ValueError, match=r"group 'web-np' is the tag of .*refusing unprotected config"):
            _view(roots)

    @pytest.mark.parametrize("name", ["unaware", "UNAWARE", "unclassified", "UNCLASSIFIED", "Fallback", "FALLBACK"])
    def test_a_group_named_like_a_pseudo_group_is_refused(self, name: str) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-web-np")["security_tag"]["name"] = name
        with pytest.raises(
            ValueError, match=rf"group '{name}' is named like the reserved pseudo-group.*refusing unprotected config"
        ):
            _view(roots)

    def test_an_unenforced_vrf_renders_no_enforce_tag(self) -> None:
        """PROD's component ports are unknown to a device with no interfaces: no enforce line, no enforce tag."""
        view = _view(_roots(), interfaces=[])
        assert view is not None
        assert view["vrfs"]["PROD"]["enforce_tag"] is None


class TestEnforcement:
    def test_a_vrf_naming_only_segment_groups_is_enforced_with_the_unclassified_group(self) -> None:
        view = _view(_roots())
        assert view is not None
        assert view["vrfs"]["NONPROD"]["unenforced"] is None
        assert view["vrfs"]["NONPROD"]["enforce_tag"] == 0

    def test_a_segment_without_a_vlan_here_leaves_its_vrf_unenforced(self) -> None:
        activations = [
            {**item, "vlan_id": None} if item["segment"]["id"] == "seg-app-np" else item for item in LEAF_ACTIVATIONS
        ]
        view = _view(_roots(), activations=activations)
        assert view is not None
        nonprod = view["vrfs"]["NONPROD"]
        assert nonprod["unenforced"] == "segment app-np has no group classifier on this device"
        assert nonprod["enforce_tag"] is None
        assert nonprod["contracts"], "the contracts are still rendered"
        assert {group["name"] for group in view["groups"]} >= {"web-np", "app-np"}
        assert view["vrfs"]["PROD"]["enforce_tag"] == 0, "another VRF's gap does not unenforce this one"

    def test_a_segment_without_a_group_leaves_its_vrf_unenforced(self) -> None:
        roots = _roots()
        gpo_segment(roots, "seg-app-np")["security_tag"] = None
        view = _view(roots)
        assert view is not None
        assert view["vrfs"]["NONPROD"]["unenforced"] == "segment app-np has no group classifier on this device"
        assert view["vrfs"]["NONPROD"]["enforce_tag"] is None

    def test_an_inline_terminated_segment_leaves_its_vrf_unenforced(self) -> None:
        activations = [
            {**item, "segment": {**item["segment"], "terminate_inline": True}} if item["vlan_id"] == 110 else item
            for item in LEAF_ACTIVATIONS
        ]
        view = _view(_roots(), activations=activations)
        assert view is not None
        assert view["vrfs"]["NONPROD"]["unenforced"] == "segment web-np has no group classifier on this device"

    def test_every_segment_without_a_classifier_is_named(self) -> None:
        view = _view(
            _roots(), activations=[{"vlan_id": None, "segment": {"id": s}} for s in ("seg-web-np", "seg-app-np")]
        )
        assert view is not None
        assert view["vrfs"]["NONPROD"]["unenforced"] == (
            "segment app-np has no group classifier on this device; segment web-np has no group classifier on this device"
        )

    def test_a_segment_outside_the_vrf_or_the_tenant_segments_does_not_block_enforcement(self) -> None:
        """Another VRF's segment, an external segment and an unknown id leave NONPROD enforced."""
        activations = [
            *LEAF_ACTIVATIONS,
            {"vlan_id": None, "segment": {"id": "seg-internet"}},
            {"vlan_id": None, "segment": {"id": "seg-unknown"}},
        ]
        view = _view(_roots(), activations=activations)
        assert view is not None
        assert view["vrfs"]["NONPROD"]["unenforced"] is None
        assert view["vrfs"]["NONPROD"]["enforce_tag"] == 0

    def test_an_unclassified_segment_of_another_vrf_does_not_unenforce_this_one(self) -> None:
        activations = [item for item in LEAF_ACTIVATIONS if item["segment"]["id"] != "seg-portal-p"]
        activations.append({"vlan_id": None, "segment": {"id": "seg-portal-p"}})
        view = _view(_roots(), activations=activations)
        assert view is not None
        assert view["vrfs"]["NONPROD"]["enforce_tag"] == 0

    def test_a_vrf_naming_classified_component_groups_is_enforced(self) -> None:
        view = _view(_roots())
        assert view is not None
        prod = view["vrfs"]["PROD"]
        assert prod["unenforced"] is None
        assert prod["enforce_tag"] == 0
        assert prod["contracts"]
        assert {"web_pay_demo_local", "db_pay_demo_local"} <= {group["name"] for group in view["groups"]}

    def test_component_groups_have_no_segment_vlan_selector_but_their_ports(self) -> None:
        view = _view(_roots())
        assert view is not None
        components = {group["name"]: group for group in view["groups"] if group["name"].endswith("_demo_local")}
        assert all(group["vlan_id"] is None for group in components.values())
        assert components["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet10"}]
        assert components["db_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]

    def test_reserved_groups_carry_the_profiles_ids(self) -> None:
        view = _view(_roots())
        assert view is not None
        nonprod = view["vrfs"]["NONPROD"]["contracts"]
        assert {c["src"] for c in nonprod if c["src_name"] == "UNCLASSIFIED"} == {0}
        assert {c["src"] for c in nonprod if c["src_name"] == "UNAWARE"} == {15}
        assert all(c["src"] is not None and c["dst"] is not None for c in nonprod)

    def test_reserved_groups_are_never_defined_or_any(self) -> None:
        view = _view(_roots())
        assert view is not None
        ids = {group["id"] for group in view["groups"]}
        assert 0 not in ids and 15 not in ids
        assert "any" not in json.dumps(view["vrfs"])

    def test_contracts_are_sorted_by_group_ids(self) -> None:
        view = _view(_roots())
        assert view is not None
        for vrf in view["vrfs"].values():
            pairs = [(item["src"], item["dst"]) for item in vrf["contracts"]]
            assert pairs == sorted(pairs)
            assert len(pairs) == len(set(pairs))


STAGED = (
    "data center DC10 stages group policy (group_policy_enforce is false): "
    "contracts are configured, security enforce is not"
)


def _stage(roots: dict[str, Any], dc_id: str, enforce: bool | None) -> dict[str, Any]:
    """Set (None: remove) ``group_policy_enforce`` on one data center of the cleaned fleet roots."""
    node = next(item for item in roots["TopologyDataCenter"] if item["id"] == dc_id)
    if enforce is None:
        node.pop("group_policy_enforce", None)
    else:
        node["group_policy_enforce"] = enforce
    return roots


class TestStaging:
    """A6: ``group_policy_enforce`` of the device's data center; false configures the contracts, not enforcement."""

    def test_the_fixture_data_center_enforces(self) -> None:
        roots = _roots()
        assert (
            next(item for item in roots["TopologyDataCenter"] if item["id"] == "dc10")["group_policy_enforce"] is True
        )
        assert data_center_contracts(roots, "dc10").enforce is True

    def test_an_enforcing_data_center_enforces_every_classifiable_vrf(self) -> None:
        view = _view(_stage(_roots(), "dc10", True))
        assert view is not None
        assert {vrf: (item["unenforced"], item["enforce_tag"]) for vrf, item in view["vrfs"].items()} == {
            "NONPROD": (None, 0),
            "PROD": (None, 0),
        }

    @pytest.mark.parametrize("flag", [False, None])
    def test_a_staging_data_center_enforces_no_vrf_and_says_why_first(self, flag: bool | None) -> None:
        """False and a missing key (an old query response) both stage."""
        view = _view(_stage(_roots(), "dc10", flag))
        assert view is not None
        for item in view["vrfs"].values():
            assert item["unenforced"] == STAGED
            assert item["enforce_tag"] is None

    def test_a_staging_data_center_still_configures_groups_and_contracts(self) -> None:
        enforcing, staged = _view(_roots()), _view(_stage(_roots(), "dc10", False))
        assert enforcing is not None and staged is not None
        assert staged["class_maps"] == enforcing["class_maps"]
        assert staged["groups"] == enforcing["groups"]
        for vrf, item in staged["vrfs"].items():
            assert item["contracts"] == enforcing["vrfs"][vrf]["contracts"]
            assert item["classified_ports"] == enforcing["vrfs"][vrf]["classified_ports"]

    def test_the_staged_reason_comes_first_and_the_other_reasons_stay_listed(self) -> None:
        activations = [
            {**item, "vlan_id": None} if item["segment"]["id"] == "seg-app-np" else item for item in LEAF_ACTIVATIONS
        ]
        view = _view(_stage(_roots(), "dc10", False), activations=activations, interfaces=[])
        assert view is not None
        assert (
            view["vrfs"]["NONPROD"]["unenforced"] == f"{STAGED}; segment app-np has no group classifier on this device"
        )
        prod = view["vrfs"]["PROD"]["unenforced"]
        assert prod.startswith(f"{STAGED}; component group ")
        assert view["vrfs"]["PROD"]["enforce_tag"] is None

    def test_the_flag_of_the_devices_own_data_center_decides(self) -> None:
        """Two data centers, opposite flags, in either order: DC10's VTEP follows DC10."""
        for dc10, dc12 in ((False, True), (True, False)):
            roots = _stage(_stage(_roots(), "dc10", dc10), "dc12", dc12)
            for ordered in (roots["TopologyDataCenter"], roots["TopologyDataCenter"][::-1]):
                reordered = {**roots, "TopologyDataCenter": ordered}
                assert data_center_contracts(reordered, "dc10").enforce is dc10
                assert data_center_contracts(reordered, "dc12").enforce is dc12
                view = _view(reordered)
                assert view is not None
                assert all((item["unenforced"] == STAGED) is (not dc10) for item in view["vrfs"].values())

    def test_the_data_center_without_a_name_is_named_by_its_id(self) -> None:
        roots = _stage(_roots(), "dc10", False)
        next(item for item in roots["TopologyDataCenter"] if item["id"] == "dc10").pop("name")
        view = _view(roots)
        assert view is not None
        assert view["vrfs"]["NONPROD"]["unenforced"] == STAGED.replace("DC10", "dc10")

    def test_the_missing_unclassified_group_id_is_refused_only_when_enforcing(self) -> None:
        """Staged: no `security enforce tag`, so the profile's unclassified id is not needed for it."""
        roots = _roots()
        for segment in roots["ManagedNetworkSegment"]:
            segment["security_policy"]["rules"] = []
        TestRefusals._without_unclassified_id(roots)
        with pytest.raises(ValueError, match=r"VRF NONPROD is enforced but.*refusing unprotected config"):
            _view(_stage(roots, "dc10", True), vrfs=["NONPROD"])
        staged = _view(_stage(roots, "dc10", False), vrfs=["NONPROD"])
        assert staged is not None
        assert staged["vrfs"]["NONPROD"]["unenforced"] == STAGED
        assert staged["vrfs"]["NONPROD"]["enforce_tag"] is None

    def test_the_view_is_the_same_for_every_vtep_of_the_staged_data_center(self) -> None:
        roots = _stage(_roots(), "dc10", False)
        for role in sorted(DC_VTEP_ROLES):
            view = _view(roots, role=role)
            assert view is not None
            assert view["vrfs"]["NONPROD"]["unenforced"] == STAGED

    def test_the_rendered_config_keeps_the_policy_and_prints_the_staging_comment_instead_of_enforce(self) -> None:
        enforcing = _run_transform(Leaf, _raw())
        staged = _run_transform(Leaf, _raw(enforce=False))
        assert "security enforce tag 0 default deny" in enforcing
        assert "security enforce" not in staged.replace(f"! unenforced: {STAGED}", "")
        assert staged.count(f"! unenforced: {STAGED}") == 2, "one comment per VRF"
        assert enforcing.count("! unenforced:") == 0
        for line in ("feature security-group", "system routing template-security-groups"):
            assert staged.splitlines().count(line) == 1
        assert "class-map type security" in staged and "policy-map type security" in staged
        assert "security contract" in staged


class TestPairNotes:
    def test_the_device_marks_both_contracts_of_a_pair(self) -> None:
        view = _view(_roots())
        assert view is not None
        marker = "two bidir contracts on one pair"
        assert any(marker in note for note in _contract(view, "NONPROD", "web-np", "app-np")["notes"])
        assert any(marker in note for note in _contract(view, "NONPROD", "app-np", "web-np")["notes"])
        assert not _contract(view, "PROD", "portal-p", "web_pay_demo_local")["notes"]

    def test_the_unclassified_end_is_marked_not_lab_verified(self) -> None:
        view = _view(_roots())
        assert view is not None
        notes = _contract(view, "NONPROD", "UNCLASSIFIED", "web-np")["notes"]
        assert any("documented-not-lab-verified: reserved group 0 (UNCLASSIFIED)" in note for note in notes)


class TestViewIsTheDataCenterView:
    def test_a_group_is_unaware_where_it_is_also_active_outside_the_reference_sites(self) -> None:
        """db-np is stretched to DC12 (legacy): a row of it arrives as the unaware group too."""
        roots = _roots()
        contracts = data_center_contracts(roots, "dc10")
        view = vxlan_gpo_view(
            contracts.all_groups,
            contracts.rows,
            reserved={"unclassified": 0, "unaware": 15},
            reference_sites=contracts.reference_sites,
            local=contracts.local_names,
        )
        assert {UNAWARE, UNCLASSIFIED} <= {row[end] for row in view["contracts"] for end in ("src", "dst")}
        device = _view(roots)
        assert device is not None
        assert any(c["src_name"] == "UNAWARE" for c in device["vrfs"]["NONPROD"]["contracts"])


class TestExtraConfig:
    """BaseDeviceTransform._extra_config sets ``group_policy`` for the VTEP roles of a reference fabric only."""

    @staticmethod
    def _config(transform_cls: type[BaseDeviceTransform], raw: dict, root_dir: Path) -> dict[str, Any]:
        transform = transform_cls.__new__(transform_cls)
        transform.root_directory = str(root_dir)
        device, roots, platform = transform._device_and_platform(copy.deepcopy(raw))
        activations = transform._device_activations(device)
        if activations:
            device["segment_deployments"] = activations
        return transform._extra_config(device, platform or "", extra_roots=roots)

    @staticmethod
    def _raw_with_vtep(name: str, role: str, segments: tuple[str, ...] = ("web-np", "app-np")) -> dict:
        raw = build_gpo_device_data(device_name=name, role=role, segments=segments)
        vtep = _gp_device(name, role, "N9K-C9364C-GX", "10.6(3)F", _gp_memory_gb())
        raw["TopologyDataCenter"]["edges"][0]["node"]["devices"]["edges"].append({"node": vtep})
        return raw

    @pytest.mark.parametrize(
        ("transform_cls", "name", "role"),
        [
            (Leaf, "dc1-leaf-01", "leaf"),
            (AccessLeaf, "dc1-access-leaf-01", "access-leaf"),
            (BorderLeaf, "dc1-border-leaf-01", "border-leaf"),
            (ToR, "dc1-tor-01", "tor"),
            (BorderSpine, "dc1-border-spine-01", "border-spine"),
        ],
    )
    def test_vtep_roles_get_the_group_policy(
        self, transform_cls: type[BaseDeviceTransform], name: str, role: str, root_dir: Path
    ) -> None:
        config = self._config(transform_cls, self._raw_with_vtep(name, role), root_dir)
        assert set(config["group_policy"]["vrfs"]) == {"NONPROD"}
        assert {group["vlan_id"] for group in config["group_policy"]["groups"] if group["vlan_id"]} == {110, 120}

    @pytest.mark.parametrize(("transform_cls", "role"), [(L2Leaf, "l2-leaf"), (Edge, "edge")])
    def test_other_roles_get_none(self, transform_cls: type[BaseDeviceTransform], role: str, root_dir: Path) -> None:
        raw = self._raw_with_vtep("dc1-leaf-01", role)
        assert "group_policy" not in self._config(transform_cls, raw, root_dir)

    def test_a_vtep_without_tenant_segments_gets_the_enablement_only_group_policy(self, root_dir: Path) -> None:
        raw = self._raw_with_vtep("dc1-leaf-01", "leaf", segments=())
        config = self._config(Leaf, raw, root_dir)
        assert config["group_policy"] == {"class_maps": [], "groups": [], "vrfs": {}}

    def test_the_enablement_only_config_has_no_group_definitions_or_contracts(self, root_dir: Path) -> None:
        rendered = _run_transform(Leaf, build_gpo_device_data(device_name="dc1-leaf-01", role="leaf", segments=()))
        lines = rendered.splitlines()
        assert lines.count("feature security-group") == 1
        assert lines.count("system routing template-security-groups") == 1
        for forbidden in (
            "class-map type security",
            "policy-map type security",
            "security-group ",
            "security contract",
        ):
            assert not [line for line in lines if line.strip().startswith(forbidden)]
        assert "security enforce" not in rendered
        assert "Group policy" not in rendered
        assert "Security groups" not in rendered

    def test_a_spine_role_gets_none(self, root_dir: Path) -> None:
        transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
        transform.device_role = "spine"
        transform.root_directory = str(root_dir)
        raw = self._raw_with_vtep("dc1-leaf-01", "spine")
        device, roots, platform = transform._device_and_platform(raw)
        assert "group_policy" not in transform._extra_config(device, platform or "", extra_roots=roots)

    def test_a_device_query_without_the_fleet_roots_gets_none(self, root_dir: Path) -> None:
        raw = {"DcimDevice": self._raw_with_vtep("dc1-leaf-01", "leaf")["DcimDevice"]}
        assert "group_policy" not in self._config(Leaf, raw, root_dir)

    def test_a_non_nxos_platform_gets_none(self, root_dir: Path) -> None:
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01", role="leaf", segments=("web-np",), fleet={**_GPO_NXOS, "platform": "arista_eos"}
        )
        assert "group_policy" not in self._config(Leaf, raw, root_dir)

    def test_the_rendered_nxos_config_carries_the_block_exactly_once(self, root_dir: Path) -> None:
        raw = self._raw_with_vtep("dc1-leaf-01", "leaf")
        transform = Leaf.__new__(Leaf)
        transform.root_directory = str(root_dir)
        rendered = asyncio.run(transform.transform(raw))
        lines = rendered.splitlines()
        assert lines.count("feature security-group") == 1
        assert lines.count("system routing template-security-groups") == 1
        assert lines.count("  security enforce tag 0 default deny") == 1
        assert "mac-segmentation" not in rendered
        assert " any" not in "\n".join(line for line in rendered.splitlines() if "security contract" in line)
        assert rendered.index("class-map type security") < rendered.index("security-group 40 name")
        assert rendered.index("security-group 40 name") < rendered.index("vrf context NONPROD")
        assert rendered.index("vrf context NONPROD") < rendered.index("security contract source")


class TestGoldens:
    """A device outside a GPO renderer's reach renders exactly its existing golden config, fleet roots or not."""

    @pytest.mark.parametrize(
        ("gpo_fixture", "golden"),
        [
            ("leaf_cisco_nxos_gpo_non_reference", "leaf_cisco_nxos_ebgp_ibgp"),
        ],
    )
    def test_output_equals_the_existing_golden(self, root_dir: Path, gpo_fixture: str, golden: str) -> None:
        configs = root_dir / "tests/smoke/configs"
        data = json.loads((configs / gpo_fixture / "input.json").read_text())
        # The fixture's input really carries the fleet roots.
        assert "DcimGroupPolicyProfile" in data
        assert _run_transform(Leaf, data) == (configs / golden / "output.txt").read_text()

    @pytest.mark.parametrize(
        "fixture",
        [
            "leaf_cisco_nxos_gpo_reference",
            "access_leaf_cisco_nxos_gpo_reference",
            "border_leaf_cisco_nxos_gpo_reference",
            "leaf_cisco_nxos_gpo_reference_no_vrf",
        ],
    )
    def test_reference_fixtures_never_render_the_unsafe_lines(self, root_dir: Path, fixture: str) -> None:
        """Never mac-segmentation, intra-group isolation, TCAM vacl, PVLAN or an ``any`` group."""
        lines = (root_dir / "tests/smoke/configs" / fixture / "output.txt").read_text().splitlines()
        code = [line.strip() for line in lines if not line.strip().startswith("!")]
        assert not [line for line in code if "mac-segmentation" in line or "isolation" in line or "vacl" in line]
        assert not [line for line in code if line.startswith("security contract") and " any " in f"{line} "]
        assert "feature security-group" in code
        assert "vlan dot1q tag native" not in "\n".join(lines), "the global native tag is never used"


# ---------------------------------------------------------------------------
# Component groups: where their instances sit (component_coverage), the selectors and the gate
# ---------------------------------------------------------------------------

WEB = "web.pay.demo.local"
DB = "db.pay.demo.local"
PAY_P = ("pay-p",)


def _placed_roots(*, web: list[dict] | None = None, db: list[dict] | None = None) -> dict[str, Any]:
    """The fleet roots with the instances of the web / db component of pay-p replaced (None: the fixture's)."""
    raw = _raw()
    for edge in raw["SecurityTag"]["edges"]:
        tag = edge["node"]
        found = {"tag-pay-web": web, "tag-pay-db": db}[tag["id"]]
        if found is not None:
            tag["components"]["edges"][0]["node"]["instances"] = _edges(found)
    return _roots(raw)


def _coverage(roots: dict[str, Any], dc_id: str = "dc10"):
    contracts = data_center_contracts(roots, dc_id)
    return component_coverage(
        contracts, vtep_names={vtep["name"] for vtep in contracts.vteps}, other_vtep_names={"bl-dc12-01"}
    )


def _server(name: str, *nics: dict) -> dict:
    return _gp_instance(name, list(nics))


def _vm(name: str, host: str, *nics: dict) -> dict:
    return _gp_instance(name, list(nics), host=host)


def _claim(group: str, vtep: str, port: str) -> ComponentClaim:
    return ComponentClaim(group, "seg-pay-p", vtep, port)


def _nic(host: str, nic: str, switch: str, port: str, tags: tuple[str, ...] = PAY_P, **kwargs: Any) -> dict:
    return _gp_nic(host, nic, switch, port, tags, **kwargs)


def _prod_reason(view: dict[str, Any] | None) -> str | None:
    assert view is not None
    return view["vrfs"]["PROD"]["unenforced"]


def _device_inputs(
    name: str, role: str, segments: tuple[str, ...], host_ports: dict[str, tuple[str, ...]] | None = None
) -> dict[str, Any]:
    """The interfaces and activations a device of the fixture fleet passes the view (the transform's own)."""
    raw = build_gpo_device_data(device_name=name, role=role, segments=segments, host_ports=host_ports)
    transform = Leaf.__new__(Leaf)
    device, _roots_, _platform = transform._device_and_platform(raw)
    return {
        "device_name": name,
        "role": role,
        "interfaces": device["interfaces"],
        "activations": transform._device_activations(device),
    }


class TestComponentCoverage:
    """Which (VTEP, port) pairs classify each component group of the data center, and what blocks it."""

    def test_the_fixtures_components_are_claimed_on_the_vtep_that_owns_their_port(self) -> None:
        """A physical instance is its own host (trunk port Ethernet10), a VM walks its hosting device (Ethernet11);
        the host's second NIC on DC12's border leaf is that data center's to classify."""
        coverage = _coverage(_roots(_raw()))
        assert coverage.claims == [
            _claim(DB, "dc1-leaf-01", "Ethernet11"),
            _claim(WEB, "dc1-leaf-01", "Ethernet10"),
        ]
        assert coverage.blockers == {}

    def test_a_lag_member_is_claimed_as_its_port_channel_once(self) -> None:
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-access-leaf-01", "Ethernet20", lag="port-channel20"),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet21", lag="port-channel20"),
            )
        ]
        coverage = _coverage(_placed_roots(web=web))
        assert [claim for claim in coverage.claims if claim.group == WEB] == [
            _claim(WEB, "dc1-access-leaf-01", "port-channel20")
        ]
        assert coverage.blockers == {}

    def test_a_lag_member_without_a_port_channel_blocks_and_is_never_the_member_port(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20", far_role="lag"))]
        coverage = _coverage(_placed_roots(web=web))
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {
            WEB: ["srv-web eno1 reaches dc1-leaf-01 Ethernet20, a LAG member with no port-channel"]
        }

    def test_a_port_that_does_not_carry_the_segment_is_not_claimed_and_blocks(self) -> None:
        """The NIC is cabled to a VTEP of this data center, but the port lacks the segment: the component's traffic
        enters unclassified there, so the group must not be enforced (it used to be silently placed)."""
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20", ("web-np",)))]
        roots = _placed_roots(web=web)
        coverage = _coverage(roots)
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {
            WEB: ["srv-web eno1 reaches dc1-leaf-01 Ethernet20, which is not tagged with the segment"]
        }
        assert _prod_reason(_view(roots)) == (
            "component group web_pay_demo_local: srv-web eno1 reaches dc1-leaf-01 Ethernet20, "
            "which is not tagged with the segment"
        )

    def test_a_port_channel_that_does_not_carry_the_segment_blocks_and_names_the_port_channel(self) -> None:
        """A LAG member: the port-channel is what must carry the segment, whatever the member port is tagged with."""
        member = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20", lag="port-channel20")
        far = member["cable"]["node"]["endpoints"]["edges"][1]["node"]
        far["capabilities"] = _edges([{"id": "seg-pay-p"}])  # the member port is tagged, not the channel
        far["lag"]["node"]["capabilities"] = _edges([{"id": "seg-web-np"}])
        roots = _placed_roots(web=[_server("srv-web", member)])
        coverage = _coverage(roots)
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {
            WEB: ["srv-web eno1 reaches dc1-leaf-01 port-channel20, which is not tagged with the segment"]
        }
        assert _prod_reason(_view(roots)) is not None

    def test_a_far_end_that_is_a_vtep_of_another_data_center_is_ignored_even_without_the_segment(self) -> None:
        """Another data center classifies its own ports: only a VTEP of THIS one is checked for the tag."""
        web = [_server("srv-web", _nic("srv-web", "eno1", "bl-dc12-01", "Ethernet5", ("web-np",)))]
        assert _coverage(_placed_roots(web=web)).blockers == {}

    def test_a_cable_with_only_the_near_end_blocks(self) -> None:
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20")
        nic["cable"]["node"]["endpoints"]["edges"].pop()
        assert _coverage(_placed_roots(web=[_server("srv-web", nic)])).blockers == {
            WEB: ["srv-web eno1: cable has 0 far ends"]
        }

    def test_a_cable_with_more_than_one_far_end_blocks_instead_of_taking_the_first(self) -> None:
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")
        second = _nic("srv-web", "eno1", "dc1-access-leaf-01", "Ethernet2")["cable"]["node"]["endpoints"]["edges"][1]
        nic["cable"]["node"]["endpoints"]["edges"].append(second)
        coverage = _coverage(_placed_roots(web=[_server("srv-web", nic)]))
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {WEB: ["srv-web eno1: cable has 2 far ends"]}

    def test_an_endpoint_without_an_id_is_no_far_end(self) -> None:
        """An endpoint the query's inline fragment did not match comes back empty: it is neither a far end nor
        a reason to ignore the real one."""
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")
        nic["cable"]["node"]["endpoints"]["edges"].append({"node": {}})
        coverage = _coverage(_placed_roots(web=[_server("srv-web", nic)]))
        assert _claim(WEB, "dc1-leaf-01", "Ethernet10") in coverage.claims
        assert coverage.blockers == {}
        lonely = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")
        lonely["cable"]["node"]["endpoints"]["edges"][1] = {"node": {}}
        assert _coverage(_placed_roots(web=[_server("srv-web", lonely)])).blockers == {
            WEB: ["srv-web eno1: cable has 0 far ends"]
        }

    def test_an_instance_only_on_another_data_center_is_not_this_ones(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "bl-dc12-01", "Ethernet5"))]
        coverage = _coverage(_placed_roots(web=web))
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {}

    def test_a_far_end_that_is_a_vtep_of_no_data_center_blocks(self) -> None:
        """A leaf below the VTEP or a firewall cannot classify the port: dc1-spine-01 is no VTEP."""
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-spine-01", "Ethernet1"))]
        coverage = _coverage(_placed_roots(web=web))
        assert coverage.blockers == {WEB: ["srv-web eno1 is cabled to dc1-spine-01, which is a VTEP of no data center"]}

    def test_a_nic_cabled_to_something_that_is_no_switch_port_blocks(self) -> None:
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20")
        far = nic["cable"]["node"]["endpoints"]["edges"][1]["node"]
        del far["name"], far["device"]
        coverage = _coverage(_placed_roots(web=[_server("srv-web", nic)]))
        assert coverage.blockers == {WEB: ["srv-web eno1 is cabled to something that is not a switch port"]}

    def test_an_instance_with_no_hosting_device_blocks(self) -> None:
        vm = _vm("vm-web", "ignored", _nic("ignored", "eno1", "dc1-leaf-01", "Ethernet20"))
        vm["hosting_device"] = _node(None)
        assert _coverage(_placed_roots(web=[vm])).blockers == {WEB: ["instance vm-web has no hosting device"]}

    def test_a_cloud_instance_blocks(self) -> None:
        cloud = {"id": "inst-c", "__typename": "CloudInstance", "name": _v("i-0abc")}
        assert _coverage(_placed_roots(web=[cloud])).blockers == {
            WEB: ["instance i-0abc (CloudInstance) is not an on-prem device"]
        }

    @pytest.mark.parametrize("nics", [[], [None]])
    def test_an_instance_not_cabled_to_a_vtep_blocks(self, nics: list[None]) -> None:
        """No NIC at all, or a NIC with no cable: where the instance sits is not known."""
        found = [_nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20") for _ in nics]
        for nic in found:
            nic["cable"] = _node(None)
        assert _coverage(_placed_roots(web=[_server("srv-web", *found)])).blockers == {
            WEB: ["instance srv-web (host srv-web) is not cabled to a VTEP"]
        }

    def test_a_port_two_components_of_one_segment_land_on_is_claimed_by_neither(self) -> None:
        shared = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet11")
        coverage = _coverage(_placed_roots(web=[_server("srv-web", shared)]))
        assert not [claim for claim in coverage.claims if claim.port == "Ethernet11"]
        text = (
            "port Ethernet11 of dc1-leaf-01 is shared by components db_pay_demo_local, web_pay_demo_local of one "
            "segment (an interface selector cannot tell them apart)"
        )
        assert coverage.blockers[WEB] == [text]
        assert coverage.blockers[DB] == [text]

    def test_the_same_port_in_two_segments_is_two_claims(self) -> None:
        """api and backend share a trunk port in different segments: the selector names the segment's VLAN."""
        raw = _raw()
        raw["SecurityTag"]["edges"].append(
            {
                "node": _gp_tag(
                    "tag-app",
                    60,
                    "app.app.demo.local",
                    "seg-app-np",
                    [_server("srv-app", _nic("srv-app", "eno1", "dc1-leaf-01", "Ethernet10", ("app-np",)))],
                )
            }
        )
        roots = _roots(raw)
        gpo_segment(roots, "seg-app-np")["isolation_mode"] = "microsegmented"
        coverage = _coverage(roots)
        assert ComponentClaim("app.app.demo.local", "seg-app-np", "dc1-leaf-01", "Ethernet10") in coverage.claims
        assert _claim(WEB, "dc1-leaf-01", "Ethernet10") in coverage.claims
        assert coverage.blockers == {}

    def test_a_component_without_an_instance_has_nothing_to_classify(self) -> None:
        coverage = _coverage(_placed_roots(web=[], db=[]))
        assert coverage.claims == []
        assert coverage.blockers == {}

    def test_the_component_groups_of_a_remote_segment_are_not_this_data_centers(self) -> None:
        """pay-p active only in DC12: its components (even unplaced ones) are no local blocker."""
        web = [_server("srv-web")]
        roots = _placed_roots(web=web)
        gpo_segment(roots, "seg-pay-p")["segment_deployments"] = [
            {"vni": 10210, "deployment": {"id": "dc12", "name": "DC12"}}
        ]
        coverage = _coverage(roots)
        assert coverage.claims == []
        assert coverage.blockers == {}

    def test_a_segment_that_is_not_microsegmented_has_no_component_groups_to_classify(self) -> None:
        roots = _placed_roots(web=[_server("srv-lost")])
        gpo_segment(roots, "seg-pay-p")["isolation_mode"] = "normal"
        coverage = _coverage(roots)
        assert coverage.claims == []
        assert coverage.blockers == {}

    def test_only_the_components_of_the_tags_segment_count(self) -> None:
        """One tag naming components of two segments: the one in the other (here unplaced) segment is not pay-p's."""
        raw = _raw()
        web_tag = raw["SecurityTag"]["edges"][0]["node"]
        web_tag["components"]["edges"].append(
            {
                "node": {
                    "fqdn": _v("other.app.demo.local"),
                    "network_segment": _node({"id": "seg-app-np"}),
                    "instances": _edges([_server("srv-lost")]),
                }
            }
        )
        assert _coverage(_roots(raw)).blockers == {}

    def test_an_instance_that_sits_nowhere_blocks_every_data_center_of_its_segment(self) -> None:
        roots = _placed_roots(web=[_server("srv-web")])
        gpo_segment(roots, "seg-pay-p")["segment_deployments"] = [
            {"vni": 10210, "deployment": {"id": "dc10", "name": "DC10"}},
            {"vni": 10210, "deployment": {"id": "dc12", "name": "DC12"}},
        ]
        for dc_id in ("dc10", "dc12"):
            assert WEB in _coverage(roots, dc_id).blockers

    def test_the_order_of_instances_nics_and_components_does_not_matter(self) -> None:
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10", PAY_P),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet2", PAY_P),
                _nic("srv-web", "eno3", "dc1-spine-01", "Ethernet1", PAY_P),
            ),
            _server("srv-web-2", _nic("srv-web-2", "eno1", "dc1-leaf-01", "Ethernet10", PAY_P)),
        ]
        expected = _coverage(_placed_roots(web=web))
        assert expected.claims and expected.blockers
        for seed in range(8):
            shuffled = copy.deepcopy(web)
            rng = random.Random(seed)
            rng.shuffle(shuffled)
            for instance in shuffled:
                rng.shuffle(instance["interfaces"]["edges"])
            roots = _placed_roots(web=shuffled)
            rng.shuffle(roots["SecurityTag"])
            assert _coverage(roots) == expected

    def test_coverage_is_read_from_the_cleaned_query_response(self) -> None:
        """clean_data keeps the typename, turns a null port-channel into None, and the cable's near end (the NIC
        itself, whose device is the host, no VTEP) is skipped: nothing here blocks."""
        raw = _raw()
        nic = raw["SecurityTag"]["edges"][1]["node"]["components"]["edges"][0]["node"]["instances"]["edges"][0]["node"]
        cleaned = clean_data(nic)
        assert cleaned["typename"] == "DcimVirtualDevice"
        nic_cleaned = cleaned["hosting_device"]["interfaces"][0]
        assert nic_cleaned["cable"]["endpoints"][0]["lag"] is None
        assert nic_cleaned["cable"]["endpoints"][0]["id"] == nic_cleaned["id"]
        assert _coverage(_roots(raw)).blockers == {}


class TestComponentSelectors:
    """The vxlan_gpo device view: the interface selectors of the owning VTEP, and the gate."""

    @staticmethod
    def _groups(view: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        assert view is not None
        return {group["name"]: group for group in view["groups"]}

    def test_the_selectors_are_rendered_on_the_vtep_that_owns_the_port_only(self) -> None:
        roots = _roots()
        access = _view(roots, **_device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",)))
        border = _view(roots, device_name="dc1-border-leaf-01", role="border-leaf", interfaces=[])
        leaf = self._groups(_view(roots))
        assert leaf["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet10"}]
        assert leaf["db_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]
        for view in (access, border):
            assert all(group["interfaces"] == [] for group in self._groups(view).values())

    def test_segment_groups_keep_their_vlan_selector_and_no_interfaces(self) -> None:
        groups = self._groups(_view(_roots()))
        assert {name: groups[name]["vlan_id"] for name in ("web-np", "pay-p")} == {"web-np": 110, "pay-p": 210}
        assert all(groups[name]["interfaces"] == [] for name in ("web-np", "app-np", "db-np", "pay-p", "portal-p"))

    def test_both_vpc_peers_render_their_own_port(self) -> None:
        """One NIC per peer (not bonded): each peer classifies the port of its own cable."""
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet11"),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet12"),
            )
        ]
        roots = _placed_roots(web=web, db=[])
        peer = _device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",), {"Ethernet12": PAY_P})
        leaf_view = _view(roots)
        peer_view = _view(roots, **peer)
        assert self._groups(leaf_view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]
        assert self._groups(peer_view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet12"}]
        assert leaf_view is not None and peer_view is not None
        assert leaf_view["vrfs"] == peer_view["vrfs"], "the verdict is the data center's, the same on both"

    def test_a_port_channel_is_the_selectors_interface(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet30", lag="port-channel30"))]
        view = _view(_placed_roots(web=web), interfaces=self._with_port_channel())
        assert self._groups(view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "port-channel30"}]
        assert _prod_reason(view) is None

    @staticmethod
    def _with_port_channel() -> list[dict[str, Any]]:
        return [
            *_leaf_interfaces(),
            {"name": "port-channel30", "typename": "DcimLAGInterface", "capabilities": [{"id": "seg-pay-p"}]},
        ]

    def test_ports_are_sorted_naturally(self) -> None:
        """Ethernet10 sorts after Ethernet2 (and Ethernet11 after both), not alphabetically."""
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet11"),
                _nic("srv-web", "eno2", "dc1-leaf-01", "Ethernet2"),
                _nic("srv-web", "eno3", "dc1-leaf-01", "Ethernet10"),
            )
        ]
        interfaces = [*_leaf_interfaces(), {"name": "Ethernet2", "capabilities": [{"id": "seg-pay-p"}]}]
        view = _view(_placed_roots(web=web, db=[]), interfaces=interfaces)
        assert [port["name"] for port in self._groups(view)["web_pay_demo_local"]["interfaces"]] == [
            "Ethernet2",
            "Ethernet10",
            "Ethernet11",
        ]

    def test_the_view_is_deterministic(self) -> None:
        roots = _roots()
        assert json.dumps(_view(roots), sort_keys=True) == json.dumps(_view(copy.deepcopy(roots)), sort_keys=True)

    def test_selectors_are_rendered_even_while_the_vrf_is_unenforced(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")), _server("srv-lost")]
        view = _view(_placed_roots(web=web))
        assert _prod_reason(view) == (
            "component group web_pay_demo_local: instance srv-lost (host srv-lost) is not cabled to a VTEP"
        )
        assert self._groups(view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet10"}]
        assert self._groups(view)["db_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]


class TestComponentGate:
    """PROD is enforced only with no coverage blocker and no per-device reason; each cause is named."""

    def test_a_vrf_with_every_named_component_classified_is_enforced(self) -> None:
        view = _view(_roots())
        assert view is not None
        assert view["vrfs"]["PROD"]["unenforced"] is None
        assert view["vrfs"]["PROD"]["enforce_tag"] == 0

    @pytest.mark.parametrize(
        ("web", "expected"),
        [
            ([_vm("vm", "h", _nic("h", "eno1", "dc1-leaf-01", "Ethernet10"))], None),
            (
                [_server("srv-lost")],
                "component group web_pay_demo_local: instance srv-lost (host srv-lost) is not cabled to a VTEP",
            ),
            (
                [{"id": "c", "__typename": "CloudInstance", "name": _v("i-1")}],
                "component group web_pay_demo_local: instance i-1 (CloudInstance) is not an on-prem device",
            ),
            (
                [_server("srv", _nic("srv", "eno1", "dc1-spine-01", "Ethernet1"))],
                "component group web_pay_demo_local: srv eno1 is cabled to dc1-spine-01, which is a VTEP of no data center",
            ),
            (
                [_server("srv", _nic("srv", "eno1", "dc1-leaf-01", "Ethernet20", far_role="lag"))],
                "component group web_pay_demo_local: srv eno1 reaches dc1-leaf-01 Ethernet20, a LAG member with no port-channel",
            ),
        ],
    )
    def test_each_blocker_unenforces_the_vrf_and_is_named_in_the_reason(
        self, web: list[dict], expected: str | None
    ) -> None:
        view = _view(_placed_roots(web=web))
        assert _prod_reason(view) == expected
        assert view is not None
        assert (view["vrfs"]["PROD"]["enforce_tag"] is None) is (expected is not None)

    def test_a_shared_port_unenforces_the_vrf_and_names_both_components(self) -> None:
        view = _view(_placed_roots(web=[_server("srv", _nic("srv", "eno1", "dc1-leaf-01", "Ethernet11"))]))
        reason = _prod_reason(view)
        assert reason is not None
        assert reason.count("is shared by components db_pay_demo_local, web_pay_demo_local of one segment") == 2
        assert reason.startswith("component group db_pay_demo_local: port Ethernet11 of dc1-leaf-01")

    def test_a_claimed_port_the_device_does_not_have_unenforces_it(self) -> None:
        """Inconsistent data (or an unconverged generator run): fail closed rather than name a missing port."""
        interfaces = [item for item in _leaf_interfaces() if item["name"] != "Ethernet11"]
        reason = _prod_reason(_view(_roots(), interfaces=interfaces))
        assert (
            reason
            == "component group db_pay_demo_local: port Ethernet11 is not an interface of dc1-leaf-01 tagged with the segment"
        )

    def test_a_claimed_port_not_tagged_with_the_segment_unenforces_it(self) -> None:
        interfaces = [
            {**item, "capabilities": []} if item["name"] == "Ethernet11" else item for item in _leaf_interfaces()
        ]
        assert "port Ethernet11 is not an interface of dc1-leaf-01 tagged with the segment" in (
            _prod_reason(_view(_roots(), interfaces=interfaces)) or ""
        )

    def test_a_segment_without_a_vlan_on_the_device_unenforces_it(self) -> None:
        activations = [item for item in LEAF_ACTIVATIONS if item["segment"]["id"] != "seg-pay-p"]
        reason = _prod_reason(_view(_roots(), activations=activations))
        assert reason is not None
        assert "the segment has no VLAN on dc1-leaf-01, so port Ethernet10 cannot be matched" in reason

    def test_components_with_no_instance_in_the_data_center_do_not_block(self) -> None:
        """DC12's instances, a component with none, and a remote segment's group are no blockers (AC4)."""
        view = _view(_placed_roots(web=[], db=[_vm("vm", "h", _nic("h", "eno1", "bl-dc12-01", "Ethernet5"))]))
        assert _prod_reason(view) is None

    def test_the_verdict_of_the_component_part_is_the_same_on_every_vtep_of_the_data_center(self) -> None:
        roots = _placed_roots(web=[_server("srv-lost")])
        reasons = {
            _prod_reason(_view(roots)),
            _prod_reason(_view(roots, **_device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",)))),
            _prod_reason(_view(roots, device_name="dc1-border-leaf-01", role="border-leaf", interfaces=[])),
        }
        assert len(reasons) == 1
        assert reasons != {None}

    def test_a_claimed_port_this_device_lacks_unenforces_it_here_while_its_vpc_peer_enforces(self) -> None:
        """The coverage part of the verdict is the data center's, the per-device part is not: the peer that lacks
        a claimed port is left unenforced, the one that has all of its own ports enforces (module docstring)."""
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10"),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet12"),
            )
        ]
        roots = _placed_roots(web=web)
        assert _coverage(roots).blockers == {}
        peer = _device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",))  # no Ethernet12
        assert _prod_reason(_view(roots)) is None
        assert _prod_reason(_view(roots, **peer)) == (
            "component group web_pay_demo_local: port Ethernet12 is not an interface of dc1-access-leaf-01 "
            "tagged with the segment"
        )

    def test_an_enforced_vrf_relying_on_component_ports_lists_them_on_every_vtep(self) -> None:
        roots = _roots()
        views = [
            _view(roots),
            _view(roots, **_device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",))),
            _view(roots, device_name="dc1-border-leaf-01", role="border-leaf", interfaces=[]),
        ]
        for view in views:
            assert view is not None
            assert view["vrfs"]["PROD"]["unenforced"] is None
            assert view["vrfs"]["PROD"]["classified_ports"] == "dc1-leaf-01 Ethernet10, dc1-leaf-01 Ethernet11"
            assert view["vrfs"]["NONPROD"]["classified_ports"] is None, "no component group, nothing to disclose"

    def test_the_listed_ports_are_sorted_naturally_and_bounded(self) -> None:
        nics = [_nic("srv-web", f"eno{n}", "dc1-leaf-01", f"Ethernet{n}") for n in range(1, 11)]
        view = _view(_placed_roots(web=[_server("srv-web", *nics)]))
        assert view is not None
        listed = view["vrfs"]["PROD"]["classified_ports"]
        assert listed == ", ".join(f"dc1-leaf-01 Ethernet{n}" for n in range(1, 9)) + " and 3 more"

    def test_the_rendered_enforce_line_says_what_it_relies_on_and_an_unenforced_vrf_does_not(self) -> None:
        rendered = _run_transform(Leaf, _raw())
        lines = rendered.splitlines()
        enforce = [i for i, line in enumerate(lines) if "security enforce tag" in line][-1]  # PROD, after NONPROD
        assert lines[enforce - 1].strip() == (
            "! documented-not-lab-verified: enforcing this VRF relies on native-VLAN frames being classified by "
            "match vlan N interface P and on vPC peer-link behaviour (NX-OS 10.6(1)F guide); "
            "ports: dc1-leaf-01 Ethernet10, dc1-leaf-01 Ethernet11"
        )
        assert rendered.count("relies on native-VLAN frames") == 1, "NONPROD names no component group"
        raw = _raw()
        web_tag = next(edge["node"] for edge in raw["SecurityTag"]["edges"] if edge["node"]["id"] == "tag-pay-web")
        web_tag["components"]["edges"][0]["node"]["instances"] = _edges([_server("srv-lost")])
        blocked = _run_transform(Leaf, raw)
        assert "! unenforced: component group web_pay_demo_local" in blocked
        assert "relies on native-VLAN frames" not in blocked

    def test_a_name_with_a_newline_cannot_leave_the_comment(self) -> None:
        """The reason is rendered as one `! unenforced:` comment line: whitespace in a name collapses."""
        reason = _prod_reason(_view(_placed_roots(web=[_server("srv\nlost")])))
        assert reason is not None
        assert "\n" not in reason and "srv lost" in reason

    def test_a_blocker_of_a_group_no_contract_names_does_not_unenforce_the_vrf(self) -> None:
        """Only the component groups a VRF's contracts name are its business: an orphan tag's is not."""
        raw = _raw()
        raw["SecurityTag"]["edges"].append(
            {"node": _gp_tag("tag-orphan", 70, "orphan.pay.demo.local", "seg-pay-p", [_server("srv-lost")])}
        )
        roots = _roots(raw)
        assert _coverage(roots).blockers, "the orphan group does have a blocker"
        assert _prod_reason(_view(roots)) is None


class TestHostTrunks:
    """PR2: a host port a component selector names is a trunk, by the one mode rule (get_interfaces)."""

    @staticmethod
    def _interfaces(native_vlans: dict[str, int] | None) -> dict[str, dict[str, Any]]:
        raw = _raw()
        transform = Leaf.__new__(Leaf)
        device, _roots_, _platform = transform._device_and_platform(raw)
        activations = transform._device_activations(device)
        found = get_interfaces(
            device["interfaces"], activations=activations, device_name="dc1-leaf-01", native_vlans=native_vlans
        )
        return {item["name"]: item for item in found}

    def test_an_access_port_becomes_a_trunk_with_the_claimed_vlan_native(self) -> None:
        found = self._interfaces({"Ethernet11": 210})
        assert found["Ethernet11"]["mode"] == "trunk"
        assert found["Ethernet11"]["native_vlan"] == 210
        assert found["Ethernet11"]["vlans"] == [210]

    def test_without_claims_the_port_stays_access_and_no_key_is_added(self) -> None:
        for native in (None, {}, {"Ethernet12": 210}):
            found = self._interfaces(native)
            assert found["Ethernet11"]["mode"] is None
            assert "native_vlan" not in found["Ethernet11"]

    def test_a_port_that_is_already_a_trunk_is_left_as_it_is(self) -> None:
        found = self._interfaces({"Ethernet10": 210})
        assert found["Ethernet10"]["mode"] == "trunk"
        assert "native_vlan" not in found["Ethernet10"]
        assert found["Ethernet10"]["vlans"] == [110, 120, 210, 220]

    def test_a_claim_for_another_vlan_than_the_ports_changes_nothing(self) -> None:
        found = self._interfaces({"Ethernet11": 999})
        assert found["Ethernet11"]["mode"] is None
        assert "native_vlan" not in found["Ethernet11"]

    def test_the_rendered_host_port_is_an_edge_trunk_with_a_native_vlan(self) -> None:
        rendered = _run_transform(Leaf, _raw())
        stanza = rendered.split("\ninterface Ethernet11\n")[1].split("\n!\n")[0].splitlines()
        assert [line.strip() for line in stanza if line.strip() and not line.strip().startswith("!")] == [
            "no shutdown",
            "switchport",
            "switchport mode trunk",
            "switchport trunk native vlan 210",
            "switchport trunk allowed vlan 210",
            "spanning-tree port type edge trunk",
        ]
        assert any("documented-not-lab-verified" in line for line in stanza)
        assert "  match vlan 210 interface Ethernet11" in rendered
        assert "  match vlan 210 interface Ethernet10" in rendered

    def test_a_claimed_port_channel_renders_as_an_edge_trunk_with_a_native_vlan(self) -> None:
        """A bonded host: the port-channel (tagged with the segment) is the selector's interface and the trunk."""
        raw = _raw()
        node = raw["DcimDevice"]["edges"][0]["node"]
        node["interfaces"]["edges"].append(
            {
                "node": _make_interface(
                    name="port-channel30",
                    device_name="dc1-leaf-01",
                    role="server",
                    typename="DcimLAGInterface",
                    segments=_gpo_segment_nodes("dc1-leaf-01", PAY_P),
                )
            }
        )
        web_tag = next(edge["node"] for edge in raw["SecurityTag"]["edges"] if edge["node"]["id"] == "tag-pay-web")
        member = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet30", lag="port-channel30")
        web_tag["components"]["edges"][0]["node"]["instances"] = _edges([_server("srv-web", member)])
        rendered = _run_transform(Leaf, raw)
        assert "  match vlan 210 interface port-channel30" in rendered
        assert "  match vlan 210 interface Ethernet30" not in rendered
        stanza = rendered.split("\ninterface port-channel30\n")[1].split("\n!\n")[0].splitlines()
        assert [line.strip() for line in stanza if line.strip() and not line.strip().startswith("!")] == [
            "no shutdown",
            "switchport",
            "switchport mode trunk",
            "switchport trunk native vlan 210",
            "switchport trunk allowed vlan 210",
            "spanning-tree port type edge trunk",
        ]

    def test_the_existing_trunk_gets_no_native_vlan_and_nothing_renders_the_global_native_tag(self) -> None:
        rendered = _run_transform(Leaf, _raw())
        trunk = rendered.split("\ninterface Ethernet10\n")[1].split("\n!\n")[0]
        assert "native vlan" not in trunk and "spanning-tree" not in trunk
        assert "switchport trunk allowed vlan 110,120,210,220" in trunk
        assert "vlan dot1q tag native" not in rendered

    def test_an_unclaimed_access_port_stays_access(self) -> None:
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=LEAF_SEGMENTS,
            host_ports={"Ethernet11": PAY_P, "Ethernet12": PAY_P},
        )
        stanza = _run_transform(Leaf, raw).split("interface Ethernet12\n")[1].split("\n!\n")[0]
        assert "switchport mode access" in stanza
        assert "switchport access vlan 210" in stanza
        assert "native vlan" not in stanza

    @staticmethod
    def _context(raw: dict, root_dir: Path) -> tuple[dict[str, Any], MagicMock]:
        """The template context of ``raw`` (``_template_context``, what transform() renders) and the
        get_interfaces spy of that one call."""
        transform = Leaf.__new__(Leaf)
        transform.root_directory = str(root_dir)
        device, roots, platform = transform._device_and_platform(copy.deepcopy(raw))
        device["segment_deployments"] = transform._device_activations(device)
        with patch("transforms.common.get_interfaces", wraps=get_interfaces) as spy:
            return transform._template_context(device, platform or "", roots), spy

    def test_the_transform_hands_the_templates_the_trunked_interfaces_only_for_claimed_ports(
        self, root_dir: Path
    ) -> None:
        config, _spy = self._context(_raw(), root_dir)
        by_name = {item["name"]: item for item in config["interfaces"]}
        assert by_name["Ethernet11"]["native_vlan"] == 210
        assert "native_vlan" not in by_name["Ethernet10"]
        assert not any("native_vlan" in item for name, item in by_name.items() if name not in ("Ethernet11",))

    def test_a_vtep_owning_a_claimed_port_builds_its_interfaces_once_with_the_native_vlans(
        self, root_dir: Path
    ) -> None:
        config, spy = self._context(_raw(), root_dir)
        spy.assert_called_once()
        # Ethernet10 (the trunk of the web component) is named too; get_interfaces leaves a trunk as it is.
        assert spy.call_args.kwargs["native_vlans"] == {"Ethernet10": 210, "Ethernet11": 210}
        assert config["group_policy"]["groups"], "the claimed port comes from the group policy of the same context"

    def test_a_vtep_without_a_claimed_port_builds_its_interfaces_once_without_native_vlans(
        self, root_dir: Path
    ) -> None:
        """An enablement-only VTEP (no tenant VRF) and a device outside a reference fabric."""
        for raw in (
            build_gpo_device_data(device_name="dc1-leaf-01", role="leaf", segments=()),
            build_gpo_device_data(
                device_name="dc1-leaf-01", role="leaf", segments=LEAF_SEGMENTS, leaf_type="N9K-C9336C-FX2"
            ),
        ):
            _config, spy = self._context(raw, root_dir)
            spy.assert_called_once()
            assert spy.call_args.kwargs["native_vlans"] is None

    def test_the_context_keeps_the_extra_config_over_the_base_config_and_the_base_loopback(
        self, root_dir: Path
    ) -> None:
        """The same keys as the two parts merged the old way (base first, then extra on top)."""
        transform = Leaf.__new__(Leaf)
        transform.root_directory = str(root_dir)
        device, roots, platform = transform._device_and_platform(copy.deepcopy(_raw()))
        device["segment_deployments"] = transform._device_activations(device)
        config = transform._template_context(device, platform or "", roots)
        base = transform._build_config(device, platform or "")
        extra = transform._extra_config(device, platform or "", extra_roots=roots)
        assert config.keys() == {**base, **extra}.keys()
        assert config["loopback_name"] == base["loopback_name"]
        assert {key: config[key] for key in extra} == extra
        assert [i["name"] for i in config["interfaces"]] == [i["name"] for i in base["interfaces"]]

    def test_a_non_reference_device_is_not_touched(self) -> None:
        """No claim, no group policy: get_interfaces is called once, without native VLANs."""
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=LEAF_SEGMENTS,
            host_ports={"Ethernet11": PAY_P},
            leaf_type="N9K-C9336C-FX2",
        )
        rendered = _run_transform(Leaf, raw)
        assert "native vlan" not in rendered and "security-group" not in rendered
        assert "switchport mode access" in rendered


# ---------------------------------------------------------------------------
# The query fragment is what component_coverage reads
# ---------------------------------------------------------------------------


class _Spy(dict):
    """Cleaned query data that records the path of every key read from it, and from the dicts below it."""

    def __init__(self, data: dict, path: tuple[str, ...], log: set[tuple[str, ...]]) -> None:
        super().__init__(data)
        self._path, self._log = path, log

    def get(self, key: Any, default: Any = None, /) -> Any:
        self._log.add((*self._path, key))
        return self._wrap(key, super().get(key, default))

    def __getitem__(self, key: Any) -> Any:
        self._log.add((*self._path, key))
        return self._wrap(key, super().__getitem__(key))

    def _wrap(self, key: str, value: Any) -> Any:
        return _spied(value, (*self._path, key), self._log)


def _spied(value: Any, path: tuple[str, ...], log: set[tuple[str, ...]]) -> Any:
    if isinstance(value, dict):
        return _Spy(value, path, log)
    if isinstance(value, list):
        return [_spied(item, path, log) for item in value]
    return value


class TestPlacementFragment:
    """queries/fragments/group_policy.gql SecurityTagPlacementFields is tied to the code that reads its data."""

    # clean_data collapses ``edges { node }``, ``node`` and ``{ value }``, and renames ``__typename`` to ``typename``.
    COLLAPSED = frozenset({"edges", "node", "value"})
    # AppInstance kind -> the inline fragment of the placement fragment that must select its host's NICs.
    HOST_FRAGMENT = {
        "DcimPhysicalDevice": "DcimDevice",
        "ManagedControllerPhysical": "DcimDevice",
        "DcimVirtualDevice": "DcimVirtualDevice",
        "ManagedControllerVirtual": "ManagedControllerVirtual",
    }

    @classmethod
    def _paths(
        cls,
        selections: Any,
        fragments: dict[str, FragmentDefinitionNode],
        prefix: tuple[str, ...] = (),
    ) -> set[tuple[str, ...]]:
        """Every field path below ``selections`` as the cleaned data names it (inline fragments merged, named
        spreads resolved from ``fragments``)."""
        found: set[tuple[str, ...]] = set()
        for selection in selections:
            if isinstance(selection, FragmentSpreadNode):
                found |= cls._paths(fragments[selection.name.value].selection_set.selections, fragments, prefix)
            elif isinstance(selection, InlineFragmentNode):
                found |= cls._paths(selection.selection_set.selections, fragments, prefix)
            elif isinstance(selection, FieldNode):
                name = (selection.alias or selection.name).value
                path = prefix if name in cls.COLLAPSED else (*prefix, "typename" if name == "__typename" else name)
                found |= ({path} if path else set()) | cls._paths(
                    selection.selection_set.selections if selection.selection_set else (), fragments, path
                )
        return found

    @staticmethod
    def _fragments(root_dir: Path) -> dict[str, FragmentDefinitionNode]:
        """Every fragment of queries/fragments/group_policy.gql by name."""
        document = parse((root_dir / "queries/fragments/group_policy.gql").read_text())
        return {item.name.value: item for item in document.definitions if isinstance(item, FragmentDefinitionNode)}

    @classmethod
    def _fragment(cls, root_dir: Path, name: str) -> FragmentDefinitionNode:
        return cls._fragments(root_dir)[name]

    @staticmethod
    def _sub(selections: Any, name: str) -> Any:
        """The selections of the field ``name`` among ``selections``."""
        field = next(item for item in selections if isinstance(item, FieldNode) and item.name.value == name)
        assert field.selection_set is not None
        return field.selection_set.selections

    @classmethod
    def _below(cls, selections: Any, *names: str) -> list[SelectionNode]:
        """The selections of the node reached through the connections ``names`` (``edges { node }`` each)."""
        for name in names:
            selections = cls._sub(cls._sub(cls._sub(selections, name), "edges"), "node")
        return list(selections)

    @staticmethod
    def _instance(kind: str, name: str, nics: list[dict]) -> dict:
        found = _gp_instance(name, nics, host=name if "Virtual" in kind else None)
        found["__typename"] = kind
        return found

    def _recorded_reads(self, kind: str) -> set[tuple[str, ...]]:
        """The key paths below a SecurityTag that component_coverage reads for one instance of ``kind``, host
        cabled through a plain port and a LAG member (port-channel)."""
        nics = [
            _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10"),
            _nic("srv-web", "eno2", "dc1-leaf-01", "Ethernet20", lag="port-channel20"),
            _nic("srv-web", "eno3", "bl-dc12-01", "Ethernet5"),
        ]
        roots = _placed_roots(web=[self._instance(kind, "srv-web", nics)], db=[])
        log: set[tuple[str, ...]] = set()
        roots["SecurityTag"] = _spied(roots["SecurityTag"], (), log)
        coverage = _coverage(roots)
        assert coverage.claims, "the spied data must be placed, or the read paths are a prefix of the real ones"
        return log

    @pytest.mark.parametrize("kind", sorted(HOST_FRAGMENT))
    def test_the_fragment_selects_every_key_component_coverage_reads(self, root_dir: Path, kind: str) -> None:
        """Every key the coverage reads below a tag must be selected: the instance's own fields and the inline
        fragment of ITS kind (a path selected only for another kind does not count), the rest by the two fragments."""
        placement = self._fragment(root_dir, "SecurityTagPlacementFields").selection_set.selections
        shared = self._fragment(root_dir, "SecurityTagGroupPolicyFields").selection_set.selections
        instance = self._below(placement, "components", "instances")
        inline = [item for item in instance if isinstance(item, InlineFragmentNode)]
        assert {item.type_condition.name.value for item in inline} == set(self.HOST_FRAGMENT.values())
        own_kind = [item for item in inline if item.type_condition.name.value == self.HOST_FRAGMENT[kind]]
        fragments = self._fragments(root_dir)
        instance_paths = self._paths(
            [item for item in instance if item not in inline] + own_kind, fragments, ("components", "instances")
        )
        tag_paths = self._paths(placement, fragments) | self._paths(shared, fragments)

        reads = self._recorded_reads(kind)
        host = ("components", "instances") + (("hosting_device",) if "Virtual" in kind else ())
        assert (*host, "interfaces", "cable", "endpoints", "lag", "capabilities", "id") in reads, (
            "the data must exercise the whole walk down to the port-channel's tags"
        )
        missing = {
            path
            for path in reads
            if path not in (instance_paths if path[:2] == ("components", "instances") else tag_paths)
            and path not in {("components",), ("components", "instances")}
        }
        assert not missing, f"{kind}: read by component_coverage but not selected for it: {sorted(missing)}"

    def test_the_nic_roles_the_fragment_filters_on_are_the_ones_the_generator_cables(self, root_dir: Path) -> None:
        fragment = self._fragment(root_dir, "SecurityTagPlacementFields")
        found: list[list[str]] = []

        def walk(selections: Any) -> None:
            for selection in selections:
                if isinstance(selection, FieldNode) and selection.name.value == "interfaces":
                    for argument in selection.arguments:
                        assert argument.name.value == "role__values"
                        found.append([value.value for value in argument.value.values])
                if getattr(selection, "selection_set", None):
                    walk(selection.selection_set.selections)

        walk(fragment.selection_set.selections)
        assert found == [list(ENDPOINT_NIC_ROLES)] * 3, "one filter per host kind"

    def test_the_nic_block_is_one_fragment_spread_under_a_field_in_each_host_branch(self, root_dir: Path) -> None:
        """HostNicFields is the single NIC selection: spread as ``interfaces(...) { edges { node { ...F } } }`` of
        each host kind, so the three branches cannot drift."""
        placement = self._fragment(root_dir, "SecurityTagPlacementFields").selection_set.selections
        instance = self._below(placement, "components", "instances")
        spread_in: list[str] = []
        for branch in (item for item in instance if isinstance(item, InlineFragmentNode)):
            selections = branch.selection_set.selections
            if branch.type_condition.name.value != "DcimDevice":
                selections = self._sub(self._sub(selections, "hosting_device"), "node")
            node = self._below(selections, "interfaces")
            assert len(node) == 1
            spread = node[0]
            assert isinstance(spread, FragmentSpreadNode)
            spread_in.append(f"{branch.type_condition.name.value}:{spread.name.value}")
        assert spread_in == [
            f"{kind}:HostNicFields" for kind in ("DcimDevice", "DcimVirtualDevice", "ManagedControllerVirtual")
        ]

    def test_no_named_spread_sits_directly_under_an_inline_fragment(self, root_dir: Path) -> None:
        """``... on X { ...F }`` resolves to nulls on a live Infrahub (commit 7a38de0e); a spread under a field
        inside the inline fragment (``node { ...F }``) works. Holds for every fragment of the file."""
        offenders = [
            f"{name}: ... on {inline.type_condition.name.value} {{ ...{spread.name.value} }}"
            for name, definition in self._fragments(root_dir).items()
            for inline in self._inline_fragments(definition)
            for spread in inline.selection_set.selections
            if isinstance(spread, FragmentSpreadNode)
        ]
        assert not offenders, offenders

    @classmethod
    def _inline_fragments(cls, node: Any) -> list[InlineFragmentNode]:
        """Every inline fragment at any depth below ``node``."""
        selection_set = getattr(node, "selection_set", None)
        found: list[InlineFragmentNode] = []
        for selection in selection_set.selections if selection_set else ():
            if isinstance(selection, InlineFragmentNode):
                found.append(selection)
            found += cls._inline_fragments(selection)
        return found
