"""Conventions and the firewall/load-balancer step shared by the fabric generators
that own a DC-scoped or pod-scoped service chain (dc.py, pod.py)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ..pod_config import templates_by_role
from ..types import NamingConvention

# The generator definitions (.infrahub.yml) that write a DC's own data — the ones
# a consumer of DC-level data (a pod, a customer boarding) waits out.
DC_GENERATORS = ("add_dc", "dc_pod_cascade")

_SERVICE_ROLES: tuple[Literal["firewall", "load-balancer"], ...] = ("firewall", "load-balancer")


def p2p_prefix_length(ipv6: bool) -> int:
    """P2P link prefix length of the fabric's underlay address family."""
    return 127 if ipv6 else 31


def loopback_prefix_length(ipv6: bool) -> int:
    """Loopback host-route prefix length of the fabric's underlay address family."""
    return 128 if ipv6 else 32


class ServiceDevicesMixin:
    """Firewall + load-balancer creation for a fabric_templates list."""

    # Provided by DeviceMixin.
    create_ha_role_devices: Callable[..., Awaitable[list[tuple[dict[str, Any], list[str]]]]]

    async def create_firewall_and_load_balancer_devices(
        self,
        *,
        fabric_templates: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: NamingConvention,
        indexes: list[int],
    ) -> tuple[list[str], list[str]]:
        """Create the HA-paired firewall and load-balancer devices a fabric_templates
        list declares (DeviceMixin.create_ha_role_devices, one call per role).

        Returns the created (firewall names, load-balancer names), flattened over
        the template entries.
        """
        names_by_role: dict[str, list[str]] = {}
        for role in _SERVICE_ROLES:
            results = await self.create_ha_role_devices(
                role=role,
                entries=templates_by_role(fabric_templates, role),
                deployment_id=deployment_id,
                naming_convention=naming_convention,
                indexes=indexes,
            )
            names_by_role[role] = [name for _, names in results for name in names]
        return names_by_role["firewall"], names_by_role["load-balancer"]
