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

from utils.bootstrap_order import bootstrap_load_tiers

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
