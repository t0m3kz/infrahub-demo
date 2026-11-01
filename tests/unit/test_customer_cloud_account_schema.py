"""A cloud footprint has exactly one cloud account (schema shape and the 30_all data, read from the YAML).

``TopologyCustomerCloud.account`` is a mandatory relationship, unique (one footprint per account). The owner stays on the
footprint and must be the account's owner; the footprint's segments use virtual networks of that account. The two rules
are checks at run time; here the demo data is held to them without a server.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml
from infrahub_sdk.schema import SchemaRoot

from tests.integration.demo_data import ROOT, Declared, declared, name

_SCHEMA = ROOT / "schemas" / "extensions" / "topology" / "topology_customer.yml"
_ALL_DEMO = ROOT / "data" / "demos" / "30_all"


def _footprint_schema() -> dict:
    document = yaml.safe_load(_SCHEMA.read_text())
    return next(node for node in document["nodes"] if node["name"] == "CustomerCloud")


def _footprints() -> list[Declared]:
    return declared("TopologyCustomerCloud", _ALL_DEMO)


def _ref(value: object) -> str:
    """The HFID string of a reference (a bare string or a one-item list)."""
    return value[0] if isinstance(value, list) else str(value)


def test_the_schema_file_parses_as_a_schema_root() -> None:
    """The structure validates without a server."""
    SchemaRoot(**yaml.safe_load(_SCHEMA.read_text()))


def test_the_account_is_a_mandatory_single_edge_to_the_cloud_account() -> None:
    """One account, required; its identifier is its own (CloudAccount has other edges to footprint-like kinds)."""
    relationship = next(r for r in _footprint_schema()["relationships"] if r["name"] == "account")

    assert (relationship["peer"], relationship["cardinality"], relationship["optional"]) == (
        "CloudAccount",
        "one",
        False,
    )
    assert relationship["identifier"] == "customer_cloud__account"
    assert relationship["order_weight"] not in {100, 110, 150, 350, 1200, 3000}


def test_one_footprint_per_account_is_a_uniqueness_constraint_of_the_node() -> None:
    """The constraint is on the node (a relationship in a constraint must be mandatory, and it is)."""
    assert ["account"] in _footprint_schema()["uniqueness_constraints"]


def test_the_owner_is_still_the_customer_generic_edge() -> None:
    """owner stays on the generic; the node does not redeclare it."""
    assert "owner" not in {r["name"] for r in _footprint_schema()["relationships"]}


def test_every_cloud_footprint_of_30_all_names_exactly_one_account() -> None:
    """Each footprint declares one account (a string), and no two footprints declare the same one."""
    footprints = _footprints()

    assert footprints
    accounts = []
    for footprint in footprints:
        account = footprint.item.get("account")
        assert isinstance(account, str) and account, name(footprint)
        accounts.append(account)
    assert [a for a, n in Counter(accounts).items() if n > 1] == []


def _accounts() -> dict[str, Declared]:
    return {str(a.item["name"]): a for a in declared("CloudAccount", _ALL_DEMO)}


def test_every_footprint_account_is_declared_before_the_footprint_loads() -> None:
    """Object files load sorted by path: the account file must sort before the footprint file that references it."""
    accounts = _accounts()

    for footprint in _footprints():
        account = accounts[str(footprint.item["account"])]
        assert str(account.path) < str(footprint.path), (account.path, footprint.path)


def test_the_footprint_owner_is_the_account_owner() -> None:
    """The check pins owner == account.owner; the demo data already holds it."""
    accounts = _accounts()

    for footprint in _footprints():
        account = accounts[str(footprint.item["account"])]
        assert footprint.item["owner"] == account.item["owner"], name(footprint)


def test_the_footprint_segments_use_virtual_networks_of_its_account() -> None:
    """A cloud segment's virtual network belongs to the account of the footprint the segment is in."""
    networks = {str(n.item["name"]): str(n.item["account"]) for n in declared("CloudVirtualNetwork", _ALL_DEMO)}
    account_of = {name(f): str(f.item["account"]) for f in _footprints()}
    checked = 0

    for segment in declared("CloudNetworkSegment", _ALL_DEMO):
        footprint = segment.item.get("customer_deployment") or segment.item.get("customer_deployments")
        if not footprint:
            continue
        for ref in footprint if isinstance(footprint, list) else [footprint]:
            assert networks[_ref(segment.item["virtual_network"])] == account_of[_ref(ref)], segment.item
            checked += 1
    assert checked >= len(account_of)


def test_the_footprint_files_are_the_only_cloud_footprint_declarations() -> None:
    """No other data tree declares a cloud footprint without an account (the demos and the test data)."""
    for tree in (ROOT / "data", ROOT / "tests" / "integration" / "data"):
        for path in Path(tree).rglob("*.y*ml"):
            for document in yaml.safe_load_all(path.read_text()):
                spec = (document or {}).get("spec") or {}
                if spec.get("kind") == "TopologyCustomerCloud":
                    assert all("account" in item for item in spec["data"]), path


@pytest.mark.parametrize("kind", ["TopologyCustomerCloud"])
def test_the_protocol_class_carries_the_account(kind: str) -> None:
    """generators/protocols.py is hand-curated: the class has the account edge."""
    text = (ROOT / "generators" / "protocols.py").read_text()
    body = text.split(f"class {kind}(", 1)[1].split("\nclass ", 1)[0]

    assert "account: RelationshipAttribute[CloudAccount]" in body
