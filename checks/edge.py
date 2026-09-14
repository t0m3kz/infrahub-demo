"""Validate edge."""

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


class CheckEdge(BaseDeviceCheck):
    """Check Edge."""

    query = "edge_config"
    validators = [validate_interfaces, validate_routing_password]
