"""Border-leaf/border-spine firewall/load-balancer provisioning mixin for CommonGenerator.

Shared by dc.py (DC-wide border-leaf) and pod.py (a border-spine pod's own
firewall/load-balancer) — both call these with their own scope's
deployment_id/naming_convention/indexes rather than duplicating the logic.
"""

from __future__ import annotations

from typing import Any, Literal

from .types import DeviceOptions

# device_role -> HA node kind. HA pairing itself happens inside
# DeviceMixin.create_devices() (see DeviceOptions.ha_kind) — this mixin only
# needs to pick the right kind per role.
_HA_KIND_BY_ROLE: dict[str, str] = {
    "firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}


class BorderServicesMixin:
    """Mixin providing role-device provisioning for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``, and
    ``create_devices`` (all present on ``CommonGenerator``).
    """

    # CommonGenerator.create_devices (DeviceMixin) — annotation only, no method body.
    create_devices: Any

    async def _create_role_devices(
        self,
        *,
        role: Literal["firewall", "load-balancer"],
        entries: list[Any],
        deployment_id: str,
        naming_convention: Literal["standard", "hierarchical", "flat", "computed"],
        indexes: list[int],
    ) -> list[str]:
        """Create firewall/load-balancer devices for one deployment scope
        (DC-wide from dc.py, or one pod from pod.py). Each entry's devices are
        paired into an HA domain two-at-a-time by create_devices() itself (any
        quantity, not just 2 — an odd device is left unpaired). No loopback
        allocation — not part of underlay/overlay routing.

        Each entry is a fabric_templates row — a plain ``clean_data()`` dict
        (``{"quantity": ..., "template": {...}}``)."""
        device_options = DeviceOptions(indexes=indexes, ha_kind=_HA_KIND_BY_ROLE[role])
        if role == "load-balancer":
            # create_devices()'s default group_name is f"{device_role}s" = "load-balancers",
            # but the bootstrap group is named "loadbalancers" (no hyphen) — override.
            device_options["group_name"] = "loadbalancers"

        all_names: list[str] = []
        for entry in entries:
            names = await self.create_devices(
                deployment_id=deployment_id,
                device_role=role,
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_convention,
                options=device_options,
            )
            all_names.extend(names)

        return all_names
