"""Give a transit peering its routing policies: the import and export policy of utils/transit_policy.py."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data
from utils.internet_prefixes import get_internet_prefixes
from utils.transit_policy import transit_spec

from ..common import CommonGenerator

PEERING_KIND = "ManagedBGPPeering"
DIRECTION_OF = {"transit-in": "import_policies", "transit-out": "export_policies"}


class TransitPolicyGenerator(CommonGenerator):
    """Write the shared transit policies (``origin: generated``) and attach them to the peering; untracked, a person's
    policy stays.
    """

    graphql_root_key = PEERING_KIND

    async def _save(self, kind: str, data: dict[str, Any]) -> Any:
        node = await self.client.create(kind=kind, data=data)
        await node.save(allow_upsert=True, update_group_context=False)
        return node

    async def _write_prefix_list(self, spec: dict[str, Any]) -> str:
        plist = await self._save("RoutingPrefixList", {"name": spec["name"], "family": spec["family"]})
        for entry in spec["entries"]:
            await self._save("RoutingPrefixListEntry", {**entry, "prefix_list": plist.id})
        wanted = {entry["seq"] for entry in spec["entries"]}
        for stale in await self.client.filters(kind="RoutingPrefixListEntry", prefix_list__ids=[plist.id]):
            if getattr(stale, "seq").value not in wanted:
                await stale.delete()
        return plist.id

    async def _write_policy(
        self, spec: dict[str, Any], prefix_lists: dict[str, str], community_lists: dict[str, str]
    ) -> str:
        policy = await self._save(
            "RoutingPolicy",
            {"name": spec["name"], "origin": "generated", "default_action": spec["default_action"]},
        )
        for term in spec["terms"]:
            data: dict[str, Any] = {
                "policy": policy.id,
                "seq": term["seq"],
                "action": term["action"],
                "set_local_pref": term.get("local_pref"),
                "add_communities": term.get("add_communities") or [],
            }
            if term.get("prefix_list"):
                data["match_prefix_list"] = prefix_lists[term["prefix_list"]]
            if term.get("community_list"):
                data["match_community_list"] = community_lists[term["community_list"]]
            await self._save("RoutingPolicyTerm", data)
        wanted = {term["seq"] for term in spec["terms"]}
        for stale in await self.client.filters(kind="RoutingPolicyTerm", policy__ids=[policy.id]):
            if getattr(stale, "seq").value not in wanted:
                await stale.delete()
        return policy.id

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        peerings = cleaned.get(self.graphql_root_key) or []
        if not peerings:
            self.fail(f"No {self.graphql_root_key} data in GraphQL response")
        peering = peerings[0]
        spec = transit_spec(get_internet_prefixes(cleaned.get("public_origins")))
        prefix_lists = {item["name"]: await self._write_prefix_list(item) for item in spec["prefix_lists"]}
        community_lists = {
            item["name"]: (await self._save("RoutingCommunityList", item)).id for item in spec["community_lists"]
        }
        node = await self.client.get(kind=PEERING_KIND, id=peering["id"], include=list(DIRECTION_OF.values()))
        for item in spec["policies"]:
            policy_id = await self._write_policy(item, prefix_lists, community_lists)
            relation = getattr(node, DIRECTION_OF[item["name"]])
            if policy_id not in {peer.id for peer in relation.peers}:
                relation.add(policy_id)
        await node.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"Peering {peering.get('name')}: transit policies attached")
