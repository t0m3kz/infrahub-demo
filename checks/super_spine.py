"""Validate super spine."""

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


class CheckSuperSpine(BaseDeviceCheck):
    """Check Super Spine."""

    query = "super_spine_config"
    validators = [validate_interfaces, validate_routing_password]
