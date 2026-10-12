"""Validate border leaf."""

from functools import partial

from transforms.config.border_leaf import BorderLeaf

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans


class CheckBorderLeaf(BaseDeviceCheck):
    """Check Border Leaf."""

    query = "border_leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=BorderLeaf),
    ]
