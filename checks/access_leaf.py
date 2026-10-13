"""Validate access-leaf."""

from functools import partial

from transforms.config.access_leaf import AccessLeaf

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans


class CheckAccessLeaf(BaseDeviceCheck):
    """Check Access Leaf."""

    query = "leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=AccessLeaf),
    ]
