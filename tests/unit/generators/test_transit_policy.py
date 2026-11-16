"""The transit policies: the spec of utils/transit_policy.py and the generator that writes it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.connectivity.transit_policy import TransitPolicyGenerator
from generators.model.errors import GeneratorError
from tests.unit.plan_reconciler_helpers import FakeClient, kind_name, make_generator_of
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


class _Policies(FakeClient):
    """The shared fake client plus what transit policy reads: its entries and terms by parent, the peering's edges."""

    peering: Any

    def __init__(self) -> None:
        super().__init__()
        self.edge_calls: list[dict[str, Any]] = []

    async def filters(self, kind: Any, **flt: Any) -> list[Any]:
        name = kind_name(kind)
        if name == "ManagedBGPPeering":
            return [self.peering]
        for parent in ("prefix_list", "policy"):
            if f"{parent}__ids" in flt:
                return [
                    node
                    for node in self.store.values()
                    if node.kind == name and node.data.get(parent) in flt[f"{parent}__ids"]
                ]
        return await super().filters(kind, **flt)

    async def create(self, kind: Any, data: dict[str, Any]) -> Any:
        """Upsert by the object's key like the server (name; list and seq; policy and seq): the same key, the same id."""

        def key(item: dict[str, Any]) -> tuple[Any, ...]:
            return (item.get("name"), item.get("prefix_list"), item.get("policy"), item.get("seq"))

        node = await super().create(kind, data)
        for existing in self.store.values():
            if existing.kind == node.kind and key(existing.data) == key(node.data):
                node.id = existing.id
                break
        return node

    async def execute_graphql(self, query: str, variables: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.edge_calls.append(variables["data"])
        return {}


def _generator() -> tuple[TransitPolicyGenerator, _Policies]:
    """A generator over the in-memory client; the peering starts with no policy attached."""
    client = _Policies()
    client.peering = SimpleNamespace(
        id="peer-1", import_policies=SimpleNamespace(peers=[]), export_policies=SimpleNamespace(peers=[])
    )
    return make_generator_of(TransitPolicyGenerator, client), client


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


_OWNED = ("RoutingPrefixListEntry", "RoutingPolicyTerm")
"""The kinds the run owns: tracked, so the group cleanup removes the one a later run no longer saves."""


async def test_the_generator_writes_the_objects_and_attaches_both_policies() -> None:
    """The shared lists and policies are saved untracked, their entries and terms tracked; each policy joins its
    direction once."""
    generator, client = _generator()
    await generator.generate(_data(["192.0.2.0/24"]))

    assert {kind for kind, *_ in client.saves} == {
        "RoutingPrefixList",
        "RoutingPrefixListEntry",
        "RoutingCommunityList",
        "RoutingPolicy",
        "RoutingPolicyTerm",
    }
    assert {kind: kwargs for kind, _, _, kwargs in client.saves if kind not in _OWNED} == {
        kind: {"allow_upsert": True, "update_group_context": False}
        for kind in {k for k, *_ in client.saves} - set(_OWNED)
    }
    assert all(kwargs == {"allow_upsert": True} for kind, _, _, kwargs in client.saves if kind in _OWNED)
    policies = {data["name"]: (node_id, data) for kind, node_id, data, _ in client.saves if kind == "RoutingPolicy"}
    assert {data["origin"] for _, data in policies.values()} == {"generated"}
    assert [(call["id"], call["name"], [node["id"] for node in call["nodes"]]) for call in client.edge_calls] == [
        ("peer-1", "import_policies", [policies["transit-in"][0]]),
        ("peer-1", "export_policies", [policies["transit-out"][0]]),
    ]


async def test_a_term_points_at_the_ids_of_its_policy_and_lists() -> None:
    """Refs are resolved: a term carries the saved ids of its policy and prefix list, an entry those of its list."""
    generator, client = _generator()
    await generator.generate(_data([]))

    by_kind = {kind: (node_id, data) for kind, node_id, data, _ in client.saves}
    plist_id = next(node_id for kind, node_id, _, _ in client.saves if kind == "RoutingPrefixList")
    assert by_kind["RoutingPrefixListEntry"][1]["prefix_list"] == plist_id
    term = next(
        data for kind, _, data, _ in client.saves if kind == "RoutingPolicyTerm" and "match_prefix_list" in data
    )
    assert term["match_prefix_list"] in {node_id for kind, node_id, _, _ in client.saves if kind == "RoutingPrefixList"}
    assert term["policy"] in {node_id for kind, node_id, _, _ in client.saves if kind == "RoutingPolicy"}


async def test_an_attached_policy_is_not_added_again() -> None:
    """A re-run adds nothing to a peering that already has the policies (same ids: the first run saved them)."""
    generator, client = _generator()
    await generator.generate(_data([]))
    saved_ids = {data["name"]: node_id for kind, node_id, data, _ in client.saves if kind == "RoutingPolicy"}
    client.edge_calls.clear()
    client.peering.import_policies.peers = [SimpleNamespace(id=saved_ids["transit-in"])]
    client.peering.export_policies.peers = [SimpleNamespace(id=saved_ids["transit-out"])]
    await generator.generate(_data([]))

    assert client.edge_calls == []


async def test_an_entry_or_term_nobody_generated_is_never_deleted() -> None:
    """A person's entry in a generated list and a person's term in a generated policy stay: the run deletes only what
    it owns (its group), never every entry of a list.
    """
    generator, client = _generator()
    await generator.generate(_data(["192.0.2.0/24"]))
    plist_id = next(node_id for kind, node_id, data, _ in client.saves if kind == "RoutingPrefixList")
    policy_id = next(node_id for kind, node_id, data, _ in client.saves if kind == "RoutingPolicy")
    entry = client.seed("RoutingPrefixListEntry", seq=30, prefix_list=plist_id)
    term = client.seed("RoutingPolicyTerm", seq=99, policy=policy_id)
    await generator.generate(_data(["192.0.2.0/24"]))

    assert client.deleted == []
    assert {entry.id, term.id} <= set(client.store)


async def test_the_entries_the_run_wrote_are_the_ones_it_owns(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry the spec no longer has is not rewritten, so the group cleanup of the next run removes it: only the
    wanted seqs are saved.
    """
    generator, client = _generator()
    origins = ["192.0.2.0/24", "198.51.100.0/24"]
    monkeypatch.setattr("generators.connectivity.transit_policy.get_internet_prefixes", lambda _: origins)
    await generator.generate(_data([]))
    client.saves.clear()
    origins.pop()
    await generator.generate(_data([]))

    public = [
        data["seq"]
        for kind, _, data, _ in client.saves
        if kind == "RoutingPrefixListEntry" and data.get("prefix") in ("192.0.2.0/24", "198.51.100.0/24")
    ]
    assert public == [10]


async def test_a_second_run_saves_the_same_objects_and_deletes_nothing() -> None:
    """Idempotent: the same nodes by key, the same ids, nothing pruned."""
    generator, client = _generator()
    await generator.generate(_data(["192.0.2.0/24"]))
    first = {(kind, node_id) for kind, node_id, *_ in client.saves}
    await generator.generate(_data(["192.0.2.0/24"]))

    assert client.deleted == []
    assert {(kind, node_id) for kind, node_id, *_ in client.saves} >= first


async def test_a_response_without_the_peering_fails_the_run() -> None:
    """No peering in the query data is an error, not a silent no-op."""
    generator, _ = _generator()
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
