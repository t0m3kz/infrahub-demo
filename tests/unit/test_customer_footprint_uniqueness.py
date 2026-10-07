"""Uniqueness of customer footprints (schemas/extensions/topology/topology_customer.yml).

Infrahub enforces a generic's uniqueness constraint across every kind that
inherits it, so a constraint on TopologyCustomer without `parent` would allow
a customer only one footprint per environment in total — a DC and a
colocation footprint in the same environment (C005-P-DC10 + C005-P-FR in
data/demos/30_all) would then collide on upsert.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_SCHEMA = Path(__file__).parents[2] / "schemas" / "extensions" / "topology" / "topology_customer.yml"


def _entries() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(_SCHEMA.read_text())
    return {f"{e['namespace']}{e['name']}": e for section in ("generics", "nodes") for e in doc.get(section) or []}


def test_customer_generic_has_no_cross_kind_constraint() -> None:
    """TopologyCustomer must not constrain footprints across DC/Colocation/Cloud/Saas."""
    assert not _entries()["TopologyCustomer"].get("uniqueness_constraints")


@pytest.mark.parametrize(
    "kind",
    [
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerOffice",
        "TopologyCustomerCloud",
        "TopologyCustomerSaas",
    ],
)
def test_footprints_are_unique_per_parent_and_environment(kind: str) -> None:
    """Each concrete footprint is unique on name, parent facility and environment."""
    assert _entries()[kind]["uniqueness_constraints"] == [["name__value", "parent", "environment__value"]]
