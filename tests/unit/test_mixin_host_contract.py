"""The host contract of the generator mixins.

A mixin that needs a member of another base (``fail``, ``resource_lock``, ``create_cabling``, ...) aliases the
real one in a class-body ``if TYPE_CHECKING:`` block (``fail = CommonGenerator.fail``), so the type checker sees one
definition instead of a hand-copied signature, and nothing is defined at runtime. These tests read those aliases
from the source and prove the second half: no alias is defined at runtime (it would sit ahead of the real member
in the MRO and shadow it), and a host resolves each aliased name to its owner's member.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Any

import pytest

import generators
from generators.common import CommonGenerator

ROOT = Path(generators.__file__).parent


def _aliases() -> list[tuple[str, str, str, str]]:
    """(module, mixin, owner, name) of every ``name = Owner.name`` / ``name = staticmethod(Owner.name)`` alias."""
    found: list[tuple[str, str, str, str]] = []
    for path in sorted(ROOT.rglob("*.py")):
        module = ".".join(("generators", *path.relative_to(ROOT).with_suffix("").parts))
        for cls in (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.ClassDef)):
            for block in (n for n in cls.body if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"):
                for stmt in (s for s in block.body if isinstance(s, ast.Assign)):
                    value = stmt.value
                    if isinstance(value, ast.Call) and ast.unparse(value.func) == "staticmethod":
                        value = value.args[0]
                    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                        found.append((module, cls.name, value.value.id, value.attr))
    return found


def _import_all_generators() -> None:
    for info in pkgutil.walk_packages(generators.__path__, "generators."):
        importlib.import_module(info.name)


def _all_subclasses(base: type) -> set[type]:
    subs = set(base.__subclasses__())
    for sub in list(subs):
        subs |= _all_subclasses(sub)
    return subs


def _find(name: str) -> type:
    """The generators class called ``name`` (mixin names are unique in the package)."""
    _import_all_generators()
    matches = [
        obj
        for info in pkgutil.walk_packages(generators.__path__, "generators.")
        for obj in vars(importlib.import_module(info.name)).values()
        if inspect.isclass(obj) and obj.__name__ == name and obj.__module__.startswith("generators")
    ]
    assert matches, f"no class {name} in generators"
    return matches[0]


ALIASES = _aliases()


def test_the_aliases_are_found() -> None:
    """The scan sees the convention (a guard against it silently matching nothing)."""
    assert len(ALIASES) >= 40
    assert {owner for _, _, owner, _ in ALIASES} >= {"CommonGenerator", "PoolMixin", "CablingMixin"}


@pytest.mark.parametrize(("module", "mixin", "owner", "name"), ALIASES, ids=lambda v: str(v))
def test_an_alias_is_not_defined_at_runtime(module: str, mixin: str, owner: str, name: str) -> None:
    """The alias lives under TYPE_CHECKING: the mixin's own namespace has no such name, so the owner's wins in the MRO."""
    mixin_cls = getattr(importlib.import_module(module), mixin)

    assert name not in vars(mixin_cls)


def test_every_host_resolves_an_aliased_name_to_its_owner() -> None:
    """Whatever order a host lists its mixins in, ``host.name`` is the member of the class the alias names."""
    _import_all_generators()
    hosts = _all_subclasses(CommonGenerator)
    owners: dict[str, Any] = {}
    checked = 0
    for _module, mixin, owner, name in ALIASES:
        owner_cls = owners.setdefault(owner, _find(owner))
        mixin_cls = _find(mixin)
        for host in (h for h in hosts if issubclass(h, mixin_cls) and issubclass(h, owner_cls)):
            assert inspect.getattr_static(host, name) is inspect.getattr_static(owner_cls, name), (
                f"{host.__name__}.{name} is not {owner}.{name}"
            )
            checked += 1
    assert checked > 100
