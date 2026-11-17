"""Validate where a VM runs: its host's environment class and its host's DC."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from checks.common import deployment_scope_ids
from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, namespace_type_for_environment

OFFICE_KIND = "TopologyCustomerOffice"
"""An office has no environment: it is always PROD."""

HOSTING_FOOTPRINT_KINDS = frozenset({"TopologyCustomerDC", "TopologyCustomerColocation"})
"""Footprints whose parent is the DC or metro the VM's host must sit in."""


def _environment_class(deployment: dict[str, Any]) -> str | None:
    """Namespace type of the footprint a VM is deployed for, or None (an office is PROD; a cloud footprint has none)."""
    if deployment.get("typename") == OFFICE_KIND:
        return PROD_TYPE
    environment = deployment.get("environment")
    return namespace_type_for_environment(environment) if environment else None


class CheckHypervisorEnvironment(InfrahubCheck):
    """A VM sits on a host of its own environment class (prod or non_prod), in its footprint's DC."""

    query = "hypervisor_environment"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        # host name -> environment class -> "vm (deployment)" labels
        classes_by_host: dict[str, dict[str, list[str]]] = {}
        for vm in cleaned.get("DcimVirtualDevice") or []:
            host = vm.get("hosting_device") or {}
            deployment = vm.get("deployment") or {}
            if not host.get("name"):
                continue
            label = f"{vm.get('name', vm.get('id', '<unnamed-vm>'))} ({deployment.get('name', '?')})"
            env_class = _environment_class(deployment)
            if env_class:
                classes_by_host.setdefault(host["name"], {}).setdefault(env_class, []).append(label)
            self._validate_host_dc(vm, host, deployment)
        for host, classes in sorted(classes_by_host.items()):
            if len(classes) > 1:
                hosted = "; ".join(f"{env_class}: {sorted(vms)}" for env_class, vms in sorted(classes.items()))
                self.log_error(
                    message=(
                        f"Host '{host}' runs VMs of more than one environment class ({hosted}): "
                        "prod and non-prod never share a hypervisor"
                    )
                )

    def _validate_host_dc(self, vm: dict[str, Any], host: dict[str, Any], deployment: dict[str, Any]) -> None:
        """The VM's host must be in the DC or metro its footprint is in."""
        footprint_parent = deployment.get("parent") or {}
        host_deployment = host.get("deployment") or {}
        if deployment.get("typename") not in HOSTING_FOOTPRINT_KINDS or not footprint_parent.get("id"):
            return
        host_scope = deployment_scope_ids(host_deployment)
        if not host_scope or footprint_parent["id"] in host_scope:
            return
        host_parent = host_deployment.get("parent") or {}
        self.log_error(
            message=(
                f"VM '{vm.get('name', vm.get('id', '<unnamed-vm>'))}' is deployed for footprint "
                f"'{deployment.get('name', '?')}' in '{footprint_parent.get('name', '?')}' but runs on host "
                f"'{host.get('name')}' in '{host_parent.get('name') or host_deployment.get('name', '?')}': "
                "a VM runs in the DC of its customer footprint"
            )
        )
