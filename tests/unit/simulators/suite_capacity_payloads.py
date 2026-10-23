"""Check payloads for the suite capacity rules, built from the object files instead of typed by hand.

``build_payloads`` reads the YAML a demo loads and shapes it like the raw GraphQL response of
``queries/validation/suite_capacity.gql``: one ``LocationRack`` root, every rack with its position, its suite (the
capacity: ``rows`` and ``racks_per_row``) and the pod hosting it with the pod's design envelope. Racks are data, never
a generator's output (``grep`` of ``generators/`` finds no rack creation), so the objects files are the complete
rack inventory and the check can run over them unchanged.

A rack is resolved the way the loader would: its suite is the ``parent`` shortname or the suite it is nested under,
its pod the ``pod`` name (a pod is ``<DC>-<DC index>-POD-<index>``) or the pod it is nested under, its design the
``TopologyTemplatePod`` of ``data/bootstrap``. A rack hosted by a colocation zone has no design. Anything the data
names that cannot be resolved raises ``KeyError``: a payload that silently dropped a rack would make "zero findings"
meaningless.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tests.integration.demo_data import BOOTSTRAP, ROOT, Declared, declared, distinct, name

DEMOS = ROOT / "data" / "demos"

RACK = "LocationRack"
SUITE = "LocationSuite"
POD = "TopologyPod"
ZONE = "TopologyColocationZone"
DATA_CENTER = "TopologyDataCenter"
DESIGN_FIELDS = ("max_rows", "max_compute_racks_per_row", "max_network_racks_per_row")


def _edges(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _value(item: Any) -> dict[str, Any]:
    return {"value": item}


def _enclosing(obj: Declared, kind: str) -> Declared | None:
    """The nearest object of ``kind`` ``obj`` is nested under."""
    parent = obj.enclosing
    while parent is not None and parent.kind != kind:
        parent = parent.enclosing
    return parent


def suite_ref(rack: Declared) -> str | None:
    """The shortname of the suite the rack sits in: its ``parent`` or the suite it is nested under."""
    if rack.item.get("parent"):
        return str(rack.item["parent"])
    suite = _enclosing(rack, SUITE)
    return str(suite.item["shortname"]) if suite else None


def design_node(design: Declared) -> dict[str, Any]:
    """A pod design as the query selects it."""
    node: dict[str, Any] = {"id": f"id-{name(design)}", "name": _value(name(design))}
    node.update({field: _value(design.item[field]) for field in DESIGN_FIELDS})
    return node


class _Inventory:
    """Suites, pods, zones and designs of the object files under the roots, each by the name a rack refers to."""

    def __init__(self, roots: tuple[Path, ...]) -> None:
        designs = {name(d): d for d in distinct(declared("TopologyTemplatePod", BOOTSTRAP))}
        self.suites = {str(s.item["shortname"]): s for s in distinct(declared(SUITE, *roots))}
        self.data_centers = {str(dc.item["name"]): dc for dc in distinct(declared(DATA_CENTER, *roots))}
        self.pods: dict[str, dict[str, Any]] = {}
        for pod in distinct(declared(POD, *roots)):
            pod_name = self.pod_name(pod)
            self.pods[pod_name] = {
                "id": f"id-{pod_name}",
                "name": _value(pod_name),
                "design": {"node": design_node(designs[str(pod.item["design"])])},
            }
        for zone in distinct(declared(ZONE, *roots)):
            zone_name = name(zone)
            self.pods[zone_name] = {"id": f"id-{zone_name}", "name": _value(zone_name)}

    def pod_name(self, pod: Declared) -> str:
        """``<DC>-<DC index>-POD-<index>``: the DC is the pod's ``parent`` or the DC it is nested under."""
        data_center = _enclosing(pod, DATA_CENTER)
        dc_name = str(pod.item["parent"]) if pod.item.get("parent") else str((data_center or pod).item["name"])
        return f"{dc_name.upper()}-{self.data_centers[dc_name].item['index']}-POD-{pod.item['index']}"

    def suite(self, rack: Declared) -> dict[str, Any]:
        """The suite node of a rack: its capacity as the query selects it."""
        shortname = suite_ref(rack)
        if shortname is None:
            raise KeyError(f"{rack.item['shortname']}: no suite (neither a parent nor nested under one)")
        suite = self.suites[shortname]
        return {
            "id": f"id-{shortname}",
            "name": _value(shortname),
            "rows": _value(suite.item["rows"]),
            "racks_per_row": _value(suite.item["racks_per_row"]),
        }

    def hosting(self, rack: Declared) -> dict[str, Any] | None:
        """The pod (or zone) hosting a rack: its ``pod`` name or the pod it is nested under; None for neither."""
        enclosing_pod = _enclosing(rack, POD)
        if rack.item.get("pod"):
            return self.pods[str(rack.item["pod"])]
        return self.pods[self.pod_name(enclosing_pod)] if enclosing_pod else None


def build_payloads(*roots: Path) -> dict[str, Any]:
    """The response of ``suite_capacity.gql`` for every rack declared under the demo roots."""
    inventory = _Inventory(roots)
    nodes = []
    for rack in distinct(declared(RACK, *roots)):
        shortname = str(rack.item["shortname"])
        hosting = inventory.hosting(rack)
        nodes.append(
            {
                "id": f"id-{shortname}",
                "name": _value(shortname),
                "row_index": _value(rack.item["row_index"]),
                "index": _value(rack.item["index"]),
                "parent": {"node": inventory.suite(rack)},
                "pod": {"node": hosting},
            }
        )
    return {RACK: _edges(nodes)}
