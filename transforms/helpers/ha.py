"""HA (High Availability) configuration helpers for device transforms.

Mirrors the MLAG helper pattern (transforms/helpers/mlag.py) but for
ManagedFirewallHA / ManagedLoadbalancerHA / ManagedCloudFirewallHA capabilities
instead of ManagedMLAG.
"""

from typing import Any

_HA_TYPENAMES = (
    "ManagedFirewallHA",
    "ManagedCloudFirewallHA",
    "ManagedLoadbalancerHA",
    "ManagedProxyHA",
    "ManagedCloudProxyHA",
)


def get_ha(
    device_capabilities: list[dict[str, Any]] | None, interfaces: list[dict[str, Any]] | None = None
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
    """
    for cap in device_capabilities or []:
        if cap.get("typename") not in _HA_TYPENAMES:
            continue
        ha_link = None
        for iface in interfaces or []:
            if iface.get("role") == "ha":
                ha_link = iface.get("name")
                break
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
        }
    return None
