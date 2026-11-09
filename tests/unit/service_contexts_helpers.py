"""Builders for the unit tests of utils/service_contexts.py and utils/device_services.py: cleaned members, ports, contexts."""

from __future__ import annotations

from typing import Any

FW = "ManagedFirewallContext"
LB = "ManagedLoadbalancerContext"


def make_context(
    context_id: str,
    vlan: int | None,
    kind: str = FW,
    served: tuple[tuple[str, str], ...] = (),
    tenant: str | None = None,
) -> dict[str, Any]:
    """A cleaned service context as listed in a member's ``shared_capabilities`` (served: (footprint id, environment))."""
    served_key = "fw_served" if kind == FW else "lb_served"
    return {
        "typename": kind,
        "id": context_id,
        "name": context_id,
        "vlan_id": vlan,
        "tenant": {"id": tenant, "environment": "p"} if tenant else None,
        served_key: [{"id": fp, "environment": env} for fp, env in served],
    }


def make_iface(
    iface_id: str, name: str, role: str = "service", typename: str = "DcimPhysicalInterface", **extra: Any
) -> dict[str, Any]:
    """A cleaned interface."""
    return {"typename": typename, "id": iface_id, "name": name, "role": role, **extra}


def make_member(
    name: str,
    contexts: list[dict[str, Any]],
    *,
    role: str = "internal-firewall",
    uplinks: tuple[str, ...] = ("ethernet1/1",),
    subs: tuple[str, ...] = (),
    virtual: bool = False,
) -> dict[str, Any]:
    """A cleaned member device with its uplinks and the given sub-interface names."""
    kind = "DcimVirtualInterface" if virtual else "DcimPhysicalInterface"
    interfaces = [make_iface(f"{name}:{up}", up, "uplink", kind) for up in uplinks]
    interfaces += [make_iface(f"{name}:{sub}", sub, "service", "DcimVirtualInterface") for sub in subs]
    return {
        "typename": "DcimVirtualDevice" if virtual else "DcimPhysicalDevice",
        "id": f"dev-{name}",
        "name": name,
        "role": role,
        "shared_capabilities": contexts,
        "interfaces": interfaces,
    }


def make_far(
    device_: dict[str, Any], port: dict[str, Any], *, nics: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The far end of a cable as the query returns it: a physical port, its device and its pinned NICs."""
    device = {k: device_[k] for k in ("id", "name", "role", "shared_capabilities", "interfaces")}
    return {**port, "device": device, "sub_interfaces": nics or []}


def make_switch(ports: dict[str, tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """A border leaf whose service ports are cabled: ``name -> (port role, [endpoint far ends])``."""
    interfaces = []
    for name, (role, far_ends) in ports.items():
        port = make_iface(f"sw:{name}", name, role)
        port["cable"] = {"id": f"cable-{name}", "endpoints": [port | {"device": {"id": "sw"}}, *far_ends]}
        interfaces.append(port)
    return {
        "typename": "DcimPhysicalDevice",
        "id": "sw",
        "name": "sw",
        "role": "border-leaf",
        "service_ports": interfaces,
    }
