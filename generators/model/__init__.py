"""Pure domain logic compilers for network topologies (zero Infrahub I/O)."""

from generators.model.colocation_model import (
    COLO_VALID_FABRIC_ROLES,
    ColocationFabricModel,
    ColocationFlavor,
)
from generators.model.dc_model import DC_VALID_FABRIC_ROLES, DcFabricModel
from generators.model.office_model import OFFICE_VALID_FABRIC_ROLES, OfficeFabricModel
from generators.model.pod_model import PodFabricModel
from generators.model.rack_model import RackFabricModel

__all__ = [
    "COLO_VALID_FABRIC_ROLES",
    "ColocationFabricModel",
    "ColocationFlavor",
    "DC_VALID_FABRIC_ROLES",
    "DcFabricModel",
    "OFFICE_VALID_FABRIC_ROLES",
    "OfficeFabricModel",
    "PodFabricModel",
    "RackFabricModel",
]
