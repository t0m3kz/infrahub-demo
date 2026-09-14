"""Validate border leaf."""

from .common import BaseDeviceCheck, validate_exchange_gateways, validate_interfaces, validate_routing_password


class CheckBorderLeaf(BaseDeviceCheck):
    """Check Border Leaf."""

    query = "border_leaf_config"
    validators = [validate_interfaces, validate_routing_password, validate_exchange_gateways]
