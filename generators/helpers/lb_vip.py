"""Pure planning for the load balancer VIP producer (generators/lb_vip.py).

Inputs are cleaned application.gql data (utils.data_cleaning.clean_data): a
component with ``ports``, ``instances`` (an on-prem device with ``deployment``
and its ``serving_loadbalancer_context``; a cloud instance with ``private_ip``
and ``network_segment`` -> ``virtual_network`` -> ``cidr_blocks``) and
``network_segment``.
"""

from __future__ import annotations

import hashlib
import re
from ipaddress import ip_interface, ip_network
from typing import Any

from utils.limits import CLOUD_LB_NAME_MAX
from utils.ports import PortProfileHelper, PortSpec

VIP_PREFIXLEN = 32
"""A VIP is one host address of its context's routed VIP range."""

CLOUD_INSTANCE_KIND = "CloudInstance"

CLOUD_LB_TYPE = "l4_cloud"
"""A VIP's protocol is the tcp/udp of a component port (utils.ports), so a listener is layer 4."""


def component_ref(component: dict[str, Any]) -> str:
    """How a component is named in logs, errors and its VIP's hostname: its fqdn, else its name or id."""
    return str(component.get("fqdn") or component.get("name") or component.get("id") or "?")


def vip_ports(component: dict[str, Any]) -> tuple[list[PortSpec], list[str]]:
    """The ports a load_balanced component gets a VIP on, and the errors.

    A range is refused: a virtual server listens on one port, and a VIP per
    port of a range is not what a range means. A port that does not parse is
    reported, never guessed.
    """
    ports: list[PortSpec] = []
    errors: list[str] = []
    for spec in component.get("ports") or []:
        try:
            port = PortProfileHelper.parse_port_spec(spec)
        except ValueError as exc:
            errors.append(f"{component_ref(component)}: {exc}")
            continue
        if port[2] is not None:
            errors.append(f"{component_ref(component)}: port range {spec} cannot be load balanced (one VIP per port)")
            continue
        if port not in ports:
            ports.append(port)
    if not ports and not errors:
        errors.append(f"{component_ref(component)} is load_balanced but lists no port")
    return ports, errors


def serving_contexts(
    component: dict[str, Any],
) -> tuple[dict[str, tuple[dict[str, Any], list[dict[str, Any]]]], list[str]]:
    """The load balancer contexts serving the component's instances, and the errors.

    Keyed by context id, each with the instances it serves (its pool members).
    The context of an instance is the one serving its customer deployment;
    an instance without one (a deployment that has not boarded, a provider
    device, a cloud instance next to on-prem ones) is an error: strict load
    balancing has no direct path to fall back on. A component whose instances
    are all in the cloud never comes here (is_cloud_component).
    """
    contexts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    errors: list[str] = []
    instances = component.get("instances") or []
    if not instances:
        errors.append(f"{component_ref(component)} is load_balanced but has no instances")
    for instance in instances:
        context = ((instance.get("deployment") or {}).get("serving_loadbalancer_context")) or {}
        if not context.get("id"):
            errors.append(
                f"{component_ref(component)}: instance {instance.get('name', instance.get('id', '?'))} is in no deployment "
                "with a serving load balancer context"
            )
            continue
        contexts.setdefault(context["id"], (context, []))[1].append(instance)
    return dict(sorted(contexts.items())), errors


def leg_prefix_id(context: dict[str, Any]) -> str | None:
    """The context's leg /29: the prefix of the addresses on its member sub-interfaces.

    None unless exactly one: the leg is not wired yet, or two prefixes would
    make the R_server source ambiguous.
    """
    prefix_ids = {
        prefix_id
        for interface in context.get("capabilities") or []
        if (prefix_id := (((interface.get("ip_address") or {}).get("ip_prefix")) or {}).get("id"))
    }
    return next(iter(prefix_ids)) if len(prefix_ids) == 1 else None


def vip_range(context: dict[str, Any]) -> dict[str, Any] | None:
    """The context's routed VIP range, or None unless it has an id and a namespace."""
    prefix = context.get("vip_prefix") or {}
    return prefix if prefix.get("id") and (prefix.get("ip_namespace") or {}).get("id") else None


def context_ready(context: dict[str, Any] | None) -> bool:
    """The context has its VIP range and its one leg: the boarding run that builds it has written both."""
    if not context:
        return False
    return vip_range(context) is not None and leg_prefix_id(context) is not None


def member_prefix(segment: dict[str, Any]) -> dict[str, Any] | None:
    """The segment's gateway prefix the pool members are reserved in, or None unless complete."""
    prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
    if segment.get("id") and prefix.get("id") and prefix.get("prefix") and prefix.get("ip_namespace"):
        return prefix
    return None


def vip_identifier(component_id: str, port: PortSpec) -> str:
    """The allocation identifier of a VIP /32: the same address on every run, in every caller's run."""
    protocol, port_start, _port_end = port
    return f"{component_id}-{protocol}-{port_start}-vip"


def member_identifier(instance_id: str, segment_id: str) -> str:
    """The allocation identifier of an instance's pool-member address in its segment."""
    return f"{instance_id}-{segment_id}-lb-member"


def vip_pool_name(prefix_id: str) -> str:
    """The address pool over a context's VIP range (named by the prefix: a moved range is a new pool)."""
    return f"lb-vip-{prefix_id}"


def member_pool_name(prefix_id: str) -> str:
    """The address pool the pool members of a segment's prefix are reserved from."""
    return f"lb-members-{prefix_id}"


def is_cloud_component(component: dict[str, Any]) -> bool:
    """Every instance of the component is a cloud instance.

    Such a component is fronted by a managed cloud load balancer in its
    virtual network: VPC-native and addressed by DNS, so it has no context,
    no VIP range and no leg, and its callers keep their direct rule into its
    segment (no R_client, no R_server).
    """
    instances = component.get("instances") or []
    return bool(instances) and all(instance.get("typename") == CLOUD_INSTANCE_KIND for instance in instances)


def cloud_lb_name(component: dict[str, Any]) -> str:
    """The CloudLoadBalancer of a component: its fqdn as a slug, shortened with a hash of the fqdn when too long.

    Within CLOUD_LB_NAME_MAX, alphanumerics and hyphens; unique per component
    because the fqdn is.
    """
    ref = component_ref(component)
    slug = re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")
    if len(slug) <= CLOUD_LB_NAME_MAX:
        return slug
    digest = hashlib.sha256(ref.encode()).hexdigest()[:8]
    return f"{slug[: CLOUD_LB_NAME_MAX - len(digest) - 1].rstrip('-')}-{digest}"


def cloud_lb_scheme(security_profile: str | None) -> str:
    """internet_facing for an internet_exposed application, else internal."""
    return "internet_facing" if security_profile == "internet_exposed" else "internal"


def cloud_placement(
    component: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """The virtual network and the subnets of a cloud component's load balancer, and the errors.

    The subnets are its instances' (where the targets are), in exactly one
    virtual network; none or several is an error and leaves no load balancer.
    An instance in no subnet is an error of its own.
    """
    fqdn = component_ref(component)
    errors: list[str] = []
    segments: dict[str, dict[str, Any]] = {}
    networks: dict[str, dict[str, Any]] = {}
    for instance in component.get("instances") or []:
        segment = instance.get("network_segment") or {}
        if not segment.get("id"):
            errors.append(
                f"{fqdn}: cloud instance {instance.get('name', instance.get('id', '?'))} is in no network segment"
            )
            continue
        segments[segment["id"]] = segment
        network = segment.get("virtual_network") or {}
        if network.get("id"):
            networks[network["id"]] = network
    if len(networks) != 1:
        if networks:
            names = ", ".join(
                sorted(str(network.get("name") or network_id) for network_id, network in networks.items())
            )
            errors.append(f"{fqdn}: cloud instances span several virtual networks ({names})")
        else:
            errors.append(f"{fqdn}: no virtual network for the cloud load balancer")
        return None, [], errors
    network = next(iter(networks.values()))
    in_network = [segment for segment in segments.values() if (segment.get("virtual_network") or {}).get("id")]
    return network, sorted(in_network, key=lambda item: str(item.get("name") or item["id"])), errors


def cloud_members(component: dict[str, Any], network: dict[str, Any]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(instance name, address, namespace id) of each cloud instance, sorted by name, and the errors.

    The address is the instance's own private_ip, in the namespace of the
    virtual network's CIDR block that holds it; an instance without one, or
    outside every block, is an error.
    """
    fqdn = component_ref(component)
    blocks = [
        (ip_network(block["prefix"], strict=False), namespace_id)
        for block in network.get("cidr_blocks") or []
        if block.get("prefix") and (namespace_id := (block.get("ip_namespace") or {}).get("id"))
    ]
    members: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for instance in sorted(component.get("instances") or [], key=lambda item: str(item.get("name") or item["id"])):
        name = str(instance.get("name") or instance["id"])
        address = instance.get("private_ip")
        if not address:
            errors.append(f"{fqdn}: cloud instance {name} has no private_ip for the pool members")
            continue
        host = ip_interface(address).ip
        namespace_id = next((ns for block, ns in blocks if host in block), None)
        if namespace_id is None:
            errors.append(
                f"{fqdn}: cloud instance {name} private_ip {address} is in no CIDR block of "
                f"{network.get('name', network.get('id'))}"
            )
            continue
        members.append((name, str(address), namespace_id))
    return members, errors
