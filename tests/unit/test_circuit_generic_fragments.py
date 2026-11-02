"""A query never selects a circuit's ``capabilities`` through the TopologyCircuit generic.

``capabilities`` is declared on each circuit node, not on the generic, so a fragment ``on TopologyCircuit`` that selects it is
refused by the server when the repository is imported ("Cannot query field 'capabilities' on type 'TopologyCircuit'"). The unit
queries are not validated against a schema, so this test pins the rule on the text.
"""

from __future__ import annotations

import re
from pathlib import Path

_QUERIES = Path(__file__).parents[2] / "queries"
_GENERIC_FRAGMENT = re.compile(r"fragment\s+\w+\s+on\s+TopologyCircuit\s*\{")


def _body(text: str, start: int) -> str:
    """The text of the selection set that opens at ``start`` (the index of its ``{``)."""
    depth = 0
    for index in range(start, len(text)):
        depth += text[index] == "{"
        depth -= text[index] == "}"
        if depth == 0:
            return text[start : index + 1]
    return text[start:]


def test_no_fragment_on_the_circuit_generic_selects_capabilities() -> None:
    """The ends are read through TopologyCarrierCircuit and TopologySessionCircuit, one fragment each."""
    offenders = []
    for path in sorted(_QUERIES.rglob("*.gql")):
        text = path.read_text()
        for match in _GENERIC_FRAGMENT.finditer(text):
            if re.search(r"\bcapabilities\b", _body(text, match.end() - 1)):
                offenders.append(str(path.relative_to(_QUERIES)))

    assert not offenders, offenders
