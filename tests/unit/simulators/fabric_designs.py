"""Fabric designs for unit tests, shaped as the generator queries deliver them.

The real designs are the rows of data/bootstrap/14b_fabric_templates.yml
(TopologyTemplatePod / TopologyTemplateDC); a DC design carries its
``pod_templates`` as a list of pod design dicts, like ``design { node { ...
pod_templates { edges { node { ... } } } } }`` after clean_data(). The pod
designs carry an ``id`` (the DC generator matches a pod's design by id).
"""

from __future__ import annotations

import copy
from typing import Any

from tests.integration.demo_data import ROOT, declared

_BOOTSTRAP = ROOT / "data" / "bootstrap"


def _rows(kind: str) -> dict[str, dict[str, Any]]:
    return {str(row.item["name"]): dict(row.item) for row in declared(kind, _BOOTSTRAP)}


_POD_DESIGNS = {name: {"id": f"pod-design-{name}", **row} for name, row in _rows("TopologyTemplatePod").items()}
_DC_DESIGNS = {
    name: {**row, "pod_templates": [_POD_DESIGNS[pod] for pod in row["pod_templates"]]}
    for name, row in _rows("TopologyTemplateDC").items()
}

POD_DESIGN_NAMES = tuple(sorted(_POD_DESIGNS))
DC_DESIGN_NAMES = tuple(sorted(_DC_DESIGNS))

# One real pod design per deployment type (the generators derive the type from it).
DESIGN_FOR_DEPLOYMENT_TYPE = {"middle_rack": "S_MIDDLE", "tor": "S_TOR", "mixed": "S_MIXED"}


def pod_design(name: str) -> dict[str, Any]:
    """The bootstrap pod design ``name`` (a fresh copy, safe to mutate)."""
    return copy.deepcopy(_POD_DESIGNS[name])


def dc_design(name: str) -> dict[str, Any]:
    """The bootstrap DC design ``name`` with its pod designs (a fresh copy, safe to mutate)."""
    return copy.deepcopy(_DC_DESIGNS[name])


def custom_pod_design(name: str = "TEST_POD", /, **caps: int) -> dict[str, Any]:
    """A throwaway pod design: one row, one network rack of two leafs, two spines, one border-leaf."""
    return {
        "id": f"pod-design-{name}",
        "name": name,
        "max_rows": 1,
        "max_compute_racks_per_row": 0,
        "max_network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 1,
        **caps,
    }


def custom_dc_design(
    name: str = "TEST_DC", /, *, pod_templates: list[dict[str, Any]] | None = None, **caps: int
) -> dict[str, Any]:
    """A throwaway DC design: two pods back-to-back, no DC tiers, hosting custom_pod_design()."""
    return {
        "name": name,
        "max_pods": 2,
        "max_super_spines_per_fabric": 0,
        "max_hyper_spines_per_fabric": 0,
        "max_border_leafs_per_fabric": 0,
        "pod_templates": pod_templates if pod_templates is not None else [custom_pod_design()],
        **caps,
    }
