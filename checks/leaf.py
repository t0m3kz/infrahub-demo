"""Validate leaf."""

from .common import BaseDeviceCheck, validate_exchange_gateways, validate_interfaces, validate_routing_password


class CheckLeaf(BaseDeviceCheck):
    """Check Leaf."""

    query = "leaf_config"
    validators = [validate_interfaces, validate_routing_password, validate_exchange_gateways]
