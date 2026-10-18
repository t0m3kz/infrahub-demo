"""Uniqueness of customer footprints (schemas/extensions/topology/topology_customer.yml).

Infrahub enforces a generic's uniqueness constraint across every kind that
inherits it, so a constraint on TopologyCustomer without `parent` would allow
a customer only one footprint per environment in total — a DC and a
colocation footprint in the same environment (C005-P-DC10 + C005-P-FR in
data/demos/30_all) would then collide on upsert. Cloud and Office have no environment,
so their constraints are [name, parent] and [name].
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
    """TopologyCustomer must not constrain footprints across DC/Colocation/Cloud."""
    assert not _entries()["TopologyCustomer"].get("uniqueness_constraints")


@pytest.mark.parametrize("kind", ["TopologyCustomerDC", "TopologyCustomerColocation"])
def test_environment_scoped_footprints_are_unique_per_parent_and_environment(kind: str) -> None:
    """A DC or colocation footprint is unique on name, parent facility and environment."""
    assert _entries()[kind]["uniqueness_constraints"] == [["name__value", "parent", "environment__value"]]


def test_cloud_footprint_is_unique_per_parent_without_an_environment() -> None:
    """A cloud footprint has no environment (its CloudAccount carries it): name and parent only."""
    assert _entries()["TopologyCustomerCloud"]["uniqueness_constraints"] == [["name__value", "parent"]]


def test_office_is_unique_on_its_owner_derived_name() -> None:
    """One office per customer: the name embeds the owner's org_id, so the name alone is the constraint."""
    assert _entries()["TopologyCustomerOffice"]["uniqueness_constraints"] == [["name__value"]]
