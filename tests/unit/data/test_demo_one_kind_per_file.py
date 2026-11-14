"""Every demo object file declares one top-level kind (a mixed file is split into `<stem>_<NN>_<Kind>.yml`)."""

from __future__ import annotations

from pathlib import Path

import yaml


def _object_files(demos_dir: Path) -> list[Path]:
    """Every YAML file under data/demos."""
    return sorted(path for pattern in ("*.yml", "*.yaml") for path in demos_dir.rglob(pattern))


def _kinds(path: Path) -> list[str]:
    """The top-level `spec.kind` of every object document in the file, in order."""
    return [
        document["spec"]["kind"]
        for document in yaml.safe_load_all(path.read_text())
        if isinstance(document, dict) and isinstance(document.get("spec"), dict) and "kind" in document["spec"]
    ]


def test_demo_files_found(data_dir: Path) -> None:
    """The scan reads the object files (a wrong path would pass the real test vacuously)."""
    assert len(_object_files(data_dir / "demos")) > 100


def test_demo_object_file_declares_one_kind(data_dir: Path) -> None:
    """No demo object file mixes kinds; the message names each file and the kinds it holds."""
    demos_dir = data_dir / "demos"
    mixed = {
        str(path.relative_to(demos_dir)): kinds
        for path in _object_files(demos_dir)
        if len(set(kinds := _kinds(path))) > 1
    }
    assert not mixed, "Split into <stem>_<NN>_<Kind>.yml, one document per file:\n" + "\n".join(
        f"  - {path}: {kinds}" for path, kinds in sorted(mixed.items())
    )


def test_mixed_file_is_reported(tmp_path: Path) -> None:
    """The check sees two kinds in one file."""
    path = tmp_path / "mixed.yml"
    path.write_text(
        "---\napiVersion: infrahub.app/v1\nkind: Object\nspec:\n  kind: DcimCable\n  data: []\n"
        "---\napiVersion: infrahub.app/v1\nkind: Object\nspec:\n  kind: DcimPhysicalDevice\n  data: []\n"
    )
    assert _kinds(path) == ["DcimCable", "DcimPhysicalDevice"]
