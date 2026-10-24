"""Unit tests for the NX-OS VXLAN GPO device view (transforms/helpers/group_policy/ _vxlan_gpo_build).

The fleet is the smoke fixture's (tests/smoke/fixture_builders/group_policy.py ``_gpo_fleet_roots``): DC10 with a
leaf, an access leaf and a border leaf in the NX-OS row, a legacy DC12, NONPROD (fully classifiable by VLAN,
db-np stretched to DC12) and PROD (a microsegmented segment whose contracts name component groups: the web
component sits on the leaf's trunk port Ethernet10, the db component, a VM, on its access port Ethernet11).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.smoke.fixture_builders.group_policy import (
    _GPO_EOS,
    _GPO_NXOS,
    _gp_device,
    _gp_memory_gb,
    build_gpo_device_data,
    gpo_segment,
)
from tests.smoke.test_configs_smoke import _run_transform
from tests.unit.gpo_device_view_helpers import LEAF_ACTIVATIONS, VRFS, _raw, _roots, _view
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
    class_map_name,
    data_center_contracts,
    segment_vrf,
    vxlan_gpo_view,
)
from transforms.helpers.vxlan import get_vxlan_config
from utils.data_cleaning import clean_data
from utils.fabric_roles import DC_VTEP_ROLES


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
