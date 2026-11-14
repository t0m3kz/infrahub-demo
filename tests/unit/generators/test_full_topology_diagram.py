"""The topology diagram of data/demos/00_full_topology/README.md is the circuits of the data."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path

import yaml

_ROOT = Path(__file__).parents[3]
_DATA = _ROOT / "data" / "demos" / "00_full_topology"
_README = _DATA / "README.md"
_START, _END = "<!-- topology-diagram:start -->", "<!-- topology-diagram:end -->"
_CARRIER, _SESSION = "TopologyCarrierCircuit", "TopologySessionCircuit"

# Display name and group of each location the circuits of the sample list.
_GROUPS: dict[str, tuple[str, list[str]]] = {
    "SITES": (
        "Our sites",
        ["DC-KTW1", "DC-WAW1", "DC-GDN1", "PAR-COLO1", "FRA-COLO1", "AMS-COLO1", "C900-OFFICE", "C901-OFFICE"],
    ),
    "NET": ("Internet", ["INTERNET"]),
    "CLOUD": ("Cloud and SaaS", ["eu-central-1", "westeurope", "europe-west3", "WEBEX-FRANKFURT", "ZSCALER-MUNICH I"]),
}
_LABELS = {
    "eu-central-1": "AWS eu-central-1",
    "westeurope": "Azure westeurope",
    "europe-west3": "GCP europe-west3",
    "WEBEX-FRANKFURT": "Webex Frankfurt",
    "ZSCALER-MUNICH I": "Zscaler Munich",
}


def _node_id(location: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", location)


@cache
def _circuits() -> list[tuple[str, str, list[str]]]:
    """(kind, circuit_type, locations) of every circuit of the sample."""
    found = []
    for path in sorted(_DATA.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            kind = ((doc or {}).get("spec") or {}).get("kind")
            if kind in (_CARRIER, _SESSION):
                found += [(kind, item["circuit_type"], list(item["locations"])) for item in doc["spec"]["data"]]
    return found


def topology_diagram() -> str:
    """The Mermaid block: the locations grouped, an edge per pair with the circuit types between them."""
    order = [location for _, members in _GROUPS.values() for location in members]
    edges: dict[tuple[str, str], Counter[tuple[str, str]]] = defaultdict(Counter)
    for kind, circuit_type, locations in _circuits():
        if len(locations) != 2:
            continue
        first, second = sorted(locations, key=order.index)
        pair = (first, second)
        edges[pair][(kind, circuit_type)] += 1
    lines = ["```mermaid", "flowchart LR"]
    for group, (title, members) in _GROUPS.items():
        lines.append(f'  subgraph {group}["{title}"]')
        lines += [f'    {_node_id(m)}["{_LABELS.get(m, m)}"]' for m in members]
        lines.append("  end")
    for (a, b), counts in sorted(edges.items(), key=lambda item: (order.index(item[0][0]), order.index(item[0][1]))):
        label = ", ".join(
            f"{n}x {circuit_type.replace('_', ' ')}"
            for (kind, circuit_type), n in sorted(counts.items(), key=lambda item: (item[0][0], item[0][1]))
        )
        line = "---" if any(kind == _CARRIER for kind, _ in counts) else "-.-"
        lines.append(f'  {_node_id(a)} {line}|"{label}"| {_node_id(b)}')
    lines.append("```")
    return "\n".join(lines)


def test_every_circuit_location_has_a_place_in_the_diagram() -> None:
    """A circuit between two locations the diagram does not group would be left out without a word."""
    known = {location for _, members in _GROUPS.values() for location in members}
    unknown = {loc for _, _, locations in _circuits() for loc in locations} - known
    assert not unknown, f"add these locations to _GROUPS: {sorted(unknown)}"


def test_readme_diagram_is_the_circuits_of_the_data() -> None:
    """The block between the markers of the README is the diagram drawn from the data."""
    text = _README.read_text()
    assert _START in text and _END in text, f"the README has no {_START} ... {_END} block"
    current = text.split(_START, 1)[1].split(_END, 1)[0].strip()
    assert current == topology_diagram(), (
        "the topology diagram of the README is stale: run `uv run python -m tests.unit.generators.test_full_topology_diagram` "
        "and paste its output between the markers"
    )


if __name__ == "__main__":
    print(topology_diagram())
