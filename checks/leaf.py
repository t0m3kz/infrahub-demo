"""Validate leaf."""

from functools import partial

from transforms.config.leaf import Leaf

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans


class CheckLeaf(BaseDeviceCheck):
    """Check Leaf."""

    query = "leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=Leaf),
    ]
