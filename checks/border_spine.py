"""Validate border spine."""

from .common import BaseDeviceCheck, validate_exchange_gateways, validate_interfaces, validate_routing_password


class CheckBorderSpine(BaseDeviceCheck):
    """Check Border Spine."""

    query = "border_spine_config"
    validators = [validate_interfaces, validate_routing_password, validate_exchange_gateways]
