"""The routing tag model: schema attributes, pools, generator wiring and the customer group.

These read the YAML alone, so a broken link (a pool bound to a missing attribute, a customer outside the group the
created-trigger needs) fails here instead of as a failed or silent generator run in the integration suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from generators import protocols
from tests.integration.demo_data import (
    ROOT,
    action_generators,
    attribute,
    created_dispatches,
    declared,
    definition_targets,
    distinct,
    schema,
    trigger_rules,
)
from utils.limits import ROUTING_TAG_MAX, ROUTING_TAG_MIN

CUSTOMER_GROUP = "customers"
GENERATOR = "add_customer_routing_tag"
POOLS = {
    "Customer-Routing-Tag": ("OrganizationCustomer", "routing_tag"),
    "Segment-Routing-Tag": ("ManagedNetworkSegment", "routing_tag"),
}


def _infrahub_config(root_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((root_dir / ".infrahub.yml").read_text())


@pytest.mark.parametrize("kind", ["OrganizationCustomer", "ManagedNetworkSegment"])
class TestSchemaAttribute:
    """routing_tag is an optional, bounded Number on the customer and on the segment generic."""

    def test_is_an_optional_number_in_the_community_range(self, kind: str) -> None:
        definition = attribute(kind, "routing_tag")

        assert definition is not None
        assert definition["kind"] == "Number"
        assert definition["optional"] is True
        assert definition["parameters"] == {"min_value": ROUTING_TAG_MIN, "max_value": ROUTING_TAG_MAX}

    def test_is_not_unique_in_the_schema(self, kind: str) -> None:
        """`unique: true` on an optional attribute collides on the second unset value; the check enforces it."""
        definition = attribute(kind, "routing_tag")

        assert definition is not None
        assert not definition.get("unique")

    def test_description_fits_the_schema_limit(self, kind: str) -> None:
        definition = attribute(kind, "routing_tag")

        assert definition is not None
        assert 0 < len(definition["description"]) < 128

    def test_protocol_declares_it(self, kind: str) -> None:
        assert "routing_tag" in protocols.__dict__[kind].__annotations__


def test_a_segment_node_does_not_redefine_the_generic_attribute() -> None:
    """A node may not redefine an attribute its generic provides (no allow_override): only the generic declares it,
    and the node reaches it through inheritance."""
    assert "routing_tag" not in schema()["ManagedVxlanSegment"].attributes
    assert attribute("ManagedVxlanSegment", "routing_tag") is not None


class TestPools:
    """data/bootstrap/19c_routing_tag_pools.yml: one pool per kind, bound to the attribute, the full range."""

    @staticmethod
    def _pools(root_dir: Path) -> dict[str, dict[str, Any]]:
        document = yaml.safe_load((root_dir / "data/bootstrap/19c_routing_tag_pools.yml").read_text())
        assert document["spec"]["kind"] == "CoreNumberPool"
        return {pool["name"]: pool for pool in document["spec"]["data"]}

    def test_each_pool_is_bound_to_its_kinds_routing_tag(self, root_dir: Path) -> None:
        pools = self._pools(root_dir)

        assert set(pools) == set(POOLS)
        for name, (node, node_attribute) in POOLS.items():
            assert (pools[name]["node"], pools[name]["node_attribute"]) == (node, node_attribute)
            assert attribute(node, node_attribute) is not None

    def test_each_pool_covers_exactly_the_community_range(self, root_dir: Path) -> None:
        """Never narrower (a tag would run out) nor wider (a tag would not fit the community value)."""
        for pool in self._pools(root_dir).values():
            assert (pool["start_range"], pool["end_range"]) == (ROUTING_TAG_MIN, ROUTING_TAG_MAX)

    def test_pool_names_are_the_ones_the_helper_draws_from(self) -> None:
        from utils.limits import CUSTOMER_ROUTING_TAG_POOL, SEGMENT_ROUTING_TAG_POOL

        assert {CUSTOMER_ROUTING_TAG_POOL, SEGMENT_ROUTING_TAG_POOL} == set(POOLS)


class TestGeneratorWiring:
    """add_customer_routing_tag: definition, query, group and created-trigger."""

    def test_definition_targets_the_customers_group_with_a_registered_query(self, root_dir: Path) -> None:
        config = _infrahub_config(root_dir)
        definition = next(d for d in config["generator_definitions"] if d["name"] == GENERATOR)
        queries = {q["name"]: q["file_path"] for q in config["queries"]}

        assert definition["targets"] == CUSTOMER_GROUP
        assert definition["class_name"] == "CustomerRoutingTagGenerator"
        assert (root_dir / definition["file_path"]).is_file()
        assert (root_dir / queries[definition["query"]]).is_file()

    def test_customers_group_is_declared_in_bootstrap(self, root_dir: Path) -> None:
        groups = [
            entry["name"]
            for document in yaml.safe_load_all((root_dir / "data/bootstrap/00_groups.yml").read_text())
            for entry in document["spec"]["data"]
        ]

        assert CUSTOMER_GROUP in groups

    def test_a_created_customer_runs_the_generator_and_an_update_does_not(self) -> None:
        rules = [rule for rule in trigger_rules() if rule["node_kind"] == "OrganizationCustomer"]

        assert [(r["mutation_action"], action_generators()[r["action"]]) for r in rules] == [("created", GENERATOR)]
        assert not rules[0].get("matches")
        assert definition_targets()[GENERATOR] == CUSTOMER_GROUP

    def test_every_declared_customer_is_a_member_of_the_group(self) -> None:
        """The created-trigger raises for a non-member, so each declaration of a customer, anywhere in data/ or in
        the integration data, names the group (a re-declaration that only updates one included)."""
        outside = sorted(
            {
                str(obj.path.relative_to(ROOT))
                for obj in declared("OrganizationCustomer", ROOT / "data", ROOT / "tests" / "integration" / "data")
                if CUSTOMER_GROUP not in obj.groups()
            }
        )

        assert not outside, f"OrganizationCustomer objects not in '{CUSTOMER_GROUP}': {outside}"

    def test_the_30_all_load_must_dispatch_once_per_distinct_customer(self) -> None:
        """The dispatch floor test_59 asserts: one run per customer, a re-declaration counted once."""
        demo = ROOT / "data" / "demos" / "30_all"
        customers = distinct(declared("OrganizationCustomer", demo))

        assert len(customers) > 1
        assert created_dispatches(GENERATOR, demo) == len(customers)

    def test_30_all_customer_list_has_the_branch_created_customers(self) -> None:
        """test_59 holds these to a tag."""
        from tests.integration.all_demo_constants import ALL_DEMO_CUSTOMER_ORG_IDS

        assert {"C001", "C020", "INFRA"} <= set(ALL_DEMO_CUSTOMER_ORG_IDS)

    def test_check_is_registered_with_a_registered_query(self, root_dir: Path) -> None:
        config = _infrahub_config(root_dir)
        check = next(c for c in config["check_definitions"] if c["name"] == "validate_routing_tags")
        queries = {q["name"] for q in config["queries"]}

        assert check["class_name"] == "CheckRoutingTags"
        assert (root_dir / check["file_path"]).is_file()
        assert "routing_tags" in queries
