"""Unit tests for utils/group_policy.py: release ordering, profile rows, VTEP and site reasons."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from graphql import parse, print_ast
from graphql.language import FieldNode, FragmentDefinitionNode, OperationDefinitionNode

from utils.fabric_roles import DC_VTEP_ROLES, VTEP_ROLES
from utils.group_policy import (
    fabric_reason,
    gbp_required,
    profiles_by_platform,
    release_key,
    reserved_groups,
    segment_sites,
    site_kind,
    site_reason,
    site_vteps,
    tag_pool,
    tags_by_segment,
    vteps_by_data_center,
)

NXOS = {
    "name": "reference-cisco-nxos",
    "group_policy_kind": "vxlan_gpo",
    "platform": {"name": "cisco_nxos"},
    "min_release": "10.6(1)F",
    "min_memory_gb": 24,
    "device_types": [{"name": "N9K-C93180YC-FX3"}, {"name": "N9K-C9364C-GX"}],
    "tag_pool": {"id": "pool-sgt", "name": "SGT-POOL", "start_range": 16, "end_range": 4095},
}
EOS = {
    "name": "reference-arista-eos",
    "group_policy_kind": "segment_security",
    "platform": {"name": "arista_eos"},
    "min_release": "4.36.2F",
    "min_memory_gb": None,
    "device_types": [{"name": "DCS-7280SR3-48YC8"}],
    "tag_pool": None,
}
PROFILES = {"cisco_nxos": NXOS, "arista_eos": EOS}


def _device(
    name: str = "lf-1",
    *,
    platform: str | None = "cisco_nxos",
    device_type: str | None = "N9K-C93180YC-FX3",
    memory_gb: int | None = 32,
    version: str | None = "10.6(1)F",
    role: str = "leaf",
) -> dict[str, Any]:
    """A cleaned DeviceGroupPolicyFields device, in the NX-OS row by default."""
    return {
        "name": name,
        "role": role,
        "platform": {"name": platform} if platform else None,
        "device_type": {"name": device_type, "memory_gb": memory_gb} if device_type else None,
        "software_image": {"version": version} if version else None,
    }


def _eos(name: str = "lf-1", **kwargs: Any) -> dict[str, Any]:
    """A cleaned EOS device in the EOS row by default (no memory recorded)."""
    defaults: dict[str, Any] = {
        "platform": "arista_eos",
        "device_type": "DCS-7280SR3-48YC8",
        "memory_gb": None,
        "version": "4.36.2F",
    }
    return _device(name, **{**defaults, **kwargs})


def _bootstrap(root_dir: Path, path: str) -> list[dict[str, Any]]:
    """The data list of one bootstrap object file."""
    return yaml.safe_load((root_dir / path).read_text())["spec"]["data"]


def _cleaned_bootstrap_rows(root_dir: Path) -> list[dict[str, Any]]:
    """The bootstrap profile rows as the cleaned GroupPolicyProfileFields (+ tag_pool) selection."""
    pools = {pool["name"]: pool for pool in _bootstrap(root_dir, "data/bootstrap/19_sgt_pool.yml")}
    return [
        {
            "name": row["name"],
            "group_policy_kind": row["group_policy_kind"],
            "platform": {"name": row["platform"]},
            "min_release": row["min_release"],
            "min_memory_gb": row.get("min_memory_gb"),
            "device_types": [{"name": name} for name in row["device_types"]],
            "tag_pool": {"id": row["tag_pool"], **pools[row["tag_pool"]]} if row.get("tag_pool") else None,
        }
        for row in _bootstrap(root_dir, "data/bootstrap/19b_group_policy_profile.yml")
    ]


class TestReleaseKey:
    """Releases compare by their numbers, within one platform."""

    def test_nxos_releases_are_ordered(self) -> None:
        """AC1: 10.3(3) < 10.6(1)F < 10.6(3)F."""
        assert release_key("10.3(3)") < release_key("10.6(1)F") < release_key("10.6(3)F")

    def test_eos_releases_are_ordered(self) -> None:
        """The repo's 4.32.1F is below the EOS floor 4.36.2F."""
        assert release_key("4.32.1F") < release_key("4.36.2F")

    @pytest.mark.parametrize(
        ("version", "key"), [("10.6(1)F", (10, 6, 1)), ("4.36.2F", (4, 36, 2)), ("9.3(11)", (9, 3, 11))]
    )
    def test_numbers_are_extracted_in_order(self, version: str, key: tuple[int, ...]) -> None:
        """Every number of the release string, in order, ignoring the train letter."""
        assert release_key(version) == key

    @pytest.mark.parametrize("version", [None, "", "latest"])
    def test_unparseable_release_is_empty(self, version: str | None) -> None:
        """Empty means "does not parse"; callers fail closed on it."""
        assert release_key(version) == ()


class TestProfilesByPlatform:
    """One usable row per platform, everything else fails closed with a problem."""

    def test_one_row_per_platform(self) -> None:
        """The two reference rows map to their platforms, no problem."""
        assert profiles_by_platform([NXOS, EOS]) == (PROFILES, [])

    def test_no_row_is_a_problem(self) -> None:
        """Without rows no fabric is a reference fabric."""
        assert profiles_by_platform([]) == ({}, ["no DcimGroupPolicyProfile row: no fabric is a reference fabric"])

    def test_two_rows_of_one_platform_leave_it_without_a_row(self) -> None:
        """A duplicate is ambiguous: the platform gets no row, the other platform keeps its own."""
        profiles, problems = profiles_by_platform([NXOS, {**NXOS, "name": "twin"}, EOS])
        assert profiles == {"arista_eos": EOS}
        assert problems == [
            "platform cisco_nxos is in 2 group-policy profiles (reference-cisco-nxos, twin), expected one"
        ]

    @pytest.mark.parametrize(
        ("change", "problem"),
        [
            ({"device_types": []}, "group-policy profile reference-cisco-nxos lists no device type"),
            (
                {"min_release": "latest"},
                "group-policy profile reference-cisco-nxos min_release 'latest' does not parse",
            ),
            ({"group_policy_kind": None}, "group-policy profile reference-cisco-nxos has no group_policy_kind"),
            ({"platform": None}, "group-policy profile reference-cisco-nxos has no platform"),
        ],
    )
    def test_unusable_row_is_a_problem(self, change: dict[str, Any], problem: str) -> None:
        """A row without device types, kind or platform, or with an unparseable floor, is never used."""
        profiles, problems = profiles_by_platform([{**NXOS, **change}])
        assert profiles == {}
        assert problems == [problem]

    def test_bootstrap_rows_are_usable_and_reference_loaded_data(self, root_dir: Path) -> None:
        """AC1: one row each for cisco_nxos and arista_eos; their platforms, device types and pool exist."""
        rows = _cleaned_bootstrap_rows(root_dir)
        profiles, problems = profiles_by_platform(rows)
        assert problems == []
        assert sorted(profiles) == ["arista_eos", "cisco_nxos"]
        platforms = {item["name"] for item in _bootstrap(root_dir, "data/bootstrap/05_platforms.yml")}
        device_types = {item["name"] for item in _bootstrap(root_dir, "data/bootstrap/06_device_types.yml")}
        assert set(profiles) <= platforms
        for row in rows:
            assert {item["name"] for item in row["device_types"]} <= device_types, row["name"]
        assert tag_pool(rows)[0] is not None

    def test_bootstrap_nxos_row_is_the_fx3_gx_gx2_family_with_enough_memory(self, root_dir: Path) -> None:
        """Owner decision: FX3/GX/GX2 only (no FX2, FX, EX), each with memory_gb >= the row's floor."""
        nxos = profiles_by_platform(_cleaned_bootstrap_rows(root_dir))[0]["cisco_nxos"]
        names = {item["name"] for item in nxos["device_types"]}
        assert len(names) == 9
        assert all(re.search(r"-(FX3|GX|GX2[AB])$", name) for name in names)
        memory = {
            item["name"]: item.get("memory_gb", 0)
            for item in _bootstrap(root_dir, "data/bootstrap/06_device_types.yml")
        }
        assert all(memory[name] >= nxos["min_memory_gb"] for name in names)

    def test_sgt_pool_is_16_to_4095(self, root_dir: Path) -> None:
        """AC3: clear of 0, 1 and 15, inside the NX-OS group ID; the NX-OS row names it."""
        pool, problem = tag_pool(_cleaned_bootstrap_rows(root_dir))
        assert problem is None
        assert pool is not None
        assert (pool["name"], pool["start_range"], pool["end_range"]) == ("SGT-POOL", 16, 4095)


class TestTagPool:
    """Group IDs are fabric-wide: the rows that name a pool all name one."""

    def test_one_pool_named_by_one_row(self) -> None:
        """The EOS row names none; the NX-OS row's pool is the pool."""
        assert tag_pool([NXOS, EOS]) == (NXOS["tag_pool"], None)

    def test_two_rows_naming_the_same_pool(self) -> None:
        """The same pool twice is still one pool."""
        assert tag_pool([NXOS, {**EOS, "tag_pool": NXOS["tag_pool"]}])[0] == NXOS["tag_pool"]

    def test_no_pool_fails_closed(self) -> None:
        """No row names a pool."""
        assert tag_pool([EOS]) == (None, "the group-policy profiles name 0 tag pools [], expected one")

    def test_two_pools_fail_closed(self) -> None:
        """Different pools would let one number mean two groups."""
        other = {**EOS, "tag_pool": {"id": "pool-x", "name": "X-POOL"}}
        assert tag_pool([NXOS, other]) == (
            None,
            "the group-policy profiles name 2 tag pools ['SGT-POOL', 'X-POOL'], expected one",
        )


class TestFabricReason:
    """AC1: one VTEP against its platform's row, failing closed with the first failing property."""

    @pytest.mark.parametrize("version", ["10.6(1)F", "10.6(3)F"])
    @pytest.mark.parametrize("device_type", ["N9K-C93180YC-FX3", "N9K-C9364C-GX"])
    def test_nxos_reference_hardware_on_the_floor_or_later_is_in(self, device_type: str, version: str) -> None:
        """FX3/GX on 10.6(1)F or later with 32 GB is in the NX-OS row."""
        assert fabric_reason(_device(device_type=device_type, version=version), PROFILES) is None

    @pytest.mark.parametrize("device_type", ["N9K-C93180YC-EX", "N9K-C9336C-FX2"])
    def test_device_type_not_in_the_row_is_out(self, device_type: str) -> None:
        """EX and FX2 are not listed in the row."""
        assert (
            fabric_reason(_device(device_type=device_type), PROFILES)
            == "device type not in profile reference-cisco-nxos"
        )

    def test_release_below_the_floor_is_out(self) -> None:
        """10.3(3), today's image, is below the row's 10.6(1)F."""
        assert fabric_reason(_device(version="10.3(3)"), PROFILES) == "release 10.3(3) < 10.6(1)F"

    def test_memory_below_the_floor_is_out(self) -> None:
        """memory_gb is read at runtime: a device type edited to 16 GB fails."""
        assert fabric_reason(_device(memory_gb=16), PROFILES) == "memory 16 GB < 24 GB"

    def test_memory_on_the_floor_is_in(self) -> None:
        """24 GB meets a 24 GB floor."""
        assert fabric_reason(_device(memory_gb=24), PROFILES) is None

    def test_unknown_memory_fails_closed_when_the_row_has_a_floor(self) -> None:
        """A device type without memory_gb is not assumed to have enough."""
        assert fabric_reason(_device(memory_gb=None), PROFILES) == "memory unknown"

    def test_row_without_a_memory_floor_does_not_read_memory(self) -> None:
        """The EOS row documents no memory floor: an EOS VTEP without memory_gb is in."""
        assert fabric_reason(_eos(), PROFILES) is None

    def test_eos_release_below_the_floor_is_out(self) -> None:
        """The repo's EOS image 4.32.1F is below the row's 4.36.2F."""
        assert fabric_reason(_eos(version="4.32.1F"), PROFILES) == "release 4.32.1F < 4.36.2F"

    def test_eos_trident_type_is_out(self) -> None:
        """The demos' 7050X3 is not listed (MSS-G support unknown)."""
        reason = fabric_reason(_eos(device_type="DCS-7050CX3-32C-R"), PROFILES)
        assert reason == "device type not in profile reference-arista-eos"

    def test_missing_image_fails_closed(self) -> None:
        """No software image, no release to compare."""
        assert fabric_reason(_device(version=None), PROFILES) == "no software image"

    def test_unparseable_release_fails_closed(self) -> None:
        """A release without numbers does not compare."""
        assert fabric_reason(_device(version="latest"), PROFILES) == "release 'latest' does not parse"

    @pytest.mark.parametrize(
        ("platform", "reason"),
        [
            ("sonic", "no group-policy profile for platform sonic (L0 only)"),
            (None, "no group-policy profile for platform missing (L0 only)"),
        ],
    )
    def test_platform_without_a_row_is_out(self, platform: str | None, reason: str) -> None:
        """A platform without a row keeps L0 only, whatever its hardware."""
        assert fabric_reason(_device(platform=platform), PROFILES) == reason

    def test_no_rows_at_all_fails_closed(self) -> None:
        """Without profile rows nothing is in."""
        assert fabric_reason(_device(), {}) == "no group-policy profile for platform cisco_nxos (L0 only)"

    def test_missing_device_type_fails_closed(self) -> None:
        """No device type, no row match."""
        assert fabric_reason(_device(device_type=None), PROFILES) == "no device type"


class TestSite:
    """A site is a reference fabric only when every VTEP is in its row and the rows share one kind."""

    def test_all_reference_vteps_make_a_reference_site_of_one_kind(self) -> None:
        """Leaf, access leaf and border leaf all in the NX-OS row."""
        vteps = [_device("lf-1"), _device("al-1", role="access-leaf"), _device("bl-1", role="border-leaf")]
        assert site_reason(vteps, PROFILES) is None
        assert site_kind(vteps, PROFILES) == "vxlan_gpo"

    def test_eos_site_is_a_segment_security_site(self) -> None:
        """An all-7280R3 site on 4.36.2F is a reference fabric of the EOS kind."""
        vteps = [_eos("lf-1"), _eos("lf-2")]
        assert site_reason(vteps, PROFILES) is None
        assert site_kind(vteps, PROFILES) == "segment_security"

    def test_one_vtep_outside_names_it(self) -> None:
        """The EX access leaf alone makes the site not a reference fabric (mixed sites are unsupported)."""
        vteps = [_device("lf-1"), _device("al-1", role="access-leaf", device_type="N9K-C93180YC-EX")]
        assert site_reason(vteps, PROFILES) == (
            "access-leaf al-1 N9K-C93180YC-EX: device type not in profile reference-cisco-nxos"
        )
        assert site_kind(vteps, PROFILES) == "vxlan_gpo"

    def test_shared_cause_is_listed_once_with_a_count(self) -> None:
        """Three VTEPs on 10.3(3): one cause, the first by name, and how many more."""
        vteps = [_device(f"lf-{n}", version="10.3(3)") for n in (3, 1, 2)]
        assert site_reason(vteps, PROFILES) == "leaf lf-1 N9K-C93180YC-FX3: release 10.3(3) < 10.6(1)F (and 2 more)"

    def test_vteps_of_two_kinds_are_not_a_reference_site(self) -> None:
        """Every VTEP is in its own row, but NX-OS GPO and EOS MSS-G do not make one fabric."""
        vteps = [_device("lf-1"), _eos("lf-2")]
        assert site_reason(vteps, PROFILES) == "VTEPs mix group-policy kinds segment_security, vxlan_gpo"
        assert site_kind(vteps, PROFILES) is None

    def test_site_without_vteps_is_not_reference(self) -> None:
        """Nothing to enforce on, and no kind."""
        assert site_reason([], PROFILES) == "no VTEPs"
        assert site_kind([], PROFILES) is None

    def test_site_of_a_platform_without_a_row_has_no_kind(self) -> None:
        """A SONiC fabric keeps L0 only."""
        vteps = [_device("lf-1", platform="sonic"), _device("lf-2", platform="sonic")]
        assert site_reason(vteps, PROFILES) == (
            "leaf lf-1 N9K-C93180YC-FX3: no group-policy profile for platform sonic (L0 only) (and 1 more)"
        )
        assert site_kind(vteps, PROFILES) is None

    def test_site_vteps_keeps_only_data_center_vtep_roles(self) -> None:
        """Spines, servers and the colocation edge are no data-center VTEPs; the rest sorted by name."""
        devices = [_device("sp-1", role="spine"), _device("srv-1", role="server"), _device("e-1", role="edge")]
        devices += [_device("lf-2"), _device("bl-1", role="border-leaf")]
        assert [device["name"] for device in site_vteps(devices)] == ["bl-1", "lf-2"]

    def test_dc_vtep_roles_are_the_vtep_roles_without_edge(self) -> None:
        """One role source (utils/fabric_roles.py): the transforms' VTEPs minus the colocation edge."""
        assert DC_VTEP_ROLES == VTEP_ROLES - {"edge"}
        assert {"leaf", "access-leaf", "tor", "border-leaf", "border-spine"} == DC_VTEP_ROLES


class TestGbpRequired:
    """Only a microsegmented segment with two or more tagged components needs group policy."""

    @pytest.mark.parametrize(
        ("mode", "count", "required"),
        [("microsegmented", 2, True), ("microsegmented", 1, False), ("normal", 3, False), (None, 2, False)],
    )
    def test_required(self, mode: str | None, count: int, required: bool) -> None:
        """Microsegmented and at least two tags."""
        assert gbp_required({"isolation_mode": mode}, count) is required

    def test_tags_are_grouped_by_their_components_segment(self) -> None:
        """A tag counts for the segment of its component; a tag without components counts nowhere."""
        tags = [
            {"id": "t1", "components": [{"network_segment": {"id": "s1"}}]},
            {"id": "t2", "components": [{"network_segment": {"id": "s1"}}]},
            {"id": "t3", "components": []},
        ]
        assert {seg: [tag["id"] for tag in found] for seg, found in tags_by_segment(tags).items()} == {
            "s1": ["t1", "t2"]
        }


class TestQueries:
    """The queries select what the profile functions read, and filter no role by literal."""

    @pytest.mark.parametrize(
        "query", ["queries/validation/reference_fabric.gql", "queries/topology/segmentation_matrix.gql"]
    )
    def test_queries_read_the_profile_rows_and_no_role_literal(self, root_dir: Path, query: str) -> None:
        """Rows through the shared fragment; VTEP roles come from utils/fabric_roles.py, not the query."""
        text = (root_dir / query).read_text()
        assert "...GroupPolicyProfileFields" in text
        assert "...DeviceGroupPolicyFields" in text
        assert "role__values" not in text

    @staticmethod
    def _roots(text: str) -> dict[str, str]:
        """Response key -> printed selection of every top-level root of the query operation."""
        operation = next(item for item in parse(text).definitions if isinstance(item, OperationDefinitionNode))
        return {
            (field.alias or field.name).value: print_ast(field)
            for field in operation.selection_set.selections
            if isinstance(field, FieldNode)
        }

    @staticmethod
    def _fragment(root_dir: Path, name: str) -> str:
        """The printed definition of one fragment of queries/fragments/group_policy.gql."""
        document = parse((root_dir / "queries/fragments/group_policy.gql").read_text())
        return next(
            print_ast(item)
            for item in document.definitions
            if isinstance(item, FragmentDefinitionNode) and item.name.value == name
        )

    DEVICE_QUERIES = ("queries/config/leaf.gql", "queries/config/border_leaf.gql", "queries/config/border_spine.gql")
    PLACEMENT_SPREAD = "      ...SecurityTagPlacementFields\n"

    @pytest.mark.parametrize("query", DEVICE_QUERIES)
    def test_device_queries_carry_the_fleet_roots_of_the_matrix_after_the_device_root(
        self, root_dir: Path, query: str
    ) -> None:
        """The NX-OS GPO block reads the matrix's roots (minus ``target``) as top-level roots; DcimDevice stays first.

        The SecurityTag root is the matrix's plus the placement of each component's instances
        (``SecurityTagPlacementFields``, component_coverage): the only difference.
        """
        matrix = self._roots((root_dir / "queries/topology/segmentation_matrix.gql").read_text())
        device = self._roots((root_dir / query).read_text())
        fleet = {key: root for key, root in matrix.items() if key != "target"}
        assert list(device)[0] == "DcimDevice"
        assert list(device)[1:] == list(fleet)
        fleet["SecurityTag"] = fleet["SecurityTag"].replace(
            "...SecurityTagGroupPolicyFields\n", "...SecurityTagGroupPolicyFields\n" + self.PLACEMENT_SPREAD
        )
        assert {key: device[key] for key in fleet} == fleet

    @pytest.mark.parametrize(
        "query",
        [
            "queries/topology/segmentation_matrix.gql",
            "queries/config/leaf.gql",
            "queries/config/border_leaf.gql",
            "queries/config/border_spine.gql",
        ],
    )
    def test_every_query_reads_the_tags_through_the_one_fragment(self, root_dir: Path, query: str) -> None:
        text = (root_dir / query).read_text()
        assert "...SecurityTagGroupPolicyFields" in text
        assert "components" not in text
        fragments = (root_dir / "queries/fragments/group_policy.gql").read_text()
        assert fragments.count("fragment SecurityTagGroupPolicyFields on SecurityTag") == 1

    def test_only_the_device_queries_read_the_instance_placement(self, root_dir: Path) -> None:
        """The matrix artifact (and the check) must not pay for the NIC and cable walk of every instance."""
        for query in self.DEVICE_QUERIES:
            text = (root_dir / query).read_text()
            assert text.count("...SecurityTagPlacementFields") == 1
        for query in ("queries/topology/segmentation_matrix.gql", "queries/validation/reference_fabric.gql"):
            assert "SecurityTagPlacementFields" not in (root_dir / query).read_text()
        fragments = (root_dir / "queries/fragments/group_policy.gql").read_text()
        assert fragments.count("fragment SecurityTagPlacementFields on SecurityTag") == 1
        assert "instances" not in self._fragment(root_dir, "SecurityTagGroupPolicyFields"), (
            "the shared fragment stays as the matrix reads it"
        )

    def test_the_placement_fragment_selects_what_component_coverage_reads(self, root_dir: Path) -> None:
        """Per host kind: NICs by role with the cable's endpoints (id, device, role, tags, port-channel).

        Plain fields inside inline fragments only: a named fragment nested in an inline fragment resolves to nulls.
        """
        text = self._fragment(root_dir, "SecurityTagPlacementFields")
        assert "..." not in text.replace("... on", ""), "a named fragment is spread inside"
        assert text.count('interfaces(role__values: ["uplink", "lag"])') == 3
        for needle in (
            "... on DcimDevice",
            "... on DcimVirtualDevice",
            "... on ManagedControllerVirtual",
            "hosting_device",
            "endpoints",
            "device {",
            "role {",
            "interface_capabilities {",
            "lag {",
            "__typename",
        ):
            assert needle in text, needle

    def test_fragment_selects_memory_and_kind(self, root_dir: Path) -> None:
        """fabric_reason reads device_type.memory_gb; site_kind reads group_policy_kind."""
        text = (root_dir / "queries/fragments/group_policy.gql").read_text()
        assert "memory_gb { value }" in text
        assert "group_policy_kind { value }" in text


class TestReservedGroups:
    """The reserved group IDs of one kind come from its profile rows, never from code."""

    def test_the_nxos_row_names_its_reserved_groups(self) -> None:
        profiles = {"cisco_nxos": {**NXOS, "unclassified_group_id": 0, "unaware_group_id": 15}, "arista_eos": EOS}
        assert reserved_groups(profiles, "vxlan_gpo") == {"unclassified": 0, "unaware": 15}

    def test_a_kind_whose_rows_name_none_has_none(self) -> None:
        """EOS MSS-G classifies every source by prefix: no reserved group."""
        assert reserved_groups({"arista_eos": EOS}, "segment_security") == {"unclassified": None, "unaware": None}

    def test_rows_of_one_kind_that_disagree_fail_closed(self) -> None:
        """Two platforms of one kind naming different values: no single ID to render."""
        profiles = {
            "cisco_nxos": {**NXOS, "unaware_group_id": 15},
            "other": {**NXOS, "name": "other", "unaware_group_id": 14},
        }
        assert reserved_groups(profiles, "vxlan_gpo")["unaware"] is None

    def test_bootstrap_profile_names_the_nxos_reserved_groups_outside_the_pool(self, root_dir: Path) -> None:
        """Data, not code: 0 and 15 on the NX-OS row, both below the SGT-POOL band."""
        rows = yaml.safe_load((root_dir / "data/bootstrap/19b_group_policy_profile.yml").read_text())["spec"]["data"]
        nxos = next(row for row in rows if row["platform"] == "cisco_nxos")
        pool = yaml.safe_load((root_dir / "data/bootstrap/19_sgt_pool.yml").read_text())["spec"]["data"][0]
        assert (nxos["unclassified_group_id"], nxos["unaware_group_id"]) == (0, 15)
        assert all(not pool["start_range"] <= value <= pool["end_range"] for value in (0, 15))
        eos = next(row for row in rows if row["platform"] == "arista_eos")
        assert "unaware_group_id" not in eos and "unclassified_group_id" not in eos


class TestSites:
    """Which data center a VTEP belongs to, and where a segment is active."""

    def test_vteps_of_a_data_center_include_its_pods(self) -> None:
        data_centers = [{"id": "dc10", "devices": [_device("bl-1", role="border-leaf")]}, {"id": "dc11", "devices": []}]
        pods = [
            {"parent": {"id": "dc10"}, "devices": [_device("lf-1"), _device("sp-1", role="spine")]},
            {"parent": None, "devices": [_device("lf-x")]},
        ]
        vteps = vteps_by_data_center(data_centers, pods)
        assert {site: [device["name"] for device in found] for site, found in vteps.items()} == {
            "dc10": ["bl-1", "lf-1"],
            "dc11": [],
        }

    def test_vxlan_segment_is_active_in_its_deployments(self) -> None:
        segment = {"segment_deployments": [{"deployment": {"id": "dc10"}}, {"deployment": {"id": "fr"}}, {}]}
        assert segment_sites(segment) == {"dc10", "fr"}

    def test_vlan_segment_is_active_in_its_footprints_data_center(self) -> None:
        assert segment_sites({"customer_deployment": {"id": "fp-1", "parent": {"id": "dc11"}}}) == {"dc11"}

    def test_vlan_segment_of_another_footprint_kind_is_active_in_the_footprint(self) -> None:
        """An office or colocation footprint is its own site: never a data center."""
        assert segment_sites({"customer_deployment": {"id": "fp-office"}}) == {"fp-office"}

    def test_segment_active_nowhere(self) -> None:
        assert segment_sites({}) == set()
