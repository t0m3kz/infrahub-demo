"""Validate access-leaf."""

from functools import partial

from checks.common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans
from transforms.config.access_leaf import AccessLeaf


class CheckAccessLeaf(BaseDeviceCheck):
    """Check Access Leaf."""

    query = "leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=AccessLeaf),
    ]
