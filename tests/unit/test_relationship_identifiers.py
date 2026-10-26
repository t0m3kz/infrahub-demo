"""A relationship declared on both ends pairs only when both ends have the same identifier.

Without an ``identifier`` Infrahub derives ``<kind_a>__<kind_z>`` from the two kinds, so two ends that both leave it
out pair, and so do two that spell the same one. An end with an explicit identifier and a peer end that leaves it out
(or spells another) are two separate relationships: a value written on one is never seen from the other.
ProxyPolicy.rules / ProxyPolicyRule.policy were like that: every rule pointed at its policy and the policy listed none.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

import yaml

_SCHEMAS = Path(__file__).parents[2] / "schemas"


def _declared() -> list[tuple[str, dict[str, Any]]]:
    """(declaring kind, relationship) of every node, generic and extension."""
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(glob.glob(str(_SCHEMAS / "**/*.y*ml"), recursive=True)):
        document = yaml.safe_load(Path(path).read_text()) or {}
        for section in ("generics", "nodes"):
            for node in document.get(section) or []:
                found.extend((node["namespace"] + node["name"], rel) for rel in node.get("relationships") or [])
        for extension in (document.get("extensions") or {}).get("nodes") or []:
            found.extend((extension["kind"], rel) for rel in extension.get("relationships") or [])
    return found


def test_an_explicit_identifier_is_not_left_unpaired_by_an_automatic_one_on_the_other_end() -> None:
    """Both ends declared, one with an identifier and one without: the explicit one has no partner."""
    declared = _declared()
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for kind, rel in declared:
        by_kind.setdefault(kind, []).append(rel)

    unpaired = sorted(
        f"{kind}.{rel['name']} ({rel['identifier']}) vs {rel['peer']}.{back['name']} (automatic)"
        for kind, rel in declared
        if rel.get("identifier")
        for back in by_kind.get(rel["peer"], [])
        if back["peer"] == kind and not back.get("identifier")
    )

    assert unpaired == []
