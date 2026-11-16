"""Every generator inherits ``BaseGenerator`` and nothing else: shared behaviour is a function of its domain package."""

from __future__ import annotations

import ast
from pathlib import Path

GENERATORS = Path(__file__).resolve().parents[3] / "generators"


def test_the_mixin_package_is_gone(root_dir: Path) -> None:
    """generators/mixins does not come back."""
    assert not (root_dir / "generators" / "mixins").exists()


def test_no_generator_module_defines_or_imports_a_mixin() -> None:
    """No class whose name ends in ``Mixin``, and no import from ``generators.mixins``."""
    found: list[str] = []
    for path in sorted(GENERATORS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                found.append(f"{path.relative_to(GENERATORS)}: class {node.name}")
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("generators.mixins"):
                found.append(f"{path.relative_to(GENERATORS)}: import {node.module}")

    assert found == []
