"""The demo data gives every registered cloud block and office LAN a customer owner.

The colocation edge announces to and accepts from an external BGP peer only prefixes whose owner is an
OrganizationCustomer, and the owner belongs to the object that registers the prefix (checks/routing_invariants.py,
rule 7): a VPC block's owner is its account's owner, an office LAN's (and its hosted segments') the office's owner.
A prefix registered by a provider-owned account or office is silently never accepted or announced, so the data must
not declare one. This reads the YAML only (no Infrahub).
"""

from __future__ import annotations

from typing import Any

from tests.integration.demo_data import BOOTSTRAP, ROOT, Declared, declared, name

_DEMOS = ROOT / "data" / "demos"
_ALL = _DEMOS / "30_all"


def _org_id(reference: Any) -> str | None:
    """The org_id a relationship value names: a one-value HFID given as a string or a one-item list."""
    if isinstance(reference, list) and len(reference) == 1:
        reference = reference[0]
    return reference if isinstance(reference, str) else None


def _customers() -> set[str]:
    """The org_id of every OrganizationCustomer the bootstrap data and the demos declare."""
    return {str(obj.item["org_id"]) for obj in declared("OrganizationCustomer", BOOTSTRAP, _DEMOS)}


def _accounts_with_blocks(*paths: Any) -> list[tuple[Declared, str]]:
    """(account, VPC name) of every CloudVirtualNetwork under paths that registers at least one cidr_blocks entry."""
    accounts = {name(obj): obj for obj in declared("CloudAccount", BOOTSTRAP, _DEMOS)}
    found = []
    for vpc in declared("CloudVirtualNetwork", *paths):
        if not vpc.item.get("cidr_blocks"):
            continue
        reference = vpc.item.get("account")
        reference = reference[0] if isinstance(reference, list) and len(reference) == 1 else reference
        assert isinstance(reference, str) and reference in accounts, f"{name(vpc)}: account {reference!r} not declared"
        found.append((accounts[reference], name(vpc)))
    return found


def test_the_customer_inventory_is_not_empty() -> None:
    """Guard the other tests against passing on an empty customer set."""
    assert {"C001", "C003"} <= _customers()


def test_every_vpc_with_blocks_belongs_to_a_customer_owned_account() -> None:
    """The account of every VPC that registers cidr_blocks names an owner that is a declared customer org_id."""
    customers = _customers()
    registered = _accounts_with_blocks(_DEMOS)
    assert registered, "no VPC with cidr_blocks in data/demos: the rule has nothing to guard"
    offenders = [
        f"VPC '{vpc}' (account '{name(account)}', owner {account.item.get('owner')!r} in {account.path.relative_to(ROOT)})"
        for account, vpc in registered
        if _org_id(account.item.get("owner")) not in customers
    ]
    assert not offenders, f"VPC blocks never accepted or announced at the border: {offenders}"


def test_every_office_belongs_to_a_customer() -> None:
    """The owner of every TopologyCustomerOffice is a declared customer org_id (an office registers LANs or hosts segments)."""
    customers = _customers()
    offices = declared("TopologyCustomerOffice", _DEMOS)
    assert offices, "no TopologyCustomerOffice in data/demos: the rule has nothing to guard"
    offenders = [
        f"{obj.path.relative_to(ROOT)}: owner {obj.item.get('owner')!r}"
        for obj in offices
        if _org_id(obj.item.get("owner")) not in customers
    ]
    assert not offenders, f"office prefixes never accepted or announced at the border: {offenders}"


def test_the_30_all_demo_declares_both_registrants() -> None:
    """30_all has VPCs with blocks and offices, so the two tests above bite on the demo the border serves."""
    assert _accounts_with_blocks(_ALL)
    assert declared("TopologyCustomerOffice", _ALL)
