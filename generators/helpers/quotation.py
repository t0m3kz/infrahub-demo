"""DC-fabric sizing/pricing recommendation logic.

Shared between the offline CLI (scripts/recommend_dc_design.py, which reads
its device/design catalog from data/bootstrap/*.yaml) and the add_quotation
generator (generators/quotation.py, which reads the same catalog live via
GraphQL from a CustomerQuotation's query response). This module holds only
the pure computation — no file I/O, no Infrahub client calls — so both
callers build a catalog their own way and pass it in.

Sizing algorithm, tier by tier, all with a hard minimum of 2 devices (no
single point of failure — mirrors border_services.py's HA-pairing pattern
for firewall/load-balancer, generalized to every tier):

  leaf     - customer ports must cover the server count at the required speed.
  spine    - downlink ports must cover the chosen leaf count (one link per
             leaf); spine count = leaf's uplink-port count (one uplink per
             spine), floored to 2.
  super-spine (only relevant with pods > 1) - downlink ports must cover the
             spine count per pod; count = spine's uplink-port count, floored
             to 2.
  border-leaf, firewall, load-balancer - fixed redundant pair (2x cheapest
             capable device type); these aren't driven by server count in
             the real generators either (see generators/border_services.py).

For pods > 1 there are two real architectures in this project (see
generators/models.py's DC_SIZE_LAYOUTS, S/M vs L/XL): back-to-back (pods
mesh their own spines directly, no super-spine tier) or classic 3-tier (a
dedicated super-spine aggregates every pod's spines). Neither is strictly
better - both are costed in build_switch_fabric() and the cheaper one is
returned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_REDUNDANCY = 2

LEAF_ROLES = {"leaf", "tor", "access-leaf", "l2-leaf"}
SWITCH_FABRIC_ROLES = {"leaf", "tor", "access-leaf", "l2-leaf", "spine", "super-spine", "border-leaf"}

# CustomerQuotationRoom field name -> DcimPhysicalInterface.interface_type value.
# One independent leaf tier is sized per non-zero speed (see build_multi_speed_leaf_tiers).
PORT_SPEEDS: dict[str, str] = {
    "port_count_10g": "10gbase-x-sfpp",
    "port_count_25g": "25gbase-x-sfp28",
    "port_count_40g": "40gbase-x-qsfpp",
    "port_count_100g": "100gbase-x-qsfp28",
    "port_count_400g": "400gbase-x-qsfpdd",
}


@dataclass
class PortGroup:
    count: int
    speed: str


@dataclass
class DeviceTemplate:
    device_type_id: str
    role: str
    manufacturer: str | None = None
    ports: dict[str, PortGroup] = field(default_factory=dict)


@dataclass
class TierResult:
    tier: str
    device_type_id: str | None
    count: int
    unit_price: float | None

    @property
    def total_cost(self) -> float | None:
        if self.unit_price is None or self.device_type_id is None:
            return None
        return self.unit_price * self.count


class Recommender:
    """Ranks DeviceTemplate candidates by cost for each fabric tier.

    `prices` and templates are keyed by device_type id (not name) so both
    the GraphQL-backed generator and the YAML-backed CLI can key on
    whatever identifier their own catalog naturally provides — the CLI's
    loader maps its YAML `name` strings to ids itself before constructing
    this class, so this module never has to know about names at all.
    """

    def __init__(self, templates: list[DeviceTemplate], prices: dict[str, float]) -> None:
        self.templates = templates
        self.prices = prices

    def manufacturers_for(self, roles: set[str]) -> list[str]:
        seen: set[str] = set()
        for t in self.templates:
            if t.role in roles and t.manufacturer:
                seen.add(t.manufacturer)
        return sorted(seen)

    def _candidates(self, role: str, manufacturer: str | None = None) -> list[DeviceTemplate]:
        seen: set[str] = set()
        out = []
        for t in self.templates:
            if t.role != role or t.device_type_id in seen:
                continue
            if manufacturer is not None and t.manufacturer != manufacturer:
                continue
            seen.add(t.device_type_id)
            out.append(t)
        return out

    def _candidates_any(self, roles: set[str], manufacturer: str | None = None) -> list[DeviceTemplate]:
        seen: set[str] = set()
        out = []
        for t in self.templates:
            if t.role not in roles or t.device_type_id in seen:
                continue
            if manufacturer is not None and t.manufacturer != manufacturer:
                continue
            seen.add(t.device_type_id)
            out.append(t)
        return out

    def cheapest_leaf(
        self, servers: int, speed: str, manufacturer: str | None = None
    ) -> tuple[TierResult, DeviceTemplate | None]:
        best: tuple[float, int, DeviceTemplate] | None = None
        for t in self._candidates_any(LEAF_ROLES, manufacturer):
            customer = t.ports.get("customer")
            if not customer or customer.speed != speed:
                continue
            count = max(MIN_REDUNDANCY, math.ceil(servers / customer.count))
            price = self.prices.get(t.device_type_id)
            cost = price * count if price is not None else float("inf")
            if best is None or cost < best[0]:
                best = (cost, count, t)
        if best is None:
            return TierResult("leaf", None, 0, None), None
        _, count, t = best
        return TierResult("leaf", t.device_type_id, count, self.prices.get(t.device_type_id)), t

    def cheapest_fanin(
        self, role: str, downstream_count: int, port_role: str = "downlink", manufacturer: str | None = None
    ) -> tuple[TierResult, DeviceTemplate | None]:
        """Cheapest device of `role` whose `port_role` port count covers
        `downstream_count` (one link per downstream device), sized to the
        chosen device's own uplink-port count (one uplink per redundant
        peer), floored to MIN_REDUNDANCY."""
        best: tuple[float, int, DeviceTemplate] | None = None
        for t in self._candidates(role, manufacturer):
            down = t.ports.get(port_role)
            if not down or down.count < downstream_count:
                continue
            uplink = t.ports.get("uplink")
            count = max(MIN_REDUNDANCY, uplink.count if uplink else MIN_REDUNDANCY)
            price = self.prices.get(t.device_type_id)
            cost = price * count if price is not None else float("inf")
            if best is None or cost < best[0]:
                best = (cost, count, t)
        if best is None:
            return TierResult(role, None, 0, None), None
        _, count, t = best
        return TierResult(role, t.device_type_id, count, self.prices.get(t.device_type_id)), t

    def cheapest_pair(self, role: str, manufacturer: str | None = None) -> TierResult:
        """Fixed redundant pair of the cheapest device_type for `role` -
        border-leaf/firewall/load-balancer aren't sized from server count in
        the real generators either (see generators/border_services.py's
        fixed HA-pairing at quantity==2). `manufacturer` pins the pick to a
        preferred vendor instead of the cheapest available."""
        candidates = self._candidates(role, manufacturer)
        priced = [(self.prices.get(t.device_type_id), t) for t in candidates]
        priced = [(p, t) for p, t in priced if p is not None]
        if not priced:
            return TierResult(role, None, 0, None)
        price, t = min(priced, key=lambda pt: pt[0])
        return TierResult(role, t.device_type_id, MIN_REDUNDANCY, price)


def _cheapest_border_leaf(rec: Recommender, manufacturer: str | None) -> TierResult:
    return rec.cheapest_pair("border-leaf", manufacturer)


def build_switch_fabric(
    rec: Recommender, servers: int, speed: str, pods: int, manufacturer: str | None
) -> list[TierResult]:
    """leaf/spine/(super-spine)/border-leaf tiers, optionally restricted to
    one switch manufacturer (border-leaf itself is priced as a fixed
    redundant pair like firewall/load-balancer - see cheapest_pair - but
    still manufacturer-filtered so a single-vendor build stays single-vendor
    end-to-end across the switch fabric).

    Returns exactly 4 TierResults in order: leaf, spine, super-spine (may
    have device_type_id=None if pods<=1 or no candidate fits), border-leaf.
    """
    leaf_result, _ = rec.cheapest_leaf(servers, speed, manufacturer)
    if leaf_result.device_type_id is None:
        return [
            leaf_result,
            TierResult("spine", None, 0, None),
            TierResult("super-spine", None, 0, None),
            TierResult("border-leaf", None, 0, None),
        ]

    border_leaf_result = _cheapest_border_leaf(rec, manufacturer)

    if pods <= 1:
        spine_result, _ = rec.cheapest_fanin("spine", leaf_result.count, manufacturer=manufacturer)
        return [leaf_result, spine_result, TierResult("super-spine", None, 0, None), border_leaf_result]

    # Back-to-back: each pod's spine tier meshes directly to every other
    # pod's spine tier (no super-spine device) - same downlink-fan-in
    # requirement as the single-pod case (leaf count), just re-priced with
    # cheapest_fanin's existing floor of 2 (mirrors generators/topology/
    # pod.py's _cable_to_existing_sibling_pods full inter-pod spine mesh).
    b2b_spine_result, _ = rec.cheapest_fanin("spine", leaf_result.count, manufacturer=manufacturer)
    b2b_cost = sum(
        t.total_cost for t in (leaf_result, b2b_spine_result, border_leaf_result) if t.total_cost is not None
    )

    # Classic 3-tier: dedicated super-spine aggregates every pod's spines.
    tier3_spine_result, spine_template = rec.cheapest_fanin("spine", leaf_result.count, manufacturer=manufacturer)
    tier3_super_spine_result = TierResult("super-spine", None, 0, None)
    if spine_template is not None:
        tier3_super_spine_result, _ = rec.cheapest_fanin(
            "super-spine", tier3_spine_result.count * pods, manufacturer=manufacturer
        )
    tier3_cost = sum(
        t.total_cost
        for t in (leaf_result, tier3_spine_result, tier3_super_spine_result, border_leaf_result)
        if t.total_cost is not None
    )

    if tier3_super_spine_result.device_type_id is not None and tier3_cost < b2b_cost:
        return [leaf_result, tier3_spine_result, tier3_super_spine_result, border_leaf_result]
    return [leaf_result, b2b_spine_result, TierResult("super-spine", None, 0, None), border_leaf_result]


def build_multi_speed_leaf_tiers(
    rec: Recommender, port_counts: dict[str, int], manufacturer: str | None = None
) -> list[TierResult]:
    """One independent TierResult per non-zero speed in `port_counts` (keys
    are DcimPhysicalInterface.interface_type values, e.g. PORT_SPEEDS.values())
    — each speed needs its own leaf hardware pick, priced/sized independently
    via cheapest_leaf. Zero-count speeds are skipped entirely (no empty
    TierResult placeholder — a room with no 25G ports has no 25G leaf row).
    All results carry tier="leaf" (role, not identity) — pair each result
    with its originating speed (e.g. by zipping against the non-zero keys
    of `port_counts`) to tell them apart."""
    results = []
    for speed, count in port_counts.items():
        if count <= 0:
            continue
        leaf_result, _ = rec.cheapest_leaf(count, speed, manufacturer)
        results.append(leaf_result)
    return results


def build_multi_speed_fabric(
    rec: Recommender, port_counts: dict[str, int], pods: int, manufacturer: str | None = None
) -> tuple[list[TierResult], TierResult, TierResult, TierResult]:
    """Multi-speed leaf tiers (one per non-zero speed) plus a single combined
    spine/super-spine/border-leaf sized from their SUMMED count — mirrors
    build_switch_fabric's tier shape and back-to-back-vs-classic-3-tier cost
    race exactly, just keyed off the summed leaf count instead of a single
    cheapest_leaf() call's count (see build_switch_fabric's own docstring for
    why that race always favors back-to-back by construction — unchanged
    here, not fixed in this pass).

    Returns (leaf_results, spine, super_spine, border_leaf). leaf_results is
    empty and every other tier is device_type_id=None when every port count
    is zero (mirrors build_switch_fabric's own "no leaf candidate" case).
    """
    leaf_results = build_multi_speed_leaf_tiers(rec, port_counts, manufacturer)
    total_leaf_count = sum(r.count for r in leaf_results)
    if total_leaf_count == 0:
        return (
            [],
            TierResult("spine", None, 0, None),
            TierResult("super-spine", None, 0, None),
            TierResult("border-leaf", None, 0, None),
        )

    border_leaf_result = _cheapest_border_leaf(rec, manufacturer)

    if pods <= 1:
        spine_result, _ = rec.cheapest_fanin("spine", total_leaf_count, manufacturer=manufacturer)
        return leaf_results, spine_result, TierResult("super-spine", None, 0, None), border_leaf_result

    # Back-to-back: same shape as build_switch_fabric, keyed off total_leaf_count.
    b2b_spine_result, _ = rec.cheapest_fanin("spine", total_leaf_count, manufacturer=manufacturer)
    b2b_cost = sum(
        t.total_cost for t in (*leaf_results, b2b_spine_result, border_leaf_result) if t.total_cost is not None
    )

    # Classic 3-tier: dedicated super-spine aggregates every pod's spines.
    tier3_spine_result, spine_template = rec.cheapest_fanin("spine", total_leaf_count, manufacturer=manufacturer)
    tier3_super_spine_result = TierResult("super-spine", None, 0, None)
    if spine_template is not None:
        tier3_super_spine_result, _ = rec.cheapest_fanin(
            "super-spine", tier3_spine_result.count * pods, manufacturer=manufacturer
        )
    tier3_cost = sum(
        t.total_cost
        for t in (*leaf_results, tier3_spine_result, tier3_super_spine_result, border_leaf_result)
        if t.total_cost is not None
    )

    if tier3_super_spine_result.device_type_id is not None and tier3_cost < b2b_cost:
        return leaf_results, tier3_spine_result, tier3_super_spine_result, border_leaf_result
    return leaf_results, b2b_spine_result, TierResult("super-spine", None, 0, None), border_leaf_result


def _build_hyper_spine_domains(
    rec: Recommender, spine_count_per_pod: int, pods: int, manufacturer: str | None
) -> tuple[TierResult, TierResult]:
    """Partition `pods` into super-spine domains sized to the best available
    super-spine device's downlink port capacity, then aggregate every
    domain's super-spine devices with a hyper-spine tier (full mesh — one
    hyper-spine downlink per super-spine device across every domain, same
    shape dc.py itself cables — see generators/topology/dc.py's hyper-spine
    <-> super-spine full mesh, and role: hyper-spine's description in
    schemas/extensions/topology/topology_dc.yml: "aggregates multiple
    super-spine groups"). Called only when a single flat super-spine tier
    can't reach every pod's spines directly (see build_fabric_tiers)."""
    empty = TierResult("super-spine", None, 0, None), TierResult("hyper-spine", None, 0, None)
    super_spine_candidates = rec._candidates("super-spine", manufacturer)  # noqa: SLF001
    priced = [(rec.prices.get(t.device_type_id), t) for t in super_spine_candidates]
    priced = [(p, t) for p, t in priced if p is not None]
    if not priced:
        return empty

    # Prefer the device with the most downlink capacity — fewer, larger
    # domains means fewer hyper-spine uplinks needed overall.
    _, best_template = max(priced, key=lambda pt: pt[1].ports.get("downlink", PortGroup(0, "")).count)
    downlink_capacity = best_template.ports.get("downlink", PortGroup(0, "")).count
    if downlink_capacity < spine_count_per_pod:
        # Even a single pod's spines exceed the best super-spine device's
        # downlink capacity — no domain size can work with this catalog.
        return empty

    max_pods_per_domain = max(1, downlink_capacity // spine_count_per_pod)
    domains = math.ceil(pods / max_pods_per_domain)
    pods_in_domain = min(pods, max_pods_per_domain)

    super_spine_per_domain, _ = rec.cheapest_fanin(
        "super-spine", spine_count_per_pod * pods_in_domain, manufacturer=manufacturer
    )
    if super_spine_per_domain.device_type_id is None:
        return empty

    total_super_spines = super_spine_per_domain.count * domains
    super_spine_result = TierResult(
        "super-spine", super_spine_per_domain.device_type_id, total_super_spines, super_spine_per_domain.unit_price
    )

    hyper_spine_result, _ = rec.cheapest_fanin("hyper-spine", total_super_spines, manufacturer=manufacturer)
    return super_spine_result, hyper_spine_result


def build_fabric_tiers(
    rec: Recommender, servers: int, speed: str, pods: int, manufacturer: str | None = None
) -> list[TierResult]:
    """Bottom-up: leaf, spine, super-spine, hyper-spine, border-leaf — from
    raw inputs (server count, port speed, pod count) all the way up,
    escalating one tier at a time only when the tier below's fan-in can't be
    covered by a single device generation.

    Generalizes build_switch_fabric() with automatic hyper-spine detection.
    Policy (deliberately NOT a cost race between back-to-back and classic
    3-tier — see the note below): always prefer the tiered topology when the
    catalog can physically build it, since that's what answers "do I need a
    super-spine/hyper-spine and how big" — back-to-back is a fallback for
    when the hardware genuinely can't support a tier above spine, not a
    cost-optimization alternative to it.

      1. pods <= 1: spine only, no super-spine/hyper-spine tier is possible
         (nothing to aggregate across).
      2. Try a flat super-spine covering every pod's spines directly.
      3. If no single super-spine device has enough downlink ports for that,
         partition pods into super-spine "domains" sized to the best
         available super-spine device, and add a hyper-spine tier
         aggregating across domains (see _build_hyper_spine_domains).
      4. If even that has no working hyper-spine candidate, fall back to
         back-to-back (pods mesh their own spines directly, no tier above
         spine at all).

    Why not cost-compare back-to-back vs classic-3-tier like
    build_switch_fabric does: that comparison is only sound if back-to-back's
    spine is sized differently from classic-3-tier's spine (back-to-back
    needs uplinks meshing every sibling pod directly; classic-3-tier's spine
    only needs uplinks toward the super-spine) — this module prices both
    with the identical cheapest_fanin("spine", leaf_result.count, ...) call,
    so a cost race between them always favors back-to-back by construction
    (tier3_cost = b2b_cost + super_spine_cost + hyper_spine_cost, never
    less). Modeling that distinction is future work; the always-prefer-
    tiered policy here sidesteps it rather than reproducing the same
    always-loses race for hyper-spine.

    Returns exactly 5 TierResults in order: leaf, spine, super-spine,
    hyper-spine, border-leaf. Firewall/load-balancer aren't part of this
    (see cheapest_pair) — same split as build_switch_fabric.
    """
    leaf_result, _ = rec.cheapest_leaf(servers, speed, manufacturer)
    if leaf_result.device_type_id is None:
        return [
            leaf_result,
            TierResult("spine", None, 0, None),
            TierResult("super-spine", None, 0, None),
            TierResult("hyper-spine", None, 0, None),
            TierResult("border-leaf", None, 0, None),
        ]

    border_leaf_result = _cheapest_border_leaf(rec, manufacturer)
    spine_result, spine_template = rec.cheapest_fanin("spine", leaf_result.count, manufacturer=manufacturer)

    if pods <= 1 or spine_template is None:
        return [
            leaf_result,
            spine_result,
            TierResult("super-spine", None, 0, None),
            TierResult("hyper-spine", None, 0, None),
            border_leaf_result,
        ]

    super_spine_result, _ = rec.cheapest_fanin("super-spine", spine_result.count * pods, manufacturer=manufacturer)
    hyper_spine_result = TierResult("hyper-spine", None, 0, None)

    if super_spine_result.device_type_id is None:
        super_spine_result, hyper_spine_result = _build_hyper_spine_domains(rec, spine_result.count, pods, manufacturer)
        if super_spine_result.device_type_id is not None and hyper_spine_result.device_type_id is None:
            # _build_hyper_spine_domains only ever partitions into >1 domain
            # (a 1-domain split would have succeeded in the flat call above)
            # — multiple super-spine domains with no hyper-spine to link
            # them is not a valid topology, not just a smaller one.
            super_spine_result = TierResult("super-spine", None, 0, None)

    if super_spine_result.device_type_id is None:
        # Hardware genuinely can't support a tier above spine at this scale
        # — fall back to back-to-back.
        return [
            leaf_result,
            spine_result,
            TierResult("super-spine", None, 0, None),
            TierResult("hyper-spine", None, 0, None),
            border_leaf_result,
        ]

    return [leaf_result, spine_result, super_spine_result, hyper_spine_result, border_leaf_result]


def max_fabric_capacity(rec: Recommender, design: dict, speed: str, manufacturer: str | None = None) -> dict:
    """Top-down: inverse of build_fabric_tiers()/build_switch_fabric().

    Given a design's pod cap (`design["max_pods"]`, same dict shape as
    recommend_design's DC_SIZE_LAYOUTS entries) and a required
    customer-facing port speed, computes the maximum number of servers this
    design can physically support — bounded by the best leaf/spine device
    types actually available in the catalog for that speed/manufacturer, not
    by the design's own numbers alone (a design's caps say how many
    devices/pods are ALLOWED; the actual hardware determines how much each
    one can carry).

    Per pod, capacity is bounded by the best spine candidate's downlink port
    count (max leafs that can direct-attach in a full-mesh Clos pod — one
    link per leaf, same full-mesh assumption build_switch_fabric's
    cheapest_fanin makes) times the best leaf candidate's customer-port
    count (servers per leaf), times the number of pods the design allows.

    Returns a dict: max_servers, max_leafs_per_pod, leaf_device_type_id,
    spine_device_type_id (the last two so a caller can report which
    hardware the capacity figure assumes).
    """
    leaf_candidates = [
        t
        for t in rec._candidates_any(LEAF_ROLES, manufacturer)  # noqa: SLF001
        if (customer := t.ports.get("customer")) and customer.speed == speed
    ]
    if not leaf_candidates:
        return {"max_servers": 0, "max_leafs_per_pod": 0, "leaf_device_type_id": None, "spine_device_type_id": None}
    best_leaf = max(leaf_candidates, key=lambda t: t.ports["customer"].count)

    spine_candidates = rec._candidates("spine", manufacturer)  # noqa: SLF001
    if not spine_candidates:
        return {
            "max_servers": 0,
            "max_leafs_per_pod": 0,
            "leaf_device_type_id": best_leaf.device_type_id,
            "spine_device_type_id": None,
        }
    best_spine = max(spine_candidates, key=lambda t: t.ports.get("downlink", PortGroup(0, "")).count)
    max_leafs_per_pod = best_spine.ports.get("downlink", PortGroup(0, "")).count

    max_pods = design.get("max_pods", 1)
    max_servers = max_pods * max_leafs_per_pod * best_leaf.ports["customer"].count
    return {
        "max_servers": max_servers,
        "max_leafs_per_pod": max_leafs_per_pod,
        "leaf_device_type_id": best_leaf.device_type_id,
        "spine_device_type_id": best_spine.device_type_id,
    }


def recommend_design(
    designs: list[dict], spine_count: int, super_spine_count: int, border_leaf_count: int
) -> dict | None:
    """Smallest design dict (each with name/id + max_spines_per_pod/
    max_super_spines_per_fabric/max_border_leafs_per_fabric) whose caps cover
    the computed fabric.

    Does NOT assume `designs` arrives pre-sorted smallest-first — the CLI's
    YAML loader happens to preserve S/M/L/XL declaration order, but a
    GraphQL query result carries no such ordering guarantee (confirmed via a
    live query returning L/M/S/XL). Instead, among every fitting candidate,
    ranks by the sum of the three capacity caps (a design with smaller caps
    overall is "smaller") and returns the smallest — S/M aren't strictly
    ordered on every individual field (both cap super_spines at 0), but the
    summed caps still order correctly (S=2, M=6, L=10, XL=12)."""
    fitting: list[tuple[int, dict]] = []
    for entry in designs:
        max_spines = entry.get("max_spines_per_pod", 0)
        max_super_spines = entry.get("max_super_spines_per_fabric", 0)
        max_border_leafs = entry.get("max_border_leafs_per_fabric", 0)
        if (
            spine_count <= max_spines
            and super_spine_count <= max_super_spines
            and border_leaf_count <= max_border_leafs
        ):
            fitting.append((max_spines + max_super_spines + max_border_leafs, entry))
    if not fitting:
        return None
    fitting.sort(key=lambda pair: pair[0])
    return fitting[0][1]


def device_templates_from_graphql(raw_templates: list[dict], manufacturers: dict[str, str]) -> list[DeviceTemplate]:
    """Build DeviceTemplate objects from add_quotation.gql's
    TemplateDcimPhysicalDevice query result.

    Unlike the CLI's bootstrap-YAML loader, interfaces here arrive already
    expanded by Infrahub (one node per physical port, e.g. "Ethernet1/1",
    "Ethernet2/1", ...) rather than the YAML's compact range syntax
    ("Ethernet[1-24]/1") - so this just counts edges per interface role,
    no range-parsing needed.

    `manufacturers` maps device_type id -> manufacturer name (built by the
    caller from the same query's DcimDeviceType edges).
    """
    templates: list[DeviceTemplate] = []
    for device in raw_templates:
        role = device.get("role")
        device_type = device.get("device_type") or {}
        device_type_id = device_type.get("id")
        if not role or not device_type_id:
            continue
        ports: dict[str, PortGroup] = {}
        for iface in device.get("interfaces", []):
            iface_role = iface.get("role")
            if not iface_role:
                continue
            speed = iface.get("interface_type", "")
            if iface_role in ports:
                ports[iface_role].count += 1
            else:
                ports[iface_role] = PortGroup(count=1, speed=speed)
        templates.append(
            DeviceTemplate(
                device_type_id=device_type_id,
                role=role,
                manufacturer=manufacturers.get(device_type_id),
                ports=ports,
            )
        )
    return templates


def recommend_pod_design(pod_designs: list[dict], leaf_count: int, spine_count: int) -> dict | None:
    """Smallest pod layout dict (see POD_LAYOUTS in generators/models.py)
    whose leaf capacity (rows * network_racks_per_row *
    max_leafs_per_network_rack - the middle_rack/mixed deployment shape the
    leaf tier assumes) and max_spines_per_pod both cover the computed
    fabric. Layouts with max_leafs_per_network_rack=0 (pure ToR layouts)
    don't host `leaf`-role devices and are skipped."""
    fitting: list[tuple[int, dict]] = []
    for entry in pod_designs:
        max_leafs_per_rack = entry.get("max_leafs_per_network_rack", 0)
        if max_leafs_per_rack == 0:
            continue
        capacity = entry["rows"] * entry["network_racks_per_row"] * max_leafs_per_rack
        if leaf_count <= capacity and spine_count <= entry.get("max_spines_per_pod", 0):
            fitting.append((capacity, entry))
    if not fitting:
        return None
    fitting.sort(key=lambda pair: pair[0])
    return fitting[0][1]


def validate_room_capacity(rooms: list[dict]) -> dict:
    """Check that every declared CustomerQuotationRoom's own physical
    capacity (rows * racks_per_row) covers ITS OWN compute_rack_count +
    storage_rack_count — both now live on the same room dict (a room is the
    sizing unit, not just a capacity label; see module docstring).

    Returns a dict: fits (bool), total_room_capacity, required_racks,
    shortfall (0 if fits) — all summed across every room, since a customer
    could in principle overfill one room while underfilling another and
    still have the combined total work out; per-room shortfall reporting
    is a possible follow-up, not implemented here."""
    total_room_capacity = sum(room["rows"] * room["racks_per_row"] for room in rooms)
    required_racks = sum(room.get("compute_rack_count", 0) + room.get("storage_rack_count", 0) for room in rooms)
    shortfall = max(0, required_racks - total_room_capacity)
    return {
        "fits": shortfall == 0,
        "total_room_capacity": total_room_capacity,
        "required_racks": required_racks,
        "shortfall": shortfall,
    }


def distribute_evenly(total: int, n: int) -> list[int]:
    """Split `total` into `n` shares as evenly as possible — remainder
    distributed one-by-one to the first shares (e.g. 100 into 3 -> [34, 33,
    33]), so every pod's share differs by at most 1 from every other's."""
    if n <= 0:
        return []
    base, remainder = divmod(total, n)
    return [base + 1 if i < remainder else base for i in range(n)]


def build_proposed_pods(
    rec: Recommender,
    servers: int,
    speed: str,
    pod_count: int,
    compute_rack_count: int,
    storage_rack_count: int,
    manufacturer: str | None = None,
) -> list[dict]:
    """Split a DC-wide request evenly across `pod_count` pods and size each
    pod's leaf/spine tier independently — one pod's rack share never shares
    its spine redundancy with another pod's (see cheapest_fanin's own
    MIN_REDUNDANCY floor, applied per pod here, not once for the whole DC).

    Servers are divided evenly across pods (distribute_evenly) to size each
    pod's own leaf count; compute/storage rack counts are divided the same
    way purely for reporting (QuotationProposedPod.compute_rack_share/
    storage_rack_share) — this project has no per-pod rack-to-server density
    model yet, so rack shares don't feed back into the leaf/spine sizing
    itself.

    Returns one dict per pod (1-indexed via "index"): index, servers,
    compute_rack_share, storage_rack_share, leaf_count, spine_count,
    leaf_device_type_id, spine_device_type_id.
    """
    if pod_count <= 0:
        return []
    server_shares = distribute_evenly(servers, pod_count)
    compute_shares = distribute_evenly(compute_rack_count, pod_count)
    storage_shares = distribute_evenly(storage_rack_count, pod_count)

    pods: list[dict] = []
    for i in range(pod_count):
        leaf_result, _ = rec.cheapest_leaf(server_shares[i], speed, manufacturer)
        spine_result = TierResult("spine", None, 0, None)
        if leaf_result.device_type_id is not None:
            spine_result, _ = rec.cheapest_fanin("spine", leaf_result.count, manufacturer=manufacturer)
        pods.append(
            {
                "index": i + 1,
                "servers": server_shares[i],
                "compute_rack_share": compute_shares[i],
                "storage_rack_share": storage_shares[i],
                "leaf_count": leaf_result.count,
                "spine_count": spine_result.count,
                "leaf_device_type_id": leaf_result.device_type_id,
                "spine_device_type_id": spine_result.device_type_id,
            }
        )
    return pods


def build_room_pods(rec: Recommender, rooms: list[dict], pod_count: int, manufacturer: str | None = None) -> list[dict]:
    """Map pods 1:1 to rooms (pod N <- rooms[N-1]) and size each pod from
    exactly that room's own port_count_*/compute_rack_count/
    storage_rack_count via build_multi_speed_fabric — NOT an even DC-wide
    split (see build_proposed_pods for that older, still-supported path).

    Mismatch policy (rooms are the sizing unit, pod_count is independent):
      pod_count <= len(rooms): pods beyond pod_count simply aren't created;
        the caller (generators/quotation.py) is responsible for warning
        about any unused surplus rooms — this function only ever returns
        `pod_count` pods.
      pod_count > len(rooms): pods with no room of their own round-robin
        back onto existing rooms (pod_index % len(rooms)), reusing that
        room's full requirements again rather than splitting them further
        — a room's port/rack counts aren't a divisible resource below its
        own declared total without inventing a new sub-unit.
      no rooms declared at all: every pod is returned with an empty fabric
        (no leaf candidates, 0 everywhere) — mirrors build_multi_speed_fabric's
        own zero-port-count short-circuit.

    Returns one dict per pod (1-indexed via "index"): index, room_id (the
    id of the room this pod is sized from, or None), port_counts (the
    resolved per-speed dict actually used, keyed by interface_type — lets
    the caller sum these across pods for a DC-wide total without
    re-deriving the room-to-pod mapping), compute_rack_share,
    storage_rack_share, leaf_results (list[TierResult], one per non-zero
    speed), leaf_count (sum of leaf_results' counts), spine_count,
    spine_device_type_id, super_spine_count, super_spine_device_type_id,
    border_leaf_count, border_leaf_device_type_id.
    """
    pods: list[dict] = []
    for i in range(pod_count):
        room = rooms[i % len(rooms)] if rooms else None
        port_counts = {speed: (room.get(field, 0) if room else 0) for field, speed in PORT_SPEEDS.items()}
        compute_share = room.get("compute_rack_count", 0) if room else 0
        storage_share = room.get("storage_rack_count", 0) if room else 0

        leaf_results, spine_result, super_spine_result, border_leaf_result = build_multi_speed_fabric(
            rec, port_counts, pods=1, manufacturer=manufacturer
        )
        pods.append(
            {
                "index": i + 1,
                "room_id": room.get("id") if room else None,
                "port_counts": port_counts,
                "compute_rack_share": compute_share,
                "storage_rack_share": storage_share,
                "leaf_results": leaf_results,
                "leaf_count": sum(r.count for r in leaf_results),
                "spine_count": spine_result.count,
                "spine_device_type_id": spine_result.device_type_id,
                "super_spine_count": super_spine_result.count,
                "super_spine_device_type_id": super_spine_result.device_type_id,
                "border_leaf_count": border_leaf_result.count,
                "border_leaf_device_type_id": border_leaf_result.device_type_id,
            }
        )
    return pods


def assign_racks_to_rooms(
    compute_racks: int, storage_racks: int, rooms: list[dict], pod_index: int = 1, pod_count: int = 1
) -> list[dict]:
    """Build one dict per rack (index, rack_type, room_id) for a single
    pod's compute_rack_share + storage_rack_share.

    Prefers one room per pod: when there are at least as many rooms as
    pods (`len(rooms) >= pod_count`), this pod's racks all go to its own
    dedicated room — `rooms[pod_index - 1]` (pod 1 -> rooms[0], pod 2 ->
    rooms[1], ...) — rather than sharing a room with any other pod.

    Falls back to round-robin across every room when there are fewer rooms
    than pods (rooms aren't typed by rack kind — see QuotationRoom — any
    room can take either rack type in this pass): the Nth rack in this
    pod's own share goes to room N mod len(rooms), not sequentially filling
    one room before moving to the next.

    A rack's room_id is None if no rooms were declared at all — the caller
    already surfaces a capacity shortfall via validate_room_capacity
    separately; this function doesn't re-check per-room capacity limits,
    only distributes.

    Returns dicts in creation order: all compute racks (index 1..N) first,
    then all storage racks (index N+1..N+M) — index is unique per pod across
    both types, matching QuotationProposedRack.uniqueness_constraints
    ([pod, index]).
    """
    one_room_per_pod = bool(rooms) and len(rooms) >= pod_count
    dedicated_room_id = rooms[pod_index - 1]["id"] if one_room_per_pod else None

    racks: list[dict] = []
    index = 1
    for rack_type, count in (("compute", compute_racks), ("storage", storage_racks)):
        for _ in range(count):
            if one_room_per_pod:
                room_id = dedicated_room_id
            else:
                room_id = rooms[(index - 1) % len(rooms)]["id"] if rooms else None
            racks.append({"index": index, "rack_type": rack_type, "room_id": room_id})
            index += 1
    return racks
