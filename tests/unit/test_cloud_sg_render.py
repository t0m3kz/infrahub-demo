"""PR7: a cloud segment's policy rules render as one security group per segment."""

from __future__ import annotations

import copy
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from transforms.cloud.vpc_terraform import CloudVpcTerraform
from transforms.helpers.cloud import policy_security_group, prepare_cloud_data


def _end(name: str, *, cidr: str | None = None, gateway: str | None = None) -> dict[str, Any]:
    """A rule's segment end as PolicyRuleSegmentFields returns it (cleaned)."""
    end: dict[str, Any] = {"id": f"seg-{name}", "name": name}
    if cidr:
        end["cidr_block"] = cidr
    if gateway:
        end["gateway"] = {"ip_prefix": {"prefix": gateway}}
    return end


def _rule(index: int, name: str, src: dict, dst: dict, **extra: Any) -> dict[str, Any]:
    """A permit SecurityPolicyRule (cleaned) with tcp/443 unless overridden."""
    return {
        "id": f"rule-{name}",
        "index": index,
        "name": name,
        "action": "permit",
        "protocol": "tcp",
        "port_start": 443,
        "port_end": 443,
        "disabled": False,
        "source_segment": src,
        "destination_segment": dst,
        **extra,
    }


APP = _end("app", cidr="10.0.1.0/24")
DB = _end("db", cidr="10.0.2.0/24")
NOWHERE = _end("nowhere")


def _segment(own: list[dict], inbound: list[dict], name: str = "app", **policy: Any) -> dict[str, Any]:
    """A cloud segment (cleaned) with its own policy and inbound rules."""
    header = {"name": f"seg-{name}-egress", "enabled": True, "default_action": "deny", **policy}
    return {
        "id": f"seg-{name}",
        "name": name,
        "cidr_block": "10.0.1.0/24",
        "security_policy": {**header, "rules": own},
        "inbound_rules": [{**r, "policy": header} for r in inbound],
    }


def _cleaned(provider: str, segments: list[dict], instance_segment: str = "app") -> dict[str, Any]:
    """A minimal one-VPC account (cleaned) with `segments` and one instance."""
    return {
        "CloudVirtualNetwork": [
            {
                "id": "vpc-1",
                "name": "vpc",
                "region": {"name": "eu-central-1"},
                "cidr_blocks": [{"prefix": "10.0.0.0/16"}],
                "account": {"name": "acc", "account_id": "1", "provider": {"name": provider}},
                "network_segments": segments,
            }
        ],
        "CloudInstance": [
            {
                "id": "i-1",
                "name": "vm",
                "network_segment": {"name": instance_segment, "virtual_network": {"name": "vpc"}},
            }
        ],
    }


async def _render(cleaned: dict[str, Any]) -> str:
    """Run CloudVpcTerraform on already-cleaned data."""
    tf = CloudVpcTerraform(client=MagicMock(), infrahub_node=MagicMock(), root_directory="/tmp", branch="main")
    with patch("transforms.cloud.vpc_terraform.clean_data", return_value=cleaned):
        return await tf.transform({})


def test_egress_is_own_permits_and_ingress_is_inbound_permits() -> None:
    """Egress peers on the destination, ingress on the source, no reply legs."""
    own = [_rule(10, "to-db", APP, DB, port_start=5432, port_end=5432)]
    inbound = [_rule(10, "from-db", DB, APP, port_start=8080, port_end=8080)]
    sg = policy_security_group(_segment(own, inbound), "vpc")
    assert sg is not None
    assert sg["name"] == "app-sg"
    by_dir = {r["direction"]: r for r in sg["rules"]}
    assert len(sg["rules"]) == 2
    assert by_dir["egress"]["dest_cidr"] == "10.0.2.0/24"
    assert by_dir["egress"]["port_start"] == 5432
    assert by_dir["ingress"]["source_cidr"] == "10.0.2.0/24"
    assert by_dir["ingress"]["port_start"] == 8080


def test_gateway_prefix_wins_over_cidr_block() -> None:
    """The peer resolves through segment_prefix: gateway first, cidr_block otherwise."""
    peer = _end("onprem", cidr="192.0.2.0/24", gateway="172.16.0.1/24")
    sg = policy_security_group(_segment([_rule(10, "r", APP, peer)], []), "vpc")
    assert sg is not None
    assert sg["rules"][0]["dest_cidr"] == "172.16.0.1/24"


def test_rules_are_ordered_by_index_with_unique_priorities() -> None:
    """Rules render in index order per direction; priorities ascend and never repeat."""
    own = [_rule(30, "c", APP, DB), _rule(10, "a", APP, DB), _rule(20, "b", APP, DB)]
    inbound = [_rule(20, "y", DB, APP), _rule(10, "x", DB, APP)]
    sg = policy_security_group(_segment(own, inbound), "vpc")
    assert sg is not None
    egress = [r for r in sg["rules"] if r["direction"] == "egress"]
    ingress = [r for r in sg["rules"] if r["direction"] == "ingress"]
    assert [r["name"].split("-", 2)[2] for r in egress] == ["a", "b", "c"]
    assert [r["name"].split("-", 2)[2] for r in ingress] == ["x", "y"]
    assert [r["priority"] for r in egress] == sorted({r["priority"] for r in egress})


def test_unresolved_peer_is_skipped_and_warned(caplog: pytest.LogCaptureFixture) -> None:
    """A rule whose peer has no address is dropped with a warning, never widened."""
    own = [_rule(10, "to-nowhere", APP, NOWHERE), _rule(20, "to-db", APP, DB)]
    with caplog.at_level(logging.WARNING, logger="transforms.helpers.cloud"):
        sg = policy_security_group(_segment(own, []), "vpc")
    assert sg is not None
    assert [r["dest_cidr"] for r in sg["rules"]] == ["10.0.2.0/24"]
    assert any("to-nowhere" in message for message in caplog.messages)


def test_segment_with_only_unresolved_rules_yields_no_group() -> None:
    """Nothing resolvable means no group (not an empty or allow-all one)."""
    assert policy_security_group(_segment([_rule(10, "r", APP, NOWHERE)], []), "vpc") is None


def test_disabled_rules_and_disabled_policy_are_ignored() -> None:
    """Disabled rules and rules of a disabled policy render nothing."""
    own = [_rule(10, "off", APP, DB, disabled=True)]
    assert policy_security_group(_segment(own, []), "vpc") is None
    assert policy_security_group(_segment([_rule(10, "on", APP, DB)], [], enabled=False), "vpc") is None


def test_deny_rules_are_not_rendered() -> None:
    """A security group is permit-only."""
    own = [_rule(10, "deny", APP, DB, action="deny")]
    assert policy_security_group(_segment(own, []), "vpc") is None


def test_segment_without_policy_data_is_unchanged() -> None:
    """No rules, no group, and prepare_cloud_data leaves the data as it was."""
    plain = {"id": "s", "name": "app", "cidr_block": "10.0.1.0/24"}
    assert policy_security_group(plain, "vpc") is None
    cleaned = _cleaned("AWS", [plain])
    ctx = prepare_cloud_data(copy.deepcopy(cleaned))
    assert ctx["sgs_by_vpc"] == {}
    assert "security_groups" not in ctx["inst_by_vpc"]["vpc"][0]


def test_instance_binds_through_its_network_segment() -> None:
    """Only instances of the segment get its group."""
    segments = [_segment([_rule(10, "r", APP, DB)], [], name="app"), {"id": "o", "name": "other", "cidr_block": "x"}]
    cleaned = _cleaned("AWS", segments)
    cleaned["CloudInstance"].append(
        {
            "id": "i-2",
            "name": "elsewhere",
            "network_segment": {"name": "other", "virtual_network": {"name": "vpc"}},
        }
    )
    ctx = prepare_cloud_data(cleaned)
    bound, elsewhere = ctx["inst_by_vpc"]["vpc"]
    assert [sg["name"] for sg in bound["security_groups"]] == ["app-sg"]
    assert "security_groups" not in elsewhere


@pytest.mark.asyncio
async def test_aws_renders_rules_and_attaches_group_without_any_fallback() -> None:
    """AWS: one group, peer CIDRs on each rule, attached to the instance, no 0.0.0.0/0."""
    own = [_rule(10, "to-db", APP, DB), _rule(20, "to-nowhere", APP, NOWHERE)]
    out = await _render(_cleaned("AWS", [_segment(own, [_rule(10, "in", DB, APP)])]))
    assert 'resource "aws_security_group" "app_sg"' in out
    assert 'cidr_ipv4 = "10.0.2.0/24"' in out
    assert "aws_vpc_security_group_ingress_rule" in out
    assert "vpc_security_group_ids = [aws_security_group.app_sg.id]" in out
    assert "0.0.0.0/0" not in out
    assert "to_nowhere" not in out


@pytest.mark.asyncio
async def test_azure_binds_the_group_to_the_segment_subnet() -> None:
    """Azure: an NSG with priority-ordered rules, associated with the segment's subnet."""
    out = await _render(_cleaned("Azure", [_segment([_rule(10, "to-db", APP, DB)], [])]))
    assert 'resource "azurerm_network_security_group" "app_sg"' in out
    assert 'destination_address_prefix  = "10.0.2.0/24"' in out
    assert 'resource "azurerm_subnet_network_security_group_association" "app_sg_app_sg"' in out
    assert "azurerm_subnet.app.id" in out


@pytest.mark.asyncio
async def test_gcp_renders_one_ranged_firewall_per_rule_and_tags_instances() -> None:
    """GCP: a firewall per rule with the peer range and target tag; the instance carries the tag."""
    own = [_rule(10, "to-db", APP, DB), _rule(20, "to-db2", APP, DB, port_start=80, port_end=80)]
    out = await _render(_cleaned("GCP", [_segment(own, [_rule(10, "in", DB, APP)])]))
    assert out.count('resource "google_compute_firewall"') == 3
    assert 'destination_ranges = ["10.0.2.0/24"]' in out
    assert 'source_ranges = ["10.0.2.0/24"]' in out
    assert 'target_tags = ["app_sg"]' in out
    assert 'tags = ["app_sg"]' in out
