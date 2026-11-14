"""Validate leaf."""

from functools import partial

from checks.common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans
from transforms.config.leaf import Leaf


class CheckLeaf(BaseDeviceCheck):
    """Check Leaf."""

    query = "leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=Leaf),
    ]
