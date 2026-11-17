"""Validate border spine."""

from functools import partial

from checks.common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans
from transforms.config.border_spine import BorderSpine


class CheckBorderSpine(BaseDeviceCheck):
    """Check Border Spine."""

    query = "border_spine_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=BorderSpine),
    ]
