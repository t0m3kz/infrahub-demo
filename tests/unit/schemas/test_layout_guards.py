"""The layout of generators/: building blocks never import a topology or a service, and every capability module has
the schema file it mirrors (AGENTS.md, "Where Code Goes")."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import generators

ROOT = Path(generators.__file__).parent
SCHEMA_CAPABILITIES = ROOT.parent / "schemas" / "extensions" / "capabilities"
CONSUMERS = ("dc", "colocation", "cloud", "office", "segments", "connectivity", "customers", "applications")
BLOCKS = ("capabilities", "dcim", "ipam")
FORBIDDEN = {
    "capabilities": CONSUMERS,
    "dcim": (*CONSUMERS, "capabilities"),
    "ipam": (*CONSUMERS, "capabilities"),
}


def _sources(root: Path) -> dict[str, str]:
    """Path (relative to ``root``, posix) -> source of every module under it."""
    return {path.relative_to(root).as_posix(): path.read_text() for path in sorted(root.rglob("*.py"))}


def _imported_modules(source: str) -> list[str]:
    """Every module a source imports, absolute (``from generators.x import y`` gives ``generators.x``)."""
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
            if node.module == "generators":
                modules.extend(f"generators.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def import_direction_violations(sources: dict[str, str]) -> list[str]:
    """``<path>: <module>`` of every import of a building block that points to a package it must not use."""
    found: list[str] = []
    for path, source in sources.items():
        block = path.split("/", 1)[0]
        if block not in FORBIDDEN or "/" not in path:
            continue
        found.extend(
            f"{path}: {module}"
            for module in _imported_modules(source)
            if any(module == f"generators.{pkg}" or module.startswith(f"generators.{pkg}.") for pkg in FORBIDDEN[block])
        )
    return sorted(found)


def capabilities_without_schema(sources: dict[str, str], schema_names: set[str]) -> list[str]:
    """Modules of capabilities/ (``__init__`` aside) with no ``schemas/extensions/capabilities/<name>.yml``."""
    return sorted(
        path
        for path in sources
        if path.startswith("capabilities/")
        and path.count("/") == 1
        and Path(path).stem not in {"__init__"} | schema_names
    )


class TestImportDirection:
    """Capabilities use dcim and ipam; none of the three uses a topology or a service."""

    @pytest.mark.parametrize("consumer", CONSUMERS)
    def test_a_capability_importing_a_consumer_fails(self, consumer: str) -> None:
        """Each topology and service package is refused to a capability, by ``from`` and by ``import``."""
        sources = {
            "capabilities/a.py": f"from generators.{consumer}.x import Y\n",
            "capabilities/b.py": f"import generators.{consumer}.x\n",
        }

        assert import_direction_violations(sources) == [
            f"capabilities/a.py: generators.{consumer}.x",
            f"capabilities/b.py: generators.{consumer}.x",
        ]

    def test_dcim_and_ipam_importing_capabilities_fail(self) -> None:
        """A dcim or ipam module never reaches up to a capability, also through ``from generators import``."""
        sources = {
            "dcim/a.py": "from generators.capabilities.bgp import X\n",
            "ipam/b.py": "from generators import capabilities\n",
        }

        assert import_direction_violations(sources) == [
            "dcim/a.py: generators.capabilities.bgp",
            "ipam/b.py: generators.capabilities",
        ]

    def test_allowed_directions_pass(self) -> None:
        """Capabilities use dcim, ipam, the plan and the base; consumers use capabilities freely."""
        sources = {
            "capabilities/a.py": "from generators.dcim.cabling_plan import X\nfrom generators.ipam.pools import Y\n"
            "from generators.plan import TopologyPlan\nfrom generators.base import BaseGenerator\n",
            "dcim/b.py": "from generators.ipam.pools import Y\nfrom generators.plan import TopologyPlan\n",
            "dc/c.py": "from generators.capabilities.bgp import X\n",
        }

        assert import_direction_violations(sources) == []

    def test_a_package_that_only_starts_with_a_consumer_name_passes(self) -> None:
        """``generators.dc_sizing`` is not ``generators.dc``."""
        assert import_direction_violations({"capabilities/a.py": "from generators.dc_sizing import x\n"}) == []

    def test_the_tree_follows_the_direction(self) -> None:
        """No module of capabilities, dcim or ipam imports across the direction."""
        assert import_direction_violations(_sources(ROOT)) == []


class TestCapabilitySchemas:
    """A capability module mirrors one schemas/extensions/capabilities file."""

    def test_a_module_without_a_schema_file_fails(self) -> None:
        """A module named after no schema file is reported; ``__init__`` is not."""
        sources = {"capabilities/__init__.py": "", "capabilities/bgp.py": "", "capabilities/ghost.py": ""}

        assert capabilities_without_schema(sources, {"bgp"}) == ["capabilities/ghost.py"]

    def test_modules_with_schema_files_pass(self) -> None:
        """Every module has its schema file."""
        assert capabilities_without_schema({"capabilities/bgp.py": "", "capabilities/ha.py": ""}, {"bgp", "ha"}) == []

    def test_every_capability_has_its_schema_file(self) -> None:
        """The real tree: each module of generators/capabilities has schemas/extensions/capabilities/<name>.yml."""
        names = {path.stem for path in SCHEMA_CAPABILITIES.glob("*.yml")}

        assert capabilities_without_schema(_sources(ROOT), names) == []
