"""Explicit migration of the historical smoke routing/policy owners to the zone catalog.

Only zone relationships are migrated. An IPAM namespace, a CIDR, an interface
name or an unrelated dictionary called ``namespace`` never supplies authority.
"""

from __future__ import annotations

import copy
from typing import Any

from tests.smoke.fixture_builders.vrf import LEGACY_ZONE_NAMES, VRF_KIND, routing_vrf, routing_zone


def migrate_zone_owners(data: dict[str, Any]) -> dict[str, Any]:
    """Return catalog-shaped raw owners without altering protocols, addresses or VLAN rows."""
    result = copy.deepcopy(data)

    def zone(node: dict[str, Any]) -> None:
        name = (node.get("name") or {}).get("value")
        if not name:
            raise ValueError("Fixture routing owner has no zone name")
        node.update(routing_zone(name))

    def walk(item: Any, owner: str = "", deployment: str = "fixture-deployment") -> None:
        if isinstance(item, list):
            for child in item:
                walk(child, owner, deployment)
        elif isinstance(item, dict):
            kind = item.get("__typename") or owner
            host = (item.get("deployment") or {}).get("node") or {}
            host_name = (host.get("name") or {}).get("value")
            if host_name:
                deployment = host_name
            relation = item.get("security_zone") or {}
            zone_node = relation.get("node")
            if zone_node:
                zone(zone_node)
                name = zone_node["name"]["value"]
                if kind in {"IpamPrefix", "IpamIPAddress"} or "prefix" in item or "address" in item:
                    item.pop("security_zone")
                elif (
                    kind.startswith("Dcim")
                    and "Interface" in kind
                    or kind == "TopologySessionCircuit"
                    or "ip_address" in item
                    and "name" in item
                ):
                    item["vrf"] = {"node": {**routing_vrf(name, deployment), "security_zone": relation}}
                    item.pop("security_zone")
                elif kind == "TopologyCustomerOffice" or "lan_prefixes" in item:
                    gateway = (item.get("sdwan_gateway") or {}).get("node")
                    if gateway:
                        gateway.setdefault("interfaces", {"edges": []})["edges"].append(
                            {
                                "node": {
                                    "name": {"value": "fixture-handoff"},
                                    "capabilities": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "__typename": "TopologySessionCircuit",
                                                    "circuit_type": {"value": "handoff"},
                                                    "vrf": {"node": routing_vrf(name, deployment)},
                                                }
                                            }
                                        ]
                                    },
                                }
                            }
                        )
                    item.pop("security_zone")
                elif kind in {"CloudVirtualNetwork", "CloudGatewayAttachment"}:
                    item.pop("security_zone")
            for key, child in list(item.items()):
                if key in {"security_zone", "destination_zone"} and isinstance(child, dict) and child.get("node"):
                    zone(child["node"])
                if key == "vrf" and isinstance(child, dict) and child.get("node"):
                    vrf = child["node"]
                    if "namespace" in vrf:
                        if "security_zone" in vrf:
                            raise ValueError("Fixture VRF has both old and new routing owners")
                        vrf["security_zone"] = vrf.pop("namespace")
                    zone_ref = (vrf.get("security_zone") or {}).get("node")
                    if zone_ref:
                        name = zone_ref["name"]["value"]
                        for field, value in routing_vrf(name, deployment).items():
                            vrf.setdefault(field, value)
                if key in {"security_zone_a", "security_zone_z"} and isinstance(child, dict) and child.get("node"):
                    zone(child["node"])
                    item[f"vrf_{key[-1]}"] = {"node": routing_vrf(child["node"]["name"]["value"], deployment)}
                    item.pop(key)
                    continue
                next_owner = {
                    "ip_prefix": "IpamPrefix",
                    "ip_address": "IpamIPAddress",
                    "cidr_blocks": "IpamPrefix",
                    "allowed_prefixes": "IpamPrefix",
                    "registered_prefixes": "IpamPrefix",
                    "originated_prefixes": "IpamPrefix",
                    "virtual_network": "CloudVirtualNetwork",
                    "interfaces": "DcimVirtualInterface",
                }.get(key, kind if key in {"node", "edges"} else key)
                walk(child, next_owner, deployment)
            if "segment_deployments" in item and "lan_prefixes" in item:
                hosted = [
                    edge["node"]["segment"]["node"].get("security_zone")
                    for edge in item["segment_deployments"].get("edges") or []
                    if (edge["node"].get("segment") or {}).get("node")
                ]
                instances = item.setdefault("vrfs", {"edges": []})["edges"]
                existing = {edge["node"]["id"] for edge in instances}
                for ref in hosted:
                    if ref:
                        instance = routing_vrf(ref["node"]["name"]["value"], deployment)
                        if instance["id"] not in existing:
                            instances.append({"node": instance})
                            existing.add(instance["id"])
            if "shared_capabilities" in item:
                edges = item["shared_capabilities"].get("edges") or []
                seen: dict[str, dict[str, Any]] = {}
                keep = []
                for edge in edges:
                    node = edge["node"]
                    if node.get("__typename") == VRF_KIND:
                        name = node["security_zone"]["node"]["name"]["value"]
                        if name in seen:
                            if node != seen[name]:
                                raise ValueError(f"Fixture has different VRF instances for zone {name}")
                            continue
                        seen[name] = node
                    keep.append(edge)
                item["shared_capabilities"]["edges"] = keep

    walk(result)
    for edge in (result.get("SecurityZone") or {}).get("edges") or []:
        zone(edge["node"])
    return result


def legacy_zone_names(data: dict[str, Any]) -> set[str]:
    """The finite legacy zone aliases present on explicit relationships of a raw fixture."""
    found: set[str] = set()
    pending: list[Any] = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key in ("security_zone", "destination_zone"):
                name = (((item.get(key) or {}).get("node") or {}).get("name") or {}).get("value")
                if name in LEGACY_ZONE_NAMES:
                    found.add(name)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found
