"""Every draw from an IP pool goes through ``generators/ipam/allocation.py``: it reuses what the branch sees under an
identifier, which the server's own reuse does not (``.dev/infrahub-issue-pool-identifier-reuse.md``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ALLOCATORS = {"allocate_next_ip_prefix", "allocate_next_ip_address"}
ALLOWED = ROOT / "generators" / "ipam" / "allocation.py"


def _calls(path: Path) -> list[int]:
    """The lines of the calls to an allocator of the SDK client in ``path``."""
    tree = ast.parse(path.read_text())
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ALLOCATORS
    ]


def test_no_allocator_call_outside_the_allocation_module() -> None:
    """The SDK allocators are called in ``generators/ipam/allocation.py`` only."""
    offenders = [
        f"{path.relative_to(ROOT)}:{line}"
        for directory in ("generators", "checks", "transforms", "utils")
        for path in sorted((ROOT / directory).rglob("*.py"))
        if path != ALLOWED
        for line in _calls(path)
    ]
    assert not offenders, f"call generators.ipam.allocation instead: {offenders}"


def test_the_allocation_module_calls_both_allocators() -> None:
    """The scan sees the calls it guards (a rename of the SDK methods must not blind it)."""
    assert len(_calls(ALLOWED)) == len(ALLOCATORS)


@pytest.mark.parametrize("name", sorted(ALLOCATORS))
def test_the_sdk_still_has_the_allocator(name: str) -> None:
    """The names the scan looks for exist on the SDK client."""
    from infrahub_sdk import InfrahubClient

    assert hasattr(InfrahubClient, name)
