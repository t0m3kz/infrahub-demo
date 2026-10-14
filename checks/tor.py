"""Validate ToR."""

from functools import partial

from transforms.config.tor import ToR

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans


class CheckToR(BaseDeviceCheck):
    """Check ToR."""

    query = "leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=ToR),
    ]
