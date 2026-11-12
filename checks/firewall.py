"""Validate what a firewall pair is set up with: its exchange legs, SNAT addresses and context members."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from transforms.config.firewall import Firewall

from .common import validate_context_members, validate_exchange_gateways, validate_snat_addresses


class CheckFirewall(InfrahubCheck):
    """Validate a firewall pair's exchange legs, SNAT addresses and context members (policy rules: segment_security)."""

    query = "firewall_config"

    def validate(self, data: Any) -> None:
        device, _, _ = Firewall._device_and_platform(data)
        for error in [
            *validate_exchange_gateways(device),
            *validate_snat_addresses(device),
            *validate_context_members(device),
        ]:
            self.log_error(message=error)
