"""Validate what a firewall pair is set up with: its exchange legs, SNAT addresses and context members."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from transforms.config.firewall import Firewall

from .common import validate_context_members, validate_exchange_gateways, validate_snat_addresses


class CheckFirewall(InfrahubCheck):
    """Validate the infrastructure of a firewall: its exchange legs (checks/common.py validate_exchange_gateways),
    SNAT addresses (validate_snat_addresses) and the members of its contexts (validate_context_members: exactly its
    HA pair). The policy rules it enforces are judged once, for every firewall and switch alike, by
    ``checks/segment_security.py`` (``_validate_policy_rules``): a rule is valid or not wherever it is rendered, so
    this check no longer reads them."""

    query = "firewall_config"

    def validate(self, data: Any) -> None:
        device, _, _ = Firewall._device_and_platform(data)
        for error in [
            *validate_exchange_gateways(device),
            *validate_snat_addresses(device),
            *validate_context_members(device),
        ]:
            self.log_error(message=error)
