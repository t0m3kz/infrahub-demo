"""Fabric designs: the slice every fabric pool must have."""

from typing import NamedTuple

from generators.fabric_sizing import (
    dc_loopback_bits,
    dc_management_bits,
    dc_technical_bits,
    fabric_loopback_bits,
    host_bits_to_prefix_length,
    pod_loopback_bits,
    pod_technical_bits,
)
from generators.helpers.routing import underlay_pool_is_ipv6
from tests.unit.simulators.fabric_designs import dc_design, pod_design

# ---------------------------------------------------------------------------
# Fabric designs — the slice every fabric pool must have
#
# A DC's and a pod's pools are sized from their designs' envelope
# (generators/fabric_sizing.py) in the address family the DC's underlay_protocol
# gives each pool (generators/helpers/routing.underlay_pool_is_ipv6). The design
# rows are data/bootstrap/14b_fabric_templates.yml, read through the unit-test
# simulator, so a design change needs no edit here.
# ---------------------------------------------------------------------------

# A DC with no underlay_protocol is IPv6 (the schema default, and the generators' fallback).
DEFAULT_UNDERLAY_PROTOCOL = "ipv6"


class FabricSlice(NamedTuple):
    """The address family and prefix length one fabric pool's slice must have."""

    version: int
    prefix_length: int

    def __str__(self) -> str:
        return f"IPv{self.version} /{self.prefix_length}"


def _fabric_slice(host_bits: int, underlay_protocol: str, pool: str) -> FabricSlice:
    """The slice of a ``pool`` ("technical", "loopback" or "management") ``host_bits`` wide."""
    ipv6 = underlay_pool_is_ipv6(underlay_protocol, pool)
    return FabricSlice(6 if ipv6 else 4, host_bits_to_prefix_length(host_bits, ipv6=ipv6))


def expected_dc_pool_slices(design: str, underlay_protocol: str) -> dict[str, FabricSlice | None]:
    """A DC's pools for its bootstrap ``design``: technical, loopback, management and the fabric
    loopback (the DC-tier pool inside the loopback slice; None when the design has no DC tier).

    Raises KeyError for a design the bootstrap does not declare."""
    dc = dc_design(design)
    tier_bits = fabric_loopback_bits(dc)
    return {
        "technical": _fabric_slice(dc_technical_bits(dc), underlay_protocol, "technical"),
        "loopback": _fabric_slice(dc_loopback_bits(dc), underlay_protocol, "loopback"),
        "management": _fabric_slice(dc_management_bits(dc), underlay_protocol, "management"),
        "fabric_loopback": None if tier_bits is None else _fabric_slice(tier_bits, underlay_protocol, "loopback"),
    }


def expected_pod_pool_slices(
    dc_design_name: str, pod_design_name: str, underlay_protocol: str
) -> dict[str, FabricSlice]:
    """A pod's technical and loopback pools for its bootstrap design in its DC's bootstrap design.

    Raises KeyError for a design the bootstrap does not declare."""
    dc, pod = dc_design(dc_design_name), pod_design(pod_design_name)
    return {
        "technical": _fabric_slice(pod_technical_bits(pod, dc), underlay_protocol, "technical"),
        "loopback": _fabric_slice(pod_loopback_bits(pod), underlay_protocol, "loopback"),
    }
