"""Unit tests for bootstrap object-file dependency ordering.

``bootstrap_load_tiers`` is what stops ``infrahubctl object load data/bootstrap/``
from racing an HFID reference against the node it points at. Two properties
matter, and they pull in opposite directions:

* **Totality** — every object file lands in exactly one tier. A dropped file
  means missing objects, which surfaces much later as an unrelated failure.
* **Ordering** — a file that references another must load in a later tier.

The ordering assertions run against the real shipped data, not a synthetic
directory, because the thing that broke was the real data's layering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.bootstrap_order import (
    NESTED_TIER_CONCURRENCY,
    bootstrap_load_tiers,
    object_file_nests_children,
    tier_concurrency,
)

# Referencing file -> the file whose objects it looks up by HFID. Derived from
# the `platform:` / `manufacturer:` / `device_type:` / `software_image:` /
# `owner:` fields in data/bootstrap. Each pair must land in a strictly later
# tier than its dependency.
BOOTSTRAP_DEPENDENCIES = [
    ("05b_software_images.yml", "05_platforms.yml"),
    ("06_device_types.yml", "04_manufacturers.yml"),
    ("10_physical_devices_templates_arista_eos.yaml", "05_platforms.yml"),
    ("10_physical_devices_templates_arista_eos.yaml", "05b_software_images.yml"),
    ("10_physical_devices_templates_arista_eos.yaml", "06_device_types.yml"),
    ("10_physical_devices_templates_arista_eos.yaml", "02_providers.yml"),
    ("09_virtual_device_templates_junos.yaml", "05_platforms.yml"),
    ("09_virtual_device_templates_junos.yaml", "06_device_types.yml"),
    ("13_asns.yml", "02_providers.yml"),
]


@pytest.fixture(scope="module")
def bootstrap_dir(root_dir: Path) -> Path:
    return root_dir / "data" / "bootstrap"


@pytest.fixture(scope="module")
def bootstrap_tiers(bootstrap_dir: Path) -> list[list[Path]]:
    tiers = bootstrap_load_tiers(bootstrap_dir)
    assert tiers, "no tiers produced — the helper is not reading data/bootstrap"
    return tiers


def _tier_index(tiers: list[list[Path]], filename: str) -> int:
    for index, tier in enumerate(tiers):
        if any(path.name == filename for path in tier):
            return index
    raise AssertionError(f"{filename} is in no tier")


class TestTotality:
    def test_every_object_file_appears_exactly_once(
        self, bootstrap_dir: Path, bootstrap_tiers: list[list[Path]]
    ) -> None:
        """Dropping a file here would silently omit its objects."""
        on_disk = {path.name for path in bootstrap_dir.iterdir() if path.suffix in (".yml", ".yaml")}
        flattened = [path.name for tier in bootstrap_tiers for path in tier]

        assert sorted(flattened) == sorted(on_disk)
        assert len(flattened) == len(set(flattened)), "a file was placed in more than one tier"

    def test_tiers_are_never_empty(self, bootstrap_tiers: list[list[Path]]) -> None:
        assert all(bootstrap_tiers), "an empty tier would emit a load command with no paths"


class TestOrdering:
    @pytest.mark.parametrize(("referencing", "dependency"), BOOTSTRAP_DEPENDENCIES)
    def test_dependencies_load_in_an_earlier_tier(
        self, bootstrap_tiers: list[list[Path]], referencing: str, dependency: str
    ) -> None:
        """The regression: software images resolved `platform:` against platforms
        being created in the same concurrent run."""
        referencing_tier = _tier_index(bootstrap_tiers, referencing)
        dependency_tier = _tier_index(bootstrap_tiers, dependency)

        assert dependency_tier < referencing_tier, (
            f"{referencing} (tier {referencing_tier}) references {dependency} "
            f"(tier {dependency_tier}) — it must load strictly later"
        )

    def test_files_sharing_a_prefix_share_a_tier(self, bootstrap_tiers: list[list[Path]]) -> None:
        """Otherwise the 19 ``10_*`` template files would serialise for no
        reason — they reference lower tiers, never one another."""
        tier = bootstrap_tiers[_tier_index(bootstrap_tiers, "10_physical_devices_templates_arista_eos.yaml")]
        assert len(tier) > 1
        assert all(path.name.startswith("10_") for path in tier)

    def test_a_letter_suffix_inserts_between_numeric_tiers(self, bootstrap_tiers: list[list[Path]]) -> None:
        """``05b`` must sit strictly between ``05`` and ``06``."""
        assert (
            _tier_index(bootstrap_tiers, "05_platforms.yml")
            < _tier_index(bootstrap_tiers, "05b_software_images.yml")
            < _tier_index(bootstrap_tiers, "06_device_types.yml")
        )


class TestSorting:
    def test_single_digit_prefix_is_not_sorted_as_text(self, tmp_path: Path) -> None:
        """As text ``"5"`` sorts after ``"10"``, which would drop a single-digit
        tier to the end of the load and undo the ordering it asked for."""
        for name in ("5_five.yml", "10_ten.yml", "2_two.yml"):
            (tmp_path / name).touch()

        tiers = bootstrap_load_tiers(tmp_path)

        assert [path.name for tier in tiers for path in tier] == ["2_two.yml", "5_five.yml", "10_ten.yml"]

    def test_unprefixed_files_load_last_but_are_not_dropped(self, tmp_path: Path) -> None:
        """Nothing can depend on them via a convention they do not follow, but
        omitting them would lose data silently."""
        for name in ("01_first.yml", "zz_extra.yml"):
            (tmp_path / name).touch()

        tiers = bootstrap_load_tiers(tmp_path)

        assert [path.name for tier in tiers for path in tier] == ["01_first.yml", "zz_extra.yml"]

    def test_non_object_files_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "01_data.yml").touch()
        (tmp_path / "README.md").touch()
        (tmp_path / "notes.txt").touch()

        assert [path.name for tier in bootstrap_load_tiers(tmp_path) for path in tier] == ["01_data.yml"]

    def test_subdirectories_are_ignored(self, tmp_path: Path) -> None:
        """A nested dir would be passed to the CLI as a path, pulling in files
        from a tier it was never assigned to."""
        (tmp_path / "01_data.yml").touch()
        (tmp_path / "02_nested.yml").mkdir()

        assert [path.name for tier in bootstrap_load_tiers(tmp_path) for path in tier] == ["01_data.yml"]

    def test_empty_directory_yields_no_tiers(self, tmp_path: Path) -> None:
        assert bootstrap_load_tiers(tmp_path) == []


def _write_object_file(path: Path, data: str) -> Path:
    path.write_text(f"---\napiVersion: infrahub.app/v1\nkind: Object\nspec:\n  kind: DcimDevice\n  data:\n{data}")
    return path


class TestNestedDetection:
    """Which tiers must serialise. Detected from structure, so a new nested file
    picks up the safer concurrency without anyone maintaining a list."""

    def test_dict_form_nesting_is_detected(self, tmp_path: Path) -> None:
        """``interfaces: {kind:, data: [...]}`` — how device templates declare
        the interfaces whose parent lookup was failing."""
        path = _write_object_file(
            tmp_path / "01_x.yml",
            "    - name: leaf-01\n      interfaces:\n        kind: DcimInterface\n        data:\n          - name: et1\n",
        )
        assert object_file_nests_children(path) is True

    def test_list_form_nesting_is_detected(self, tmp_path: Path) -> None:
        """``devices: [{kind:, data: {...}}]`` — the other nested shape the
        object file format allows."""
        path = _write_object_file(
            tmp_path / "01_x.yml",
            "    - name: dc1\n      devices:\n        - kind: DcimDevice\n          data:\n            name: spine-1\n",
        )
        assert object_file_nests_children(path) is True

    def test_flat_file_is_not_nested(self, tmp_path: Path) -> None:
        """HFID string references are not nesting — the peer already exists in
        an earlier tier, so there is no parent-visibility window to protect."""
        path = _write_object_file(
            tmp_path / "01_x.yml",
            '    - name: leaf-01\n      platform: arista_eos\n      tags:\n        - "blue"\n        - "prod"\n',
        )
        assert object_file_nests_children(path) is False

    def test_unparseable_file_is_assumed_nested(self, tmp_path: Path) -> None:
        """Guessing 'flat' on a file we cannot read would hand it the fast path
        on no evidence; the safe answer costs only speed."""
        path = tmp_path / "01_broken.yml"
        path.write_text("spec: {data: [unclosed\n")
        assert object_file_nests_children(path) is True

    def test_real_template_files_are_detected_as_nested(self, bootstrap_dir: Path) -> None:
        """The regression: these are the files whose nested interfaces failed
        with 'Unable to find the node <uuid> / TemplateDcimDevice'."""
        for name in (
            "09_virtual_device_templates_netscaler.yaml",
            "10_physical_devices_templates_arista_eos.yaml",
        ):
            assert object_file_nests_children(bootstrap_dir / name) is True, name

    def test_real_flat_files_are_not_serialised(self, bootstrap_dir: Path) -> None:
        for name in ("05_platforms.yml", "05b_software_images.yml", "04_manufacturers.yml"):
            assert object_file_nests_children(bootstrap_dir / name) is False, name


class TestTierConcurrency:
    def test_a_nested_file_serialises_its_whole_tier(self, tmp_path: Path) -> None:
        """A tier loads as one invocation, so one nested file sets the pace for
        the files sharing its prefix."""
        flat = _write_object_file(tmp_path / "10_flat.yml", "    - name: a\n")
        nested = _write_object_file(
            tmp_path / "10_nested.yml",
            "    - name: b\n      interfaces:\n        kind: DcimInterface\n        data:\n          - name: et1\n",
        )
        assert tier_concurrency([flat, nested], 10) == NESTED_TIER_CONCURRENCY

    def test_a_flat_tier_keeps_the_default(self, tmp_path: Path) -> None:
        flat = _write_object_file(tmp_path / "05_flat.yml", "    - name: a\n")
        assert tier_concurrency([flat], 10) == 10

    def test_the_real_template_tiers_are_serialised(self, bootstrap_tiers: list[list[Path]]) -> None:
        for filename in (
            "09_virtual_device_templates_netscaler.yaml",
            "10_physical_devices_templates_arista_eos.yaml",
        ):
            tier = bootstrap_tiers[_tier_index(bootstrap_tiers, filename)]
            assert tier_concurrency(tier, 10) == NESTED_TIER_CONCURRENCY, filename
