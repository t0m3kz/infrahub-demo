"""MLAG configuration helpers for device transforms."""

from ipaddress import IPv4Address
from typing import Any

from transforms.helpers.addressing import host_ip

# lacp_mode -> the `channel-group ... mode` keyword of EOS and NX-OS (a static LAG has no LACP: `on`).
CHANNEL_GROUP_MODE = {"active": "active", "passive": "passive", "static": "on"}


def _on_device(interfaces: list[dict[str, Any]], device_name: str) -> list[dict[str, Any]]:
    """The domain's listed interfaces that sit on ``device_name``."""
    return [interface for interface in interfaces if (interface.get("device") or {}).get("name") == device_name]


def _peer_link_rank(interface: dict[str, Any]) -> tuple[int, str]:
    """Sort key of the peer-link candidates among a member's listed ``mlag-peer`` interfaces."""
    if interface.get("lag_id") is not None:
        rank = 0
    elif interface.get("typename") == "DcimVirtualInterface":
        rank = 1
    else:
        rank = 2
    return rank, str(interface.get("name") or "")


def _control_address(interfaces: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Return (interface name, address with prefix length) of one member's MLAG control session."""
    candidates = [
        interface
        for interface in interfaces
        if interface.get("role") in {"mlag-control", "mlag-peer"} and (interface.get("ip_address") or {}).get("address")
    ]
    for interface in sorted(candidates, key=lambda interface: interface.get("role") != "mlag-control"):
        return interface.get("name"), interface["ip_address"]["address"]
    return None, None


def _management_ip(member: dict[str, Any]) -> str | None:
    return host_ip((member.get("primary_address") or {}).get("address"))


def get_mlag(
    device_capabilities: list[dict[str, Any]] | None,
    interfaces: list[dict[str, Any]] | None = None,
    device_name: str = "",
) -> dict[str, Any] | None:
    """Extract MLAG domain configuration for template rendering."""
    for cap in device_capabilities or []:
        if cap.get("typename") != "ManagedMLAG":
            continue
        peer_link = None
        virtual_peer_link = False
        peer_link_lag_id = None
        peer_link_lacp_mode = None
        peer_link_channel_group_mode = None
        peer_link_members: list[str] = []
        domain_interfaces = cap.get("interfaces") or []
        own_interfaces = _on_device(domain_interfaces, device_name)
        peer_interfaces = [iface for iface in own_interfaces if iface.get("role") == "mlag-peer"]
        for iface in sorted(peer_interfaces, key=_peer_link_rank):
            iface_name = iface.get("name")
            lag_id = iface.get("lag_id")
            virtual_peer_link = iface.get("typename") == "DcimVirtualInterface"
            if lag_id is not None:
                peer_link = iface_name
                peer_link_lag_id = lag_id
                peer_link_lacp_mode = iface.get("lacp_mode", "active")
                peer_link_channel_group_mode = CHANNEL_GROUP_MODE.get(peer_link_lacp_mode, "active")
                peer_link_members = [m.get("name") for m in (iface.get("member_interfaces") or []) if m.get("name")]
            else:
                peer_link = iface_name
            break
        members = cap.get("devices") or cap.get("capabilities") or []
        own = next((member for member in members if member.get("name") == device_name), None)
        peer = next((member for member in members if member.get("name") != device_name), None) if own else None
        control_interface, local_address = _control_address(own_interfaces)
        _, peer_address = _control_address(_on_device(domain_interfaces, peer["name"])) if peer else (None, None)
        listed_names = {interface.get("name") for interface in own_interfaces}
        mclag_interfaces = sorted(
            (
                {
                    "name": interface["name"],
                    "members": [member["name"] for member in interface.get("member_interfaces") or []],
                }
                for interface in interfaces or []
                if interface.get("lag_id") is not None
                and interface.get("name") not in listed_names
                and (interface.get("mlag_domain") or {}).get("id") == cap.get("id")
            ),
            key=lambda interface: interface["name"],
        )
        return {
            "name": cap.get("name"),
            "domain_id": cap.get("domain_id"),
            "reload_delay": cap.get("reload_delay", 300),
            "reload_delay_non_mlag": cap.get("reload_delay_non_mlag", 330),
            "devices": [member.get("name") for member in members],
            "control_interface": control_interface,
            "local_address": local_address,
            "local_ip": host_ip(local_address),
            "peer_ip": host_ip(peer_address),
            "local_management_ip": _management_ip(own) if own else None,
            "peer_management_ip": _management_ip(peer) if peer else None,
            "peer_link": peer_link,
            "virtual_peer_link": virtual_peer_link,
            "peer_link_lag_id": peer_link_lag_id,
            "peer_link_lacp_mode": peer_link_lacp_mode,
            "peer_link_channel_group_mode": peer_link_channel_group_mode,
            "peer_link_members": peer_link_members,
            "mclag_interfaces": mclag_interfaces,
        }
    return None


def get_sonic_mlag_config(mlag: dict[str, Any] | None) -> dict[str, Any]:
    """Build the SONiC ConfigDB tables for an MC-LAG domain."""
    if not mlag:
        return {}
    if not all(mlag.get(field) for field in ("domain_id", "local_address", "peer_ip", "peer_link_lag_id")):
        raise ValueError(
            "SONiC MC-LAG requires a domain, an IPv4 MLAG control address on both peers, and a physical peer-link LAG"
        )
    if not 1 <= mlag["domain_id"] <= 4095:
        raise ValueError("SONiC MC-LAG domain ID must be between 1 and 4095")
    for endpoint in ("local_ip", "peer_ip"):
        IPv4Address(mlag[endpoint])
    if mlag["local_ip"] == mlag["peer_ip"]:
        raise ValueError("SONiC MC-LAG peers must use distinct control IPs")
    peer_link = mlag["peer_link"]
    control_interface = mlag.get("control_interface") or "Vlan4094"
    control_vlan_id = control_interface.removeprefix("Vlan")
    domain_id = str(mlag["domain_id"])
    portchannels = {peer_link: {"admin_status": "up"}}
    portchannel_members = {f"{peer_link}|{member}": {} for member in mlag["peer_link_members"]}
    for interface in mlag["mclag_interfaces"]:
        portchannels[interface["name"]] = {"admin_status": "up"}
        portchannel_members.update({f"{interface['name']}|{member}": {} for member in interface["members"]})
    return {
        "MCLAG_DOMAIN": {
            domain_id: {
                "source_ip": mlag["local_ip"],
                "peer_ip": mlag["peer_ip"],
                "peer_link": peer_link,
            }
        },
        "MCLAG_INTERFACE": {
            f"{domain_id}|{interface['name']}": {"if_type": "PortChannel"} for interface in mlag["mclag_interfaces"]
        },
        "PORTCHANNEL": portchannels,
        "PORTCHANNEL_MEMBER": portchannel_members,
        "VLAN": {control_interface: {"vlanid": control_vlan_id, "admin_status": "up"}},
        "VLAN_MEMBER": {f"{control_interface}|{peer_link}": {"tagging_mode": "tagged"}},
        "VLAN_INTERFACE": {control_interface: {}, f"{control_interface}|{mlag['local_address']}": {}},
    }
