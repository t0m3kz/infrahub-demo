"""HA (High Availability) configuration helpers for device transforms.

Mirrors the MLAG helper pattern (transforms/helpers/mlag.py) but for
ManagedFirewallHA / ManagedLoadbalancerHA / ManagedCloudFirewallHA capabilities
instead of ManagedMLAG.
"""

from ipaddress import ip_interface
from typing import Any

from transforms.helpers.addressing import host_ip
from utils.limits import OFFSET_VIP

# The HA sync link is a back-to-back cable between the two members, never
# routed anywhere else, so its addresses are a fixed link-local /30 per pair
# rather than IPAM-allocated: the primary (first member by name) takes .1.
HA_LINK_ADDRESSES = {"primary": "169.254.255.1/30", "secondary": "169.254.255.2/30"}

_HA_TYPENAMES = (
    "ManagedFirewallHA",
    "ManagedCloudFirewallHA",
    "ManagedLoadbalancerHA",
    "ManagedProxyHA",
    "ManagedCloudProxy",
)


def get_ha(
    device_capabilities: list[dict[str, Any]] | None,
    interfaces: list[dict[str, Any]] | None = None,
    device_name: str | None = None,
) -> dict[str, Any] | None:
    """Extract HA domain configuration for template rendering.

    Scans device_capabilities for a ManagedInlineService entry (ManagedFirewallHA,
    ManagedLoadbalancerHA, or ManagedCloudFirewallHA).
    Both peer devices reference the same HA node via device_capabilities, so
    this function returns the first matching capability found, or None if the
    device has no HA domain.

    Mirrors get_mlag() from transforms/helpers/mlag.py but for HA.
    Key difference: the peer device list comes from cap.get("capabilities")
    (the inbound relationship name on the HA node), not cap.get("devices").

    If interfaces are provided, the HA link interface (role == 'ha')
    is identified and included as ha_link in the result.

    With device_name, ``unit`` is this device's role in the pair — "primary"
    for the first member by name, "secondary" for the other — the unit the
    HA link addresses (HA_LINK_ADDRESSES) and the keepalived state are keyed by,
    and ``peer_management_ip`` the other member's management address (its
    primary_address, where the query selects it: the NetScaler HA node).
    """
    for cap in device_capabilities or []:
        if cap.get("typename") not in _HA_TYPENAMES:
            continue
        ha_link = None
        for iface in interfaces or []:
            if iface.get("role") == "ha":
                ha_link = iface.get("name")
                break
        members = sorted(d.get("name") for d in (cap.get("capabilities") or []) if d.get("name"))
        unit = None
        peer_management_ip = None
        if device_name in members:
            unit = "primary" if members.index(device_name) == 0 else "secondary"
            peer = next((d for d in cap.get("capabilities") or [] if d.get("name") not in (None, device_name)), {})
            peer_management_ip = host_ip((peer.get("primary_address") or {}).get("address"))
        return {
            "name": cap.get("name"),
            "group_id": cap.get("group_id"),
            "mode": cap.get("mode", "active-passive"),
            "priority": cap.get("priority", 100),
            "preempt": cap.get("preempt", False),
            "ha_timer": cap.get("ha_timer", "standard"),
            "ha_protocol": cap.get("ha_protocol"),
            "devices": [d.get("name") for d in (cap.get("capabilities") or [])],
            "ha_link": ha_link,
            "members": members,
            "unit": unit,
            "peer_management_ip": peer_management_ip,
            "link_addresses": HA_LINK_ADDRESSES if ha_link else None,
        }
    return None


def inline_addresses(
    capabilities: list[dict[str, Any]] | None,
    ha: dict[str, Any] | None,
    leg_address: str | None = None,
) -> dict[str, str | None]:
    """The HA pair's virtual IP on an interface.

    Two sources, one result shape:

    - an interface carrying a segment the pair terminates inline
      (terminate_inline, inline_service == this pair): virtual_ip is the
      segment gateway — the address hosts use, owned by the active member
      (floating IP / VIP / VRRP address, per vendor).
    - an exchange transit leg (``leg_address`` is the member's own address in
      the leg's /29, see utils/exchange_transit.py): virtual_ip is the /29's
      firewall VIP, a fixed offset that needs no lookup of the peer.

    The interface's own ip_address is this member's own address. virtual_ip is
    None when the interface carries no such segment/leg, or the device has no HA.
    """
    none: dict[str, str | None] = {"virtual_ip": None}
    if not ha:
        return none
    if leg_address:
        network = ip_interface(leg_address).network
        if network.version != 4:
            return none
        return {"virtual_ip": f"{network[OFFSET_VIP]}/{network.prefixlen}"}
    for cap in capabilities or []:
        if not cap.get("terminate_inline") or (cap.get("inline_service") or {}).get("name") != ha.get("name"):
            continue
        virtual_ip = (cap.get("gateway") or {}).get("address")
        if not virtual_ip:
            continue
        return {"virtual_ip": virtual_ip}
    return none
