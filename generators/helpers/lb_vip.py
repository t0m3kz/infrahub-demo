"""Pure planning for the load balancer VIP producer (generators/lb_vip.py).

Inputs are cleaned application.gql data (utils.data_cleaning.clean_data): a
component with ``ports``, ``instances`` (each with ``deployment`` and its
``serving_loadbalancer_context``) and ``network_segment``.
"""

from __future__ import annotations

from typing import Any

from utils.ports import PortProfileHelper, PortSpec

VIP_PREFIXLEN = 32
"""A VIP is one host address of its context's routed VIP range."""


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
    an instance without one (a deployment that has not boarded, a cloud or
    provider device) is an error: strict load balancing has no direct path
    to fall back on.
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
