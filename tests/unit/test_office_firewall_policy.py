"""transforms/topology/office_firewall_policy.py: the stateful policy of an office's SD-WAN edge, from the segment rules."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import yaml

from tests.integration.demo_data import ROOT
from transforms.topology.office_firewall_policy import OfficeFirewallPolicy

QUERY = ROOT / "queries" / "config" / "office_firewall_policy.gql"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _rule(
    index: int, name: str, *, zone: str | None = None, prefix: str | None = None, port: int | None = None
) -> dict:
    """A SecurityPolicyRule row of the query: to a zone (the internet) or to a prefix."""
    return {
        "id": f"rule-{name}",
        "index": _v(index),
        "name": _v(name),
        "action": _v("permit"),
        "protocol": _v("tcp" if port else "any"),
        "port_start": _v(port),
        "port_end": _v(None),
        "log": _v(False),
        "disabled": _v(False),
        "description": _v(""),
        "apply_on_switch": _v(False),
        "security_profile": {"node": None},
        "source_segment": {"node": None},
        "destination_segment": {"node": None},
        # A prefix selector carries no routing authority of its own: the rule names its zone (the office is PROD).
        "destination_zone": {"node": {"name": _v(zone or "PROD" if prefix else zone)} if zone or prefix else None},
        "source_prefixes": _edges(),
        "destination_prefixes": _edges(
            *(
                [
                    {
                        "id": "p",
                        "prefix": _v(prefix),
                        "ip_namespace": {"node": {"name": {"value": "default"}}},
                    }
                ]
                if prefix
                else []
            )
        ),
        "source_ip_addresses": _edges(),
        "destination_ip_addresses": _edges(),
    }


def _segment(name: str, prefix: str, *rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"seg-{name}",
        "name": _v(name),
        "customer_name": _v(name),
        "environment": _v("p"),
        "gateway": {"node": {"ip_prefix": {"node": {"prefix": _v(prefix)}}}},
        "security_zone": {"node": {"name": _v("PROD"), "trust_level": _v(70)}},
        "security_policy": {
            "node": {
                "id": f"pol-{name}",
                "name": _v(f"policy-{name}"),
                "enabled": _v(True),
                "default_action": _v("deny"),
                "segment": {"node": None},
                # a rule id is global: the same name in two policies is two rules
                "rules": _edges(*({**rule, "id": f"{name}-{rule['id']}"} for rule in rules)),
            }
        },
        "inbound_rules": _edges(),
    }


def _office(*segments: dict[str, Any]) -> dict[str, Any]:
    return {
        "TopologyCustomerOffice": _edges(
            {
                "__typename": "TopologyCustomerOffice",
                "id": "office-1",
                "name": _v("C900-OFFICE"),
                "owner": {"node": {"org_id": _v("C900")}},
                "network_segments": _edges(*segments),
            }
        )
    }


def _run(data: dict[str, Any]) -> Any:
    transform = cast(Any, OfficeFirewallPolicy.__new__(OfficeFirewallPolicy))
    result = asyncio.run(transform.transform(data))
    return json.loads(result) if result else result


GUEST = _segment("c900-wifi-guest-p", "10.96.1.0/24", _rule(10, "internet", zone="INTERNET"))
CORP = _segment(
    "c900-wifi-corp-p",
    "10.96.2.0/24",
    _rule(10, "intranet", prefix="10.90.1.0/24", port=443),
    _rule(20, "internet", zone="INTERNET"),
)


def test_the_payload_names_the_office_its_zones_and_its_segments_with_their_closing_action() -> None:
    payload = _run(_office(GUEST, CORP))

    assert payload["office"] == "C900-OFFICE" and payload["owner"] == "C900"
    assert payload["zones"] == ["INTERNET", "PROD"]
    assert payload["segments"] == [
        {"name": "c900-wifi-corp-p", "zone": "PROD", "prefix": "10.96.2.0/24", "default_action": "deny"},
        {"name": "c900-wifi-guest-p", "zone": "PROD", "prefix": "10.96.1.0/24", "default_action": "deny"},
    ]


def test_a_guest_segment_reaches_only_the_internet_and_the_corporate_one_also_its_intranet() -> None:
    payload = _run(_office(GUEST, CORP))
    by_policy = {policy["name"]: policy["rules"] for policy in payload["policies"]}

    # the one implicit deny-all of the table closes its last policy (get_zone_policies), here the guest's
    guest = by_policy["policy-c900-wifi-guest-p"]
    assert [(rule["action"], rule["dst_zone"]) for rule in guest] == [("permit", "INTERNET"), ("deny", None)]
    corp = by_policy["policy-c900-wifi-corp-p"]
    assert [rule["action"] for rule in corp] == ["permit", "permit"]
    assert corp[0]["dst"] == "10.90.1.0/24" and corp[0]["dst_port"] == "eq 443"
    assert corp[1]["dst_zone"] == "INTERNET"
    assert all(rule["dst"] != "10.90.1.0/24" for rule in guest)


def test_the_rules_are_numbered_across_the_whole_table() -> None:
    """One flat table for the edge, as a firewall context's: the sequence never restarts per policy."""
    payload = _run(_office(GUEST, CORP))
    sequence = [rule["seq"] for policy in payload["policies"] for rule in policy["rules"]]

    assert sequence == sorted(sequence) and len(set(sequence)) == len(sequence)


def test_an_office_without_segments_gives_an_empty_policy_and_no_office_gives_nothing() -> None:
    assert _run(_office())["policies"] == []
    assert _run({"TopologyCustomerOffice": _edges()}) == ""


def test_the_query_reads_the_segments_through_the_shared_fragments_at_the_node() -> None:
    """The zone and the rules come from the fragments every consumer reads, spread at the segment node."""
    text = QUERY.read_text()
    node = text.split("network_segments {", 1)[1]

    assert "...SegmentSecurityZoneFields" in node and "...SegmentRulesFields" in node
    assert "... on" not in node
    assert "gateway { node { ip_prefix { node { prefix { value } } } } }" in node


def test_the_artifact_targets_the_office_group_and_is_registered() -> None:
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    artifact = next(a for a in config["artifact_definitions"] if a["name"] == "office_firewall_policy")
    transforms = {t["name"]: t for t in config["python_transforms"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert artifact["targets"] == "customer_deployments_office" and artifact["content_type"] == "application/json"
    assert transforms[artifact["transformation"]]["class_name"] == "OfficeFirewallPolicy"
    assert (
        OfficeFirewallPolicy.query == "office_firewall_policy" and (ROOT / queries["office_firewall_policy"]).is_file()
    )
