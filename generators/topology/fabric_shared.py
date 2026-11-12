"""Conventions and the firewall/load-balancer step shared by dc.py and pod.py."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE

from ..pod_config import templates_by_role
from ..types import NamingConvention

if TYPE_CHECKING:
    from ..devices import DeviceMixin

# The generator definitions that write a DC's own data; consumers (pod, customer boarding) wait them out.
DC_GENERATORS = ("add_dc", "dc_pod_cascade")

_SERVICE_ROLES: tuple[Literal["internal-firewall", "load-balancer"], ...] = ("internal-firewall", "load-balancer")


class ServiceDevicesMixin:
    """Firewall + load-balancer creation for a fabric_templates list."""

    # Provided by DeviceMixin.
    if TYPE_CHECKING:
        create_ha_role_devices = DeviceMixin.create_ha_role_devices

    async def create_firewall_and_load_balancer_devices(
        self,
        *,
        fabric_templates: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: NamingConvention,
        indexes: list[int],
    ) -> tuple[list[str], list[str]]:
        """Create the HA-paired firewall and load-balancer devices a fabric_templates list declares; returns (firewall
        names, load-balancer names).
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
        return names_by_role["internal-firewall"], names_by_role["load-balancer"]

    async def create_external_firewall_devices(
        self,
        *,
        fabric_templates: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: NamingConvention,
        indexes: list[int],
    ) -> list[str]:
        """Create the HA-paired external-firewall devices (the Internet edge pair) a fabric_templates list declares;
        returns the names.
        """
        results = await self.create_ha_role_devices(
            role=EXTERNAL_FIREWALL_ROLE,
            entries=templates_by_role(fabric_templates, EXTERNAL_FIREWALL_ROLE),
            deployment_id=deployment_id,
            naming_convention=naming_convention,
            indexes=indexes,
        )
        return [name for _, names in results for name in names]
