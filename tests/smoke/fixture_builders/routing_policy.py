"""Routing-policy fixtures: a leaf whose first peering carries an import and an export policy, enforced and staged."""

from __future__ import annotations

from tests.smoke.fixture_builders.common import _edges, _v, write_data_fixtures
from tests.smoke.fixture_builders.routing_protection import build_protected_device_data
from transforms.config.leaf import Leaf

PLATFORMS = ("arista_eos", "cisco_nxos", "cisco_xe", "sonic", "dell_sonic")
PEERING = "dc1-leaf-01-spine-01"


def _prefix_list(name: str, family: str, *entries: tuple[int, str, str, int | None]) -> dict:
    return {
        "name": _v(name),
        "family": _v(family),
        "entries": _edges(
            [
                {
                    "seq": _v(seq),
                    "action": _v(action),
                    "prefix": _v(prefix),
                    "min_length": _v(None),
                    "max_length": _v(max_length),
                }
                for seq, action, prefix, max_length in entries
            ]
        ),
    }


def _community_list(name: str, *members: str, kind: str = "standard", match: str = "any") -> dict:
    return {
        "name": _v(name),
        "community_type": _v(kind),
        "members": _v(list(members)),
        "match": _v(match),
    }


def _term(seq: int, action: str = "permit", **fields: object) -> dict:
    node: dict = {
        "seq": _v(seq),
        "action": _v(action),
        "set_local_pref": _v(fields.get("local_pref")),
        "set_med": _v(None),
        "add_communities": _v(fields.get("add_communities") or []),
        "prepend_count": _v(fields.get("prepend")),
        "match_prefix_list": {"node": fields.get("prefix_list")},
        "match_community_list": {"node": fields.get("community_list")},
        "delete_community_list": {"node": fields.get("delete_list")},
    }
    return {"node": node}


def _policy(name: str, default: str, *terms: dict) -> dict:
    return {
        "node": {
            "name": _v(name),
            "origin": _v("manual"),
            "priority": _v(100),
            "default_action": _v(default),
            "terms": {"edges": list(terms)},
        }
    }


def add_policies(data: dict) -> None:
    """Attach the import and the export policy to the first peering of the device, wherever the fixture lists it."""
    own = _prefix_list("PL-OWN", "ipv4", (10, "permit", "10.0.0.0/8", 32))
    learned = _community_list("CL-LEARNED", "64500:100")
    internal = _community_list("CL-INTERNAL", "64500:900", "64500:901")
    imported = _policy(
        "transit-in",
        "permit",
        _term(10, community_list=learned, local_pref=300, add_communities=["64500:100"]),
        _term(20, prefix_list=own, add_communities=["64500:200", "64500:1:2"]),
    )
    exported = _policy(
        "transit-out",
        "deny",
        _term(10, "deny", community_list=learned),
        _term(20, prefix_list=own, add_communities=["no-export"], prepend=2, delete_list=internal),
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    for edge in node["capabilities"]["edges"]:
        for peering in ((edge["node"].get("peerings") or {}).get("edges")) or []:
            if PEERING in peering["node"]["id"]:
                peering["node"]["import_policies"] = {"edges": [imported]}
                peering["node"]["export_policies"] = {"edges": [exported]}


def write_fixtures() -> tuple[int, int]:
    """Write the enforced and the staged leaf fixture of the policies per CLI platform."""
    fixtures: list[tuple[type, str, dict]] = []
    for platform in PLATFORMS:
        for enforce in (False, True):
            data = build_protected_device_data(
                device_name="dc1-leaf-01", role="leaf", platform=platform, enforce=enforce
            )
            add_policies(data)
            fixtures.append((Leaf, f"leaf_{platform}_routing_policy_{'enforced' if enforce else 'staged'}", data))
    print("\nGenerating routing-policy fixtures (leaf; staged and enforced):")
    return write_data_fixtures(fixtures)


def _spec_policy(spec: dict, lists: dict) -> dict:
    """A policy node of utils/transit_policy.py's spec, the way the transform query returns it."""
    terms = [
        _term(
            term["seq"],
            term["action"],
            local_pref=term.get("local_pref"),
            add_communities=term.get("add_communities"),
            prefix_list=lists.get(term.get("prefix_list")),
            community_list=lists.get(term.get("community_list")),
        )
        for term in spec["terms"]
    ]
    node = _policy(spec["name"], spec["default_action"], *terms)
    node["node"]["origin"] = _v("generated")
    return node


def add_transit_policies(data: dict, public_prefixes: list[str]) -> None:
    """Attach the generated transit policies (utils/transit_policy.py) to every peering of the device."""
    from utils.transit_policy import transit_spec  # noqa: PLC0415 - only the transit fixture needs it

    spec = transit_spec(public_prefixes)
    lists: dict = {
        item["name"]: _prefix_list(
            item["name"],
            item["family"],
            *((e["seq"], e["action"], e["prefix"], e["max_length"]) for e in item["entries"]),
        )
        for item in spec["prefix_lists"]
    }
    lists |= {
        item["name"]: _community_list(item["name"], *item["members"], kind=item["community_type"], match=item["match"])
        for item in spec["community_lists"]
    }
    imported, exported = (_spec_policy(item, lists) for item in spec["policies"])

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for peering in ((node.get("peerings") or {}).get("edges")) or []:
                if "EXT-LUMEN-FRA1" not in peering["node"]["id"]:
                    continue
                peering["node"]["import_policies"] = {"edges": [imported]}
                peering["node"]["export_policies"] = {"edges": [exported]}
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(data)
