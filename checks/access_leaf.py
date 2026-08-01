"""Validate access-leaf."""

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


class CheckAccessLeaf(BaseDeviceCheck):
    """Check Access Leaf."""

    query = "leaf_config"
    validators = [validate_interfaces, validate_routing_password]
