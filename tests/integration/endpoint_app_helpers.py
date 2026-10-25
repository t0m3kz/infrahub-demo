"""Endpoint cabling and application graph fetchers (test_61)."""

import asyncio
import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .query_files import QUERY_GET_APPLICATION_GRAPH, QUERY_GET_ENDPOINT_CABLING
from .suite_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


# ======================================================================
# Endpoint cabling, applications, interconnects
# ======================================================================


async def fetch_endpoint_cabling(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Fetch every endpoint host with its rack position and cabled peers.

    The peer list a cable exposes includes the host's own interface, so the
    local end is filtered out here — callers only ever care about what the host
    is attached *to* (queries/get_endpoint_cabling.gql).

    Returns:
        [{"name": str, "rack": str | None, "rack_row": int | None,
          "rack_type": str | None,
          "links": [{"local_interface": str, "cable": str,
                     "peer_device": str, "peer_role": str,
                     "peer_interface": str}]}]
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_ENDPOINT_CABLING)
    result = clean_data(raw)

    hosts: list[dict[str, Any]] = []
    for device in result.get("DcimPhysicalDevice", []) or []:
        host_name = str(device.get("name") or "")
        rack = device.get("rack") or {}
        links: list[dict[str, Any]] = []
        for interface in device.get("interfaces", []) or []:
            cable = interface.get("cable")
            if not cable:
                continue
            for peer in cable.get("endpoints", []) or []:
                peer_device = peer.get("device") or {}
                if str(peer_device.get("name") or "") == host_name:
                    continue
                links.append(
                    {
                        "local_interface": str(interface.get("name") or ""),
                        "cable": str(cable.get("name") or ""),
                        "peer_device": str(peer_device.get("name") or ""),
                        "peer_role": str(peer_device.get("role") or ""),
                        "peer_interface": str(peer.get("name") or ""),
                    }
                )
        hosts.append(
            {
                "name": host_name,
                "rack": rack.get("name"),
                "rack_row": rack.get("row_index"),
                "rack_type": rack.get("rack_type"),
                "links": links,
            }
        )

    logger.info("Endpoint cabling on branch '%s': %d host(s)", branch, len(hosts))
    for host in sorted(hosts, key=lambda h: h["name"]):
        peers = ", ".join(f"{link['peer_device']}:{link['peer_interface']}" for link in host["links"])
        logger.info("  %s (rack=%s row=%s) -> %s", host["name"], host["rack"], host["rack_row"], peers or "<uncabled>")
    return hosts


async def fetch_application_graph(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Fetch every application with its components, segments and instances.

    Returns:
        [{"name": str, "criticality": str,
          "components": [{"fqdn": str, "component_type": str,
                          "segment": str | None, "segment_kind": str | None,
                          "instances": [{"name": str, "kind": str,
                                         "host": str | None,
                                         "host_role": str | None}]}]}]
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_APPLICATION_GRAPH)
    result = clean_data(raw)

    applications: list[dict[str, Any]] = []
    for app in result.get("AppApplication", []) or []:
        components: list[dict[str, Any]] = []
        for component in app.get("children", []) or []:
            segment = component.get("network_segment") or {}
            instances: list[dict[str, Any]] = []
            for instance in component.get("instances", []) or []:
                host = instance.get("hosting_device") or {}
                instances.append(
                    {
                        "name": str(instance.get("name") or ""),
                        "kind": str(instance.get("typename") or ""),
                        "host": host.get("name"),
                        "host_role": host.get("role"),
                    }
                )
            components.append(
                {
                    "fqdn": str(component.get("fqdn") or ""),
                    "component_type": str(component.get("component_type") or ""),
                    "segment": segment.get("name"),
                    "segment_kind": segment.get("typename"),
                    "instances": instances,
                }
            )
        applications.append(
            {
                "name": str(app.get("name") or ""),
                "criticality": str(app.get("criticality") or ""),
                "components": components,
            }
        )

    total_components = sum(len(app["components"]) for app in applications)
    logger.info(
        "Application graph on branch '%s': %d app(s), %d component(s)",
        branch,
        len(applications),
        total_components,
    )
    for app in sorted(applications, key=lambda a: a["name"]):
        logger.info("  %s (%s)", app["name"], app["criticality"])
        for component in app["components"]:
            hosts = ", ".join(f"{i['name']}@{i['host'] or i['kind']}" for i in component["instances"])
            logger.info(
                "    %s [%s] segment=%s -> %s",
                component["fqdn"],
                component["component_type"],
                component["segment"],
                hosts,
            )
    return applications
