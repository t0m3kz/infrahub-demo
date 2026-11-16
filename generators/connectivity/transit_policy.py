"""Give a transit peering its routing policies: the import and export policy of utils/transit_policy.py."""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.plan import EdgesSpec, NodeRef, NodeSpec, TopologyPlan
from utils.data_cleaning import clean_data
from utils.internet_prefixes import get_internet_prefixes
from utils.transit_policy import transit_spec

PEERING_KIND = "ManagedBGPPeering"
DIRECTION_OF = {"transit-in": "import_policies", "transit-out": "export_policies"}


class TransitPolicyGenerator(BaseGenerator):
    """Write the shared transit policies (``origin: generated``) and attach them to the peering. The lists, policies and
    community lists are shared catalogue objects (never tracked, never deleted: a person's policy stays); their entries
    and terms are owned: tracked, so the run's group cleanup removes the one the spec no longer has, and a person's
    entry in a generated list is never touched.
    """

    graphql_root_key = PEERING_KIND

    @staticmethod
    def _plan_prefix_list(plan: TopologyPlan, spec: dict[str, Any]) -> None:
        plist = f"prefix-list-{spec['name']}"
        plan.add(NodeSpec(plist, "RoutingPrefixList", {"name": spec["name"], "family": spec["family"]}))
        for entry in spec["entries"]:
            plan.add(
                NodeSpec(
                    f"{plist}-{entry['seq']}",
                    "RoutingPrefixListEntry",
                    {**entry, "prefix_list": NodeRef(plist)},
                    ownership="owned",
                )
            )

    @staticmethod
    def _plan_policy(plan: TopologyPlan, spec: dict[str, Any]) -> None:
        policy = f"policy-{spec['name']}"
        plan.add(
            NodeSpec(
                policy,
                "RoutingPolicy",
                {"name": spec["name"], "origin": "generated", "default_action": spec["default_action"]},
            )
        )
        for term in spec["terms"]:
            data: dict[str, Any] = {
                "policy": NodeRef(policy),
                "seq": term["seq"],
                "action": term["action"],
                "set_local_pref": term.get("local_pref"),
                "add_communities": term.get("add_communities") or [],
            }
            if term.get("prefix_list"):
                data["match_prefix_list"] = NodeRef(f"prefix-list-{term['prefix_list']}")
            if term.get("community_list"):
                data["match_community_list"] = NodeRef(f"community-list-{term['community_list']}")
            plan.add(NodeSpec(f"{policy}-{term['seq']}", "RoutingPolicyTerm", data, ownership="owned"))

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        peerings = cleaned.get(self.graphql_root_key) or []
        if not peerings:
            self.fail(f"No {self.graphql_root_key} data in GraphQL response")
        peering = peerings[0]
        spec = transit_spec(get_internet_prefixes(cleaned.get("public_origins")))
        # No root: the peering is the target. The shared lists and policies are untracked, their entries owned
        plan = TopologyPlan(root_id=None)
        for item in spec["prefix_lists"]:
            self._plan_prefix_list(plan, item)
        for item in spec["community_lists"]:
            plan.add(NodeSpec(f"community-list-{item['name']}", "RoutingCommunityList", item))
        for item in spec["policies"]:
            self._plan_policy(plan, item)
            plan.add(
                EdgesSpec(
                    node=peering["id"],
                    relation=DIRECTION_OF[item["name"]],
                    peers=(NodeRef(f"policy-{item['name']}"),),
                    kind=PEERING_KIND,
                )
            )
        await self.apply_plan(plan)
        self.logger.info(f"Peering {peering.get('name')}: transit policies attached")
