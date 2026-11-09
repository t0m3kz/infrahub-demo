"""The transit policies: the spec of utils/transit_policy.py and the generator that writes it."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.topology.transit_policy import TransitPolicyGenerator
from utils.transit_policy import (
    IN_POLICY,
    LEARNED_LIST,
    OUT_POLICY,
    PUBLIC_OUT_LIST,
    TRANSIT_COMMUNITY,
    TRANSIT_LOCAL_PREF,
    transit_spec,
)


def _policy(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """The policy called ``name`` of a spec."""
    return next(item for item in spec["policies"] if item["name"] == name)


def test_import_takes_the_default_only_tags_it_and_prefers_it_less() -> None:
    """The import policy permits the default route, tags it as learned from a transit and lowers its preference."""
    policy = _policy(transit_spec(["192.0.2.0/24"]), IN_POLICY)

    assert policy["default_action"] == "deny"
    assert policy["terms"] == [
        {
            "seq": 10,
            "action": "permit",
            "prefix_list": "TRANSIT-DEFAULT-IN",
            "local_pref": TRANSIT_LOCAL_PREF,
            "add_communities": [TRANSIT_COMMUNITY],
        }
    ]
    assert TRANSIT_LOCAL_PREF < 100


def test_export_denies_what_was_learned_from_a_transit_then_announces_the_public_space() -> None:
    """The tag is checked first, so two upstream providers are never joined through us."""
    spec = transit_spec(["192.0.2.0/24", "198.51.100.0/24"])
    policy = _policy(spec, OUT_POLICY)

    assert [(t["seq"], t["action"]) for t in policy["terms"]] == [(10, "deny"), (20, "permit")]
    assert policy["terms"][0]["community_list"] == LEARNED_LIST
    assert policy["terms"][1]["prefix_list"] == PUBLIC_OUT_LIST
    public = next(item for item in spec["prefix_lists"] if item["name"] == PUBLIC_OUT_LIST)
    assert [e["prefix"] for e in public["entries"]] == ["192.0.2.0/24", "198.51.100.0/24"]
    assert [e["seq"] for e in public["entries"]] == [10, 20]


def test_without_public_space_nothing_is_announced() -> None:
    """No public prefix: no list, no permit term; the default action denies the rest."""
    spec = transit_spec([])
    policy = _policy(spec, OUT_POLICY)

    assert [t["action"] for t in policy["terms"]] == ["deny"]
    assert PUBLIC_OUT_LIST not in {item["name"] for item in spec["prefix_lists"]}
    assert policy["default_action"] == "deny"


def test_the_tag_is_a_valid_standard_community() -> None:
    """ASN:value, both in range, and a community list holds it."""
    asn, value = TRANSIT_COMMUNITY.split(":")
    assert 0 < int(asn) < 65535 and 0 < int(value) < 65535
    assert transit_spec([])["community_lists"][0]["members"] == [TRANSIT_COMMUNITY]


def _generator(prefixes: list[str] | None = None) -> tuple[TransitPolicyGenerator, MagicMock, list[tuple[str, dict]]]:
    """A generator over a mocked client, the saved (kind, data) pairs and the peering node it attaches to."""
    saved: list[tuple[str, dict]] = []
    generator = TransitPolicyGenerator.__new__(TransitPolicyGenerator)
    client = MagicMock()

    async def _create(*, kind: str, data: dict) -> Any:
        saved.append((kind, data))
        node = MagicMock()
        node.id = f"{kind}:{data.get('name') or data.get('seq')}"
        node.save = AsyncMock()
        return node

    peering = MagicMock()
    peering.import_policies.peers = []
    peering.export_policies.peers = []
    peering.save = AsyncMock()
    client.create = AsyncMock(side_effect=_create)
    client.filters = AsyncMock(return_value=[])
    client.get = AsyncMock(return_value=peering)
    generator.client = client
    generator.logger = MagicMock()
    return generator, peering, saved


def _data(prefixes: list[str]) -> dict[str, Any]:
    """The transit_policy query result for one peering and the given public prefixes."""
    return {
        "ManagedBGPPeering": {"edges": [{"node": {"id": "peer-1", "name": {"value": "TRANSIT-X"}}}]},
        "IpamPrefix": {
            "edges": [
                {
                    "node": {
                        "security_zone": {"node": {"vrf_type": {"value": "internet"}}},
                        "prefix": {"value": p},
                        "ip_namespace": {"node": {"name": {"value": "default"}}},
                    }
                }
                for p in prefixes
            ]
        },
    }


async def test_the_generator_writes_the_objects_and_attaches_both_policies() -> None:
    """Lists, entries, community list, policies and terms are saved; each policy joins its direction once."""
    generator, peering, saved = _generator()
    await generator.generate(_data(["192.0.2.0/24"]))

    kinds = [kind for kind, _ in saved]
    assert {
        "RoutingPrefixList",
        "RoutingPrefixListEntry",
        "RoutingCommunityList",
        "RoutingPolicy",
        "RoutingPolicyTerm",
    } == set(kinds)
    policies = {data["name"]: data for kind, data in saved if kind == "RoutingPolicy"}
    assert {data["origin"] for data in policies.values()} == {"generated"}
    peering.import_policies.add.assert_called_once_with("RoutingPolicy:transit-in")
    peering.export_policies.add.assert_called_once_with("RoutingPolicy:transit-out")
    peering.save.assert_awaited_once()


async def test_an_attached_policy_is_not_added_again() -> None:
    """A re-run adds nothing to a peering that already has the policy."""
    generator, peering, _ = _generator()
    peering.import_policies.peers = [MagicMock(id="RoutingPolicy:transit-in")]
    peering.export_policies.peers = [MagicMock(id="RoutingPolicy:transit-out")]
    await generator.generate(_data([]))

    peering.import_policies.add.assert_not_called()
    peering.export_policies.add.assert_not_called()


async def test_entries_the_public_space_no_longer_has_are_deleted() -> None:
    """A list entry whose seq is not wanted any more goes; a wanted one stays."""
    generator, _, _ = _generator()
    stale, kept = MagicMock(), MagicMock()
    stale.seq.value, kept.seq.value = 30, 10
    stale.delete, kept.delete = AsyncMock(), AsyncMock()
    generator.client.filters = AsyncMock(return_value=[stale, kept])
    await generator.generate(_data(["192.0.2.0/24"]))

    stale.delete.assert_awaited()
    kept.delete.assert_not_awaited()


async def test_a_response_without_the_peering_fails_the_run() -> None:
    """No peering in the query data is an error, not a silent no-op."""
    generator, _, _ = _generator()
    with pytest.raises(GeneratorError):
        await generator.generate({"ManagedBGPPeering": {"edges": []}})


def test_the_full_topology_data_is_the_spec_of_the_generator() -> None:
    """00_full_topology authors the transit policies as plain objects (its peering is in no generator group): they must
    stay what utils/transit_policy.py derives from the public ranges of the data."""
    import yaml  # noqa: PLC0415

    from tests.integration.demo_data import ROOT  # noqa: PLC0415

    folder = ROOT / "data" / "demos" / "00_full_topology"
    documents = [
        doc
        for path in sorted((folder / "90_bindings/90_internet/03_routing").glob("03_routing_policies*.yml"))
        for doc in yaml.safe_load_all(path.read_text())
        if doc
    ]
    by_kind: dict[str, list[dict]] = {}
    for doc in documents:
        by_kind.setdefault(doc["spec"]["kind"], []).extend(doc["spec"]["data"])
    public = [
        item["prefix"]
        for doc in yaml.safe_load_all((folder / "00_common/04_internet/01_prefixes.yml").read_text())
        if doc
        for item in doc["spec"]["data"]
        if item.get("role") == "public" and ":" not in item["prefix"]
    ]
    spec = transit_spec(sorted(public))

    lists = {item["name"]: item for item in by_kind["RoutingPrefixList"] if item["name"] != "PL-OBJECT-STORE-PUBLIC"}
    assert set(lists) == {item["name"] for item in spec["prefix_lists"]}
    for item in spec["prefix_lists"]:
        assert [(e["seq"], e["action"], e["prefix"]) for e in lists[item["name"]]["entries"]["data"]] == [
            (e["seq"], e["action"], e["prefix"]) for e in item["entries"]
        ]
    community = by_kind["RoutingCommunityList"][0]
    assert (community["name"], community["members"]) == (
        spec["community_lists"][0]["name"],
        spec["community_lists"][0]["members"],
    )
    policies = {p["name"]: p for p in by_kind["RoutingPolicy"] if p["origin"] == "generated"}
    assert set(policies) == {p["name"] for p in spec["policies"]}
    for item in spec["policies"]:
        authored = policies[item["name"]]
        assert authored["default_action"] == item["default_action"]
        assert [
            (t["seq"], t["action"], t.get("set_local_pref"), t.get("add_communities") or [])
            for t in authored["terms"]["data"]
        ] == [(t["seq"], t["action"], t.get("local_pref"), t.get("add_communities") or []) for t in item["terms"]]
