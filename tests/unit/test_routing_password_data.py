"""Every RoutingPassword value in the demo data is visibly a demo secret.

A BGP key that looks real invites someone to reuse it; the "demo-" prefix makes
a leaked artifact or a copied snippet obviously harmless (and greppable).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DATA = Path(__file__).parents[2] / "data"
_PREFIX = "demo-"


def _routing_passwords() -> list[tuple[Path, str, Any]]:
    """(file, password name, password value) for every RoutingPassword object under data/."""
    found: list[tuple[Path, str, Any]] = []
    for path in sorted(_DATA.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            spec = doc.get("spec") if isinstance(doc, dict) else None
            if not isinstance(spec, dict) or spec.get("kind") != "RoutingPassword":
                continue
            found.extend((path, entry.get("name", "?"), entry.get("password")) for entry in spec.get("data") or [])
    return found


def test_routing_passwords_are_found() -> None:
    """The walk sees the RoutingPassword objects (guards the test against silently checking nothing)."""
    assert len(_routing_passwords()) >= 3


def test_every_routing_password_value_starts_with_demo() -> None:
    """Report every offender at once: file, password object name and value."""
    offenders = [
        f"{path.relative_to(_DATA.parent)}: {name} = {value!r}"
        for path, name, value in _routing_passwords()
        if not str(value).startswith(_PREFIX)
    ]

    assert not offenders, f"RoutingPassword values must start with {_PREFIX!r}:\n" + "\n".join(offenders)
