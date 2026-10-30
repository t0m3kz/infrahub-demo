"""Offline data tests: every destination of C001's egress policy in 30_all is reachable.

The rules of ``seg-c001-nordix-prod-p-egress`` (07_zone_policies/05_security_policy.yml) aim at three far-side
destinations. The border policy accepts and announces only prefixes REGISTERED by an owned object, so a destination
is reachable only when it is one of:

- a VPC block of an account owned by the source segment's owner, whose attachment propagates into the route table the
  hybrid (transit VIF) attachment is associated with (what ``vif_registered_blocks`` reads off the AWS handoff session),
- a prefix in the ``registered_prefixes`` of a tunnel circuit owned by that owner (the Acme extranet), or
- the internet zone, ``destination_zone: INTERNET-ZONE`` (a SaaS PoP is public space and registers nothing).
"""

from __future__ import annotations

from typing import Any

import pytest

from tasks import ALL_DEMO_DATA
from tests.integration.demo_data import BOOTSTRAP, ROOT, Declared, declared, declared_session, distinct, name
from utils.exchange_transit import INTERNET_ZONE

_ALL = ROOT / ALL_DEMO_DATA
_POLICY = "seg-c001-nordix-prod-p-egress"
_SOURCE_SEGMENT = "c001-nordix-prod-p"
_OWNER = "C001"
_VPC = "nordix-vpc-prod"
_VPC_BLOCK = ["10.10.0.0/16", "PROD"]
_ACME_RANGE = ["198.18.0.0/24", "PROD"]


def _rules(policy_name: str = _POLICY) -> list[dict[str, Any]]:
    """The hand-written rules of the policy ``policy_name``."""
    (policy,) = [p for p in declared("SecurityPolicy", _ALL) if name(p) == policy_name]
    return policy.item["rules"]["data"]


def _by_name(kind: str) -> dict[str, Declared]:
    """The 30_all declarations of ``kind`` by name (a re-declaration counts once)."""
    return {name(obj): obj for obj in distinct(declared(kind, _ALL))}


def _owner_of_segment(segment: str) -> str:
    """The owner (org_id) of the declared segment ``segment``."""
    (found,) = [s for s in declared("ManagedVxlanSegment", _ALL) if name(s) == segment]
    return str(found.item.get("owner") or (found.enclosing.item["org_id"] if found.enclosing else None))


def _hub_registered_blocks(owner: str) -> set[tuple[str, str]]:
    """(prefix, namespace) of the VPC blocks ``owner`` registers behind the transit VIF session.

    The chain of transforms/helpers/bgp_external.py ``vif_registered_blocks``: every VPC attachment propagating into the
    table the hybrid attachment is associated with, its virtual network's blocks, owned by the account's owner.
    """
    attachments = list(_by_name("CloudTransitGatewayAttachment").values())
    (hybrid,) = [a for a in attachments if a.item["attachment_type"] == "hybrid"]
    accounts = _by_name("CloudAccount")
    networks = _by_name("CloudVirtualNetwork")
    registered: set[tuple[str, str]] = set()
    for attachment in attachments:
        item = attachment.item
        if (
            item["attachment_type"] != "vpc"
            or hybrid.item["associated_route_table"] not in item["propagates_to_route_tables"]
        ):
            continue
        network = networks[item["virtual_network"]].item
        if accounts[network["account"]].item["owner"] == owner:
            registered.update((block[0], block[1]) for block in network["cidr_blocks"])
    return registered


def _tunnel_registered_prefixes(owner: str) -> set[tuple[str, str]]:
    """(prefix, namespace) of the ``registered_prefixes`` of every tunnel circuit owned by ``owner``."""
    return {
        (prefix[0], prefix[1])
        for tunnel in distinct(declared_session("tunnel", _ALL))
        if tunnel.item.get("owner") == owner
        for prefix in tunnel.item.get("registered_prefixes") or []
    }


def _declared_prefixes() -> set[tuple[str, str]]:
    """(prefix, namespace) of every IpamPrefix object the 30_all files declare."""
    return {(p.item["prefix"], p.item.get("ip_namespace", "default")) for p in declared("IpamPrefix", _ALL)}


# ---------------------------------------------------------------------------
# The AWS side
# ---------------------------------------------------------------------------


def test_nordix_is_boarded_into_aws_like_the_other_hub_tenants() -> None:
    """C001 owns an account (environment p, like the PROD VIF it sits behind) holding the VPC, which has a block."""
    account = _by_name("CloudAccount")["nordix-aws-prod"].item
    network = _by_name("CloudVirtualNetwork")[_VPC].item

    assert account["owner"] == _OWNER and account["environment"] == "p" and account["provider"] == "P004"
    assert network["account"] == "nordix-aws-prod" and network["region"] == "eu-central-1"
    assert network["cidr_blocks"] == [_VPC_BLOCK]
    assert tuple(_VPC_BLOCK) in _declared_prefixes()


def test_the_nordix_vpc_attachment_is_a_cross_account_attachment_onto_the_hub() -> None:
    """The attachment belongs to the tenant's account while the gateway is the hub's, associated like vaultex's."""
    attachment = _by_name("CloudTransitGatewayAttachment")["tgwa-vpc-nordix"].item
    vaultex = _by_name("CloudTransitGatewayAttachment")["tgwa-vpc-vaultex"].item

    assert attachment["attachment_type"] == "vpc"
    assert attachment["owner_account"] == "nordix-aws-prod" and attachment["virtual_network"] == _VPC
    assert attachment["transit_gateway"] == vaultex["transit_gateway"]
    assert attachment["associated_route_table"] == vaultex["associated_route_table"]
    assert attachment["propagates_to_route_tables"] == vaultex["propagates_to_route_tables"]
    assert attachment["cloud_id"] not in {
        a.item["cloud_id"] for n, a in _by_name("CloudTransitGatewayAttachment").items() if n != "tgwa-vpc-nordix"
    }


def test_the_transit_vif_session_registers_the_nordix_block_for_c001() -> None:
    """The VPC block is reachable over the hub: propagated into the hybrid attachment's table, owned by C001."""
    assert tuple(_VPC_BLOCK) in _hub_registered_blocks(_OWNER)


def test_the_hub_registers_no_block_for_an_owner_without_a_vpc() -> None:
    """Failure case of the derivation: an owner with no account behind the hub registers nothing."""
    assert _hub_registered_blocks("C002") == set()


def test_no_two_hub_tenants_share_a_block() -> None:
    """Two owners with one block would be a WARNING in the transform and an error of routing invariants rule 8."""
    blocks = [
        tuple(block)
        for network in _by_name("CloudVirtualNetwork").values()
        if network.item["account"] in _by_name("CloudAccount")
        for block in network.item["cidr_blocks"]
    ]
    assert len(blocks) == len(set(blocks))


# ---------------------------------------------------------------------------
# The egress rules
# ---------------------------------------------------------------------------


def test_the_source_segment_of_the_policy_is_owned_by_c001() -> None:
    """The owner whose registrations count is the owner of the segment the policy belongs to."""
    assert _owner_of_segment(_SOURCE_SEGMENT) == _OWNER
    assert {rule["source_segment"] for rule in _rules()} == {_SOURCE_SEGMENT}


@pytest.mark.parametrize("rule", _rules(), ids=lambda rule: rule["name"])
def test_every_destination_of_a_c001_egress_rule_is_registered_or_the_internet(rule: dict[str, Any]) -> None:
    """A destination prefix is a block C001 registers behind the AWS session or the tunnel; else the internet zone."""
    owner = _owner_of_segment(rule["source_segment"])
    registered = _hub_registered_blocks(owner) | _tunnel_registered_prefixes(owner)
    destinations = [tuple(prefix) for prefix in rule.get("destination_prefixes") or []]

    for prefix in destinations:
        assert prefix in registered, f"{rule['name']}: {prefix} is registered behind no session"
        assert prefix in _declared_prefixes(), f"{rule['name']}: {prefix} is not a declared prefix"
    if not destinations:
        assert rule["destination_zone"] == INTERNET_ZONE
        assert "destination_segment" not in rule


def test_the_rules_aim_at_the_vpc_block_the_acme_range_and_the_internet() -> None:
    """Rule 10 the nordix VPC block, rule 20 the Acme range behind the tunnel, rule 30 the internet zone."""
    rules = {rule["index"]: rule for rule in _rules()}

    assert rules[10]["destination_prefixes"] == [_VPC_BLOCK]
    assert rules[20]["destination_prefixes"] == [_ACME_RANGE]
    assert rules[30]["destination_zone"] == INTERNET_ZONE
    assert "destination_segment" not in rules[30]
    assert "destination_prefixes" not in rules[30]
    assert rules[30]["apply_on_switch"] is False


def test_the_internet_zone_is_declared_in_the_bootstrap() -> None:
    """Rule 30 names a zone that exists."""
    assert INTERNET_ZONE in {name(zone) for zone in declared("SecurityZone", BOOTSTRAP)}


def test_a_destination_registered_behind_nothing_is_rejected_by_the_derivation() -> None:
    """Failure case: the old C004 Azure range is registered by no hub tenant of C001, so it would fail the check above."""
    assert ("10.40.0.0/16", "default") not in _hub_registered_blocks(_OWNER) | _tunnel_registered_prefixes(_OWNER)


def test_the_unreachable_destinations_are_gone_from_the_demo() -> None:
    """No prefix object stands for the C004 Azure range (aimed at without a session) or the Zscaler PoP (internet space)."""
    prefixes = {prefix for prefix, _ in _declared_prefixes()}
    assert "198.51.100.0/24" not in prefixes
    assert ("10.40.0.0/16", "default") not in _declared_prefixes()
