"""Validate where a VM runs: its host's environment class and its host's DC."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, namespace_type_for_environment

from .common import deployment_scope_ids

OFFICE_KIND = "TopologyCustomerOffice"
"""An office has no environment: it is always PROD."""

HOSTING_FOOTPRINT_KINDS = frozenset({"TopologyCustomerDC", "TopologyCustomerColocation"})
"""Footprints whose parent is the DC or metro the VM's host must sit in."""


def _environment_class(deployment: dict[str, Any]) -> str | None:
    """Namespace type of the footprint a VM is deployed for, or None when it has no environment class.

    A DC or Colocation footprint states an environment, an office is always PROD; any other deployment (a cloud
    footprint, a pod) has none.
    """
    if deployment.get("typename") == OFFICE_KIND:
        return PROD_TYPE
    environment = deployment.get("environment")
    return namespace_type_for_environment(environment) if environment else None


class CheckHypervisorEnvironment(InfrahubCheck):
    """A VM sits on a host of its own environment class, in its footprint's DC.

    Environment class: every VM on one hypervisor is of one class. The class
    is the namespace type of the environment of the customer footprint a VM is
    deployed for (``p`` -> prod, every other value -> non_prod, an office ->
    prod), the same rule that puts its segments in PROD or NON-PROD. A host
    running both is a shared failure and trust domain between prod and
    non-prod, which the network keeps apart everywhere else.

    Placement: the host's DC (a pod's parent, or the deployment itself) or
    metro (a zone's parent) must be the parent of the VM's DC or Colocation
    footprint. A VM running on a host of another DC than the one its customer
    was boarded in is served by another fabric than the footprint's segments.

    VM placement is data (hosting_device), so this check is what holds the
    rules. A VM with no host, or whose deployment is no customer footprint, has
    no class to compare and is skipped; a host not yet in a deployment
    (add_endpoint sets it when it cables the host) has no DC to compare.
    """

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
