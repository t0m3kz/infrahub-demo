"""Validate ToR."""

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


class CheckToR(BaseDeviceCheck):
    """Check ToR."""

    query = "leaf_config"
    validators = [validate_interfaces, validate_routing_password]
