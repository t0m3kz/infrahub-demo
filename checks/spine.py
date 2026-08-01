"""Validate spine."""

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


class CheckSpine(BaseDeviceCheck):
    """Check Spine."""

    query = "spine_config"
    validators = [validate_interfaces, validate_routing_password]
