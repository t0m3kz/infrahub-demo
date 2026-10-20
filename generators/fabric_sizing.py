"""Every fabric pool size, derived from the design envelope — pure, no I/O.

A DC's ``design`` (TopologyTemplateDC) and a pod's ``design``
(TopologyTemplatePod) state the ENVELOPE (``max_*``); the pools have to be sized
before the racks exist, so they are sized for that envelope, never for the
devices built so far. The inputs are the design dicts as the generator queries
deliver them after clean_data(): attribute name -> value, and the DC design's
``pod_templates`` as a list of pod design dicts.

Every result is a protocol-agnostic HOST-BIT width. The caller converts it with
host_bits_to_prefix_length() for the address family its parent pool is sliced
from (an IPv4 DC asks its /32 space for what an IPv6 DC asks its /128 space).

Model, per pod design and DC design:

- S = pod spines, L = pod leafs (rows x network racks x leafs per network rack),
  T = pod ToRs (rows x compute racks x ToRs per compute rack),
  B = border-leafs one pod can host (its own cap, never more than the fabric's),
  SS/HS = super-/hyper-spines of the fabric, P = max pods,
  S_sib = the widest sibling pod's spine count (any pod design the DC may host).
- A pod's links (one /31 or /127 each) are its spines' downlinks to every
  leaf/ToR/border-leaf slot, S x (L + T + B), plus its spines' uplinks: to every
  super-spine (S x SS), or, back-to-back, to every spine of every other pod
  ((P - 1) x S x S_sib). Access-leaf links count inside the L/T slot budget.
- A pool that addresses devices or links keeps 25% headroom; a pool that only
  holds child pools is exactly as wide as its children.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PodDesign = Mapping[str, Any]
DcDesign = Mapping[str, Any]

# A pool holding devices or links is never filled beyond 75% of its addresses.
_FILL_NUMERATOR = 3
_FILL_DENOMINATOR = 4
# Spare loopbacks a pod keeps beyond its envelope (kept from the former table).
_POD_LOOPBACK_SPARE = 2
# Spare loopbacks the DC-tier pool keeps beyond SS + BL + HS.
_FABRIC_LOOPBACK_SPARE = 2


def host_bits_to_prefix_length(host_bits: int, *, ipv6: bool) -> int:
    """The prefix length of a pool ``host_bits`` wide in the given address family."""
    return (128 if ipv6 else 32) - host_bits


def bits_to_hold(addresses: int) -> int:
    """The fewest host bits whose block holds ``addresses`` (ceil(log2)), no headroom."""
    return max(0, addresses - 1).bit_length()


def bits_with_headroom(addresses: int) -> int:
    """The fewest host bits whose block holds ``addresses`` at no more than 75% fill."""
    bits = 0
    while addresses * _FILL_DENOMINATOR > _FILL_NUMERATOR * 2**bits:
        bits += 1
    return bits


# ---------------------------------------------------------------------------
# Pod envelope
# ---------------------------------------------------------------------------


def pod_spines(pod: PodDesign) -> int:
    """S: the most spines (or border-spines) a pod of this design holds."""
    return pod["max_spines_per_pod"]


def pod_leafs(pod: PodDesign) -> int:
    """L: the most leafs a pod of this design holds."""
    return pod["max_rows"] * pod["max_network_racks_per_row"] * pod["max_leafs_per_network_rack"]


def pod_tors(pod: PodDesign) -> int:
    """T: the most ToRs a pod of this design holds."""
    return pod["max_rows"] * pod["max_compute_racks_per_row"] * pod["max_tors_per_compute_rack"]


def pod_border_leafs(pod: PodDesign, dc: DcDesign) -> int:
    """B: the most border-leafs a pod of this design hosts in a DC of this design."""
    return min(pod["max_border_leafs_per_pod"], dc["max_border_leafs_per_fabric"])


def max_sibling_spines(dc: DcDesign) -> int:
    """S_sib: the widest spine tier of any pod design the DC may host.

    Also the per-pod stride on the super-spines' downlink ports: every pod owns
    this many of them, whatever its own design."""
    return max(pod_spines(pod) for pod in _pod_templates(dc))


def pod_links(pod: PodDesign, dc: DcDesign) -> int:
    """The point-to-point links a pod of this design addresses from its technical pool."""
    spines = pod_spines(pod)
    downlinks = spines * (pod_leafs(pod) + pod_tors(pod) + pod_border_leafs(pod, dc))
    super_spines = dc["max_super_spines_per_fabric"]
    if super_spines > 0:
        uplinks = spines * super_spines
    else:
        uplinks = (dc["max_pods"] - 1) * spines * max_sibling_spines(dc)
    return downlinks + uplinks


def pod_technical_bits(pod: PodDesign, dc: DcDesign) -> int:
    """Host bits of a pod's technical (P2P) pool: two addresses per link, 25% headroom."""
    return bits_with_headroom(2 * pod_links(pod, dc))


def pod_loopback_bits(pod: PodDesign) -> int:
    """Host bits of a pod's loopback pool: one per spine, leaf and ToR, plus two spare."""
    return bits_to_hold(pod_spines(pod) + pod_leafs(pod) + pod_tors(pod) + _POD_LOOPBACK_SPARE)


def pod_devices(pod: PodDesign, dc: DcDesign) -> int:
    """The most switches one pod of this design holds: spines, leafs, ToRs and its border-leafs."""
    return pod_spines(pod) + pod_leafs(pod) + pod_tors(pod) + pod_border_leafs(pod, dc)


# ---------------------------------------------------------------------------
# DC envelope
# ---------------------------------------------------------------------------


def _pod_templates(dc: DcDesign) -> list[PodDesign]:
    """The pod designs the DC design may host; there is nothing to size a DC from without one."""
    templates = dc["pod_templates"]
    if not templates:
        raise ValueError(f"fabric design {dc.get('name')!r} lists no pod_templates")
    return templates


def fabric_loopback_bits(dc: DcDesign) -> int | None:
    """Host bits of the DC-tier loopback pool (super-spine, border-leaf, hyper-spine),
    25% headroom; None when the design has none of those tiers (no pool at all)."""
    tier_devices = (
        dc["max_super_spines_per_fabric"] + dc["max_border_leafs_per_fabric"] + dc["max_hyper_spines_per_fabric"]
    )
    if tier_devices == 0:
        return None
    return bits_with_headroom(tier_devices + _FABRIC_LOOPBACK_SPARE)


def dc_technical_bits(dc: DcDesign) -> int:
    """Host bits of the DC technical pool: one widest pod technical pool per pod,
    plus the super-spine <-> hyper-spine links, which draw from the DC pool itself."""
    pod_block = 2 ** max(pod_technical_bits(pod, dc) for pod in _pod_templates(dc))
    hyper_spine_links = dc["max_super_spines_per_fabric"] * dc["max_hyper_spines_per_fabric"]
    return bits_to_hold(dc["max_pods"] * pod_block + 2 * hyper_spine_links)


def dc_loopback_bits(dc: DcDesign) -> int:
    """Host bits of the DC loopback pool: one widest pod loopback pool per pod,
    plus the DC-tier loopback pool when the design has one."""
    pod_block = 2 ** max(pod_loopback_bits(pod) for pod in _pod_templates(dc))
    tier_bits = fabric_loopback_bits(dc)
    tier_block = 2**tier_bits if tier_bits is not None else 0
    return bits_to_hold(dc["max_pods"] * pod_block + tier_block)


def dc_management_devices(dc: DcDesign) -> int:
    """The most switches a DC of this design holds: max_pods of its largest pod
    design, plus the super- and hyper-spines."""
    largest_pod = max(pod_devices(pod, dc) for pod in _pod_templates(dc))
    return dc["max_pods"] * largest_pod + dc["max_super_spines_per_fabric"] + dc["max_hyper_spines_per_fabric"]


def dc_management_bits(dc: DcDesign) -> int:
    """Host bits of the DC management pool: one address per switch, 25% headroom."""
    return bits_with_headroom(dc_management_devices(dc))
