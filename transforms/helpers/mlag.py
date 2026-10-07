"""MLAG configuration helpers for device transforms."""

from ipaddress import IPv4Address
from typing import Any

from transforms.helpers.addressing import host_ip


def _control_address(member: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (interface name, address with prefix length) of the member's
    MLAG control session: the role=mlag-control peer-link SVI, or the
    virtual peer-link loopback (role=mlag-peer) when the domain has no
    physical peer-link. (None, None) for vPC, which has no control session."""
    candidates = [
        interface
        for interface in member.get("interfaces") or []
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
    """Extract MLAG domain configuration for template rendering.

    Scans device_capabilities for a ManagedMLAG entry. Both peer devices
    reference the same ManagedMLAG node via device_capabilities, so this
    function returns the first ManagedMLAG capability found, or None if
    the device has no MLAG domain.

    If interfaces are provided, the peer-link interface (role == 'mlag-peer')
    is identified and included as peer_link in the result. The peer-link name
    is taken directly from the database — the generator creates it with the
    vendor-correct naming convention (Port-Channel100, port-channel100,
    lag-100, PortChannel100, peer-link, etc.).
    """
    for cap in device_capabilities or []:
        if cap.get("typename") != "ManagedMLAG":
            continue
        peer_link = None
        peer_link_lag_id = None
        peer_link_lacp_mode = None
        peer_link_members: list[str] = []
        peer_interfaces = [iface for iface in interfaces or [] if iface.get("role") == "mlag-peer"]
        for iface in sorted(peer_interfaces, key=lambda iface: iface.get("lag_id") is None):
            iface_name = iface.get("name")
            lag_id = iface.get("lag_id")
            if lag_id is not None:
                peer_link = iface_name
                peer_link_lag_id = lag_id
                peer_link_lacp_mode = iface.get("lacp_mode", "active")
                peer_link_members = [m.get("name") for m in (iface.get("member_interfaces") or []) if m.get("name")]
            else:
                peer_link = iface_name
            break
        members = cap.get("devices") or cap.get("capabilities") or []
        own = next((member for member in members if member.get("name") == device_name), None)
        peer = next((member for member in members if member.get("name") != device_name), None) if own else None
        control_interface, local_address = _control_address(own) if own else (None, None)
        _, peer_address = _control_address(peer) if peer else (None, None)
        mclag_interfaces = sorted(
            (
                {
                    "name": interface["name"],
                    "members": [member["name"] for member in interface.get("member_interfaces") or []],
                }
                for interface in interfaces or []
                if interface.get("lag_id") is not None
                and interface.get("role") != "mlag-peer"
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
            "peer_link_lag_id": peer_link_lag_id,
            "peer_link_lacp_mode": peer_link_lacp_mode,
            "peer_link_members": peer_link_members,
            "mclag_interfaces": mclag_interfaces,
        }
    return None


def get_sonic_mlag_config(mlag: dict[str, Any] | None) -> dict[str, Any]:
    """Build the SONiC ConfigDB tables for an MC-LAG domain. The ICCP session
    runs between the peer-link SVI's IPv4 /31 addresses (MLAG-Control-IPv4
    pool), so the SVI's VLAN, tagged peer-link membership and address are
    emitted alongside MCLAG_DOMAIN. Templates must merge these per table —
    a shallow update would drop VLANs defined elsewhere in the device config."""
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
