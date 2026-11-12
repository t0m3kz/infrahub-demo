"""OSPF configuration helpers for device transforms."""

from typing import Any

from utils.interface_speed import InterfaceSpeedMatcher


def _reference_bandwidth_mbps(interfaces: list[dict[str, Any]]) -> int | None:
    """Derive OSPF auto-cost reference-bandwidth (Mbps) from the fastest fabric interface."""
    speeds_mbps = [
        InterfaceSpeedMatcher.extract_speed(iface.get("interface_type"))
        for iface in interfaces
        if iface.get("interface_type")
    ]
    speeds_mbps = [s for s in speeds_mbps if s]
    return max(speeds_mbps) if speeds_mbps else None


# RoutingOSPFInterface.authentication_mode values each platform's OSPF partial renders.
OSPF_AUTH_MODES: dict[str, frozenset[str]] = {
    "arista_eos": frozenset({"md5", "sha1"}),
    "cisco_nxos": frozenset({"md5"}),
    "cisco_ios": frozenset({"md5"}),
    "cisco_xe": frozenset({"md5"}),
}


def refuse_unrenderable_ospf_auth(platform_name: str, interfaces: list[dict[str, Any]]) -> None:
    """Raise when an OSPF interface asks for an authentication mode its platform's template cannot render."""
    supported = OSPF_AUTH_MODES.get(platform_name, frozenset())
    unsupported = sorted(
        f"{iface['name']} ({ospf['authentication_mode']})"
        for iface in interfaces
        if (ospf := iface.get("ospf"))
        and ospf.get("password")
        and ospf.get("authentication_mode")
        and ospf["authentication_mode"] not in supported
    )
    if unsupported:
        raise ValueError(
            f"{platform_name} cannot render the OSPF authentication mode of {', '.join(unsupported)}; "
            "refusing a weaker-authenticated config"
        )


def get_ospf(
    device_capabilities: list[dict[str, Any]], interfaces: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Extract OSPF configuration information."""
    ospf_services = [svc for svc in device_capabilities if svc.get("typename") == "ManagedOSPF"]
    if not ospf_services:
        return []

    derived_bandwidth = _reference_bandwidth_mbps(interfaces or [])

    ospf_configs = [
        {
            "process_id": service["process_id"],
            "router_id": service["router_id"]["address"],
            "reference_bandwidth": derived_bandwidth or service["reference_bandwidth"],
        }
        for service in ospf_services
    ]

    ospf_configs.sort(key=lambda c: c["process_id"])
    return ospf_configs
