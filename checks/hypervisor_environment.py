"""Validate that prod and non-prod VMs never share a physical host."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import namespace_type_for_environment


class CheckHypervisorEnvironment(InfrahubCheck):
    """Every VM on one hypervisor is of one environment class.

    The class is the namespace type of the environment of the customer
    footprint a VM is deployed for (``p`` -> prod, every other value ->
    non_prod), the same rule that puts its segments in PROD or NON-PROD. A
    host running both is a shared failure and trust domain between prod and
    non-prod, which the network keeps apart everywhere else. VM placement is
    data (hosting_device), so this check is what holds the rule. A VM with no
    host, or whose deployment is no customer footprint, has no class to
    compare and is skipped.
    """

    query = "hypervisor_environment"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        # host name -> environment class -> "vm (deployment)" labels
        classes_by_host: dict[str, dict[str, list[str]]] = {}
        for vm in cleaned.get("DcimVirtualDevice") or []:
            host = (vm.get("hosting_device") or {}).get("name")
            deployment = vm.get("deployment") or {}
            environment = deployment.get("environment")
            if not host or not environment:
                continue
            label = f"{vm.get('name', vm.get('id', '<unnamed-vm>'))} ({deployment.get('name', '?')})"
            classes = classes_by_host.setdefault(host, {})
            classes.setdefault(namespace_type_for_environment(environment), []).append(label)
        for host, classes in sorted(classes_by_host.items()):
            if len(classes) > 1:
                hosted = "; ".join(f"{env_class}: {sorted(vms)}" for env_class, vms in sorted(classes.items()))
                self.log_error(
                    message=(
                        f"Host '{host}' runs VMs of more than one environment class ({hosted}): "
                        "prod and non-prod never share a hypervisor"
                    )
                )
