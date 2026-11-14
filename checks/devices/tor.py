"""Validate ToR."""

from functools import partial

from checks.common import BaseDeviceCheck, validate_interfaces, validate_routing_password, validate_transit_vlans
from transforms.config.tor import ToR


class CheckToR(BaseDeviceCheck):
    """Check ToR."""

    query = "leaf_config"
    validators = [
        validate_interfaces,
        validate_routing_password,
        partial(validate_transit_vlans, transform_cls=ToR),
    ]
