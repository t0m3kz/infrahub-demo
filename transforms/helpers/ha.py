"""HA (High Availability) configuration helpers for device transforms.

Mirrors the MLAG helper pattern (transforms/helpers/mlag.py) but for
ManagedFirewallHA / ManagedLoadbalancerHA / ManagedCloudFirewallHA capabilities
instead of ManagedMLAG.
"""

from ipaddress import ip_interface
from typing import Any

from utils.exchange_transit import OFFSET_MEMBER_B, OFFSET_VIP

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
    for the first member by name, "secondary" for the other — the failover
    unit ASA and the HA link addresses (HA_LINK_ADDRESSES) are keyed by.
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
        if device_name in members:
            unit = "primary" if members.index(device_name) == 0 else "secondary"
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
            "link_addresses": HA_LINK_ADDRESSES if ha_link else None,
        }
    return None


def inline_addresses(
    capabilities: list[dict[str, Any]] | None,
    ha: dict[str, Any] | None,
    leg_address: str | None = None,
) -> dict[str, str | None]:
    """The HA pair's addresses on an interface.

    Two sources, one result shape:

    - an interface carrying a segment the pair terminates inline
      (terminate_inline, inline_service == this pair): virtual_ip is the
      segment gateway — the address hosts use, owned by the active member
      (floating IP / VIP / VRRP address, per vendor) — and standby_ip the
      secondary member's OWN address on that segment, which the ASA
      `standby` keyword needs in both members' config.
    - an exchange transit leg (``leg_address`` is the member's own address in
      the leg's /29, see utils/exchange_transit.py): virtual_ip is the /29's
      firewall VIP, standby_ip member B's address, fixed offsets that need no
      lookup of the peer.

    The interface's own ip_address is this member's own address. Both are
    None when the interface carries no such segment/leg, or the device has no HA.
    """
    none: dict[str, str | None] = {"virtual_ip": None, "standby_ip": None}
    if not ha:
        return none
    if leg_address:
        network = ip_interface(leg_address).network
        if network.version != 4:
            return none
        return {
            "virtual_ip": f"{network[OFFSET_VIP]}/{network.prefixlen}",
            "standby_ip": f"{network[OFFSET_MEMBER_B]}/{network.prefixlen}",
        }
    secondary = ha["members"][1] if len(ha.get("members") or []) > 1 else None
    for cap in capabilities or []:
        if not cap.get("terminate_inline") or (cap.get("inline_service") or {}).get("name") != ha.get("name"):
            continue
        virtual_ip = (cap.get("gateway") or {}).get("address")
        if not virtual_ip:
            continue
        standby_ip = next(
            (
                address
                for iface in cap.get("interface_capabilities") or []
                if (iface.get("device") or {}).get("name") == secondary
                and (address := (iface.get("ip_address") or {}).get("address"))
            ),
            None,
        )
        return {"virtual_ip": virtual_ip, "standby_ip": standby_ip}
    return none
