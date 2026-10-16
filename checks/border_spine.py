"""Validate border spine."""

from functools import partial

from transforms.config.border_spine import BorderSpine

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans


class CheckBorderSpine(BaseDeviceCheck):
    """Check Border Spine."""

    query = "border_spine_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=BorderSpine),
    ]
