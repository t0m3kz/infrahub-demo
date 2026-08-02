#!/usr/bin/env python3
"""Calculate minimal switch count for a typical Clos fabric.

How to use
==========

1) Run help:
    python scripts/clos_switch_calculator.py --help

2) Define at least one room using --room.
    Accepted formats:
    - rows:racks
    - name:rows:racks

    Examples:
    - --room 8:30
    - --room roomA:10:24 --room roomB:12:28

3) Choose cabling method with --method:
    - tor: Top-of-Rack leafs connected directly to spine.
    - middle: Middle-of-Row leafs connected directly to spine.
    - mixed: ToR part + L2 leaf -> L1 leaf -> spine part.
    - all: calculate tor, middle, mixed and pick minimum.

4) Set traffic and resilience assumptions:
    - --oversubscription 1:2 means uplink:downlink ratio.
    - --homing single|dual controls 1x or 2x rack port demand.
    - --fabric-count 2 models A/B fabric duplication.
    - --spare-ratio keeps free headroom, e.g. 0.2 = 20% reserve.

5) Set switch profiles and reservations:
    - --access-switch-ports, --spine-switch-ports, --l2-switch-ports
    - --reserved-access-ports, --reserved-spine-ports, --reserved-l2-ports

6) Optional layout tuning:
    - --tor-racks-per-switch
    - --middle-racks-per-switch
    - --mixed-tor-share
    - --min-spines

Output
======

The script prints, per method:
- access leaf count
- optional L2 and L1 leaf count (mixed)
- spine count
- total switches for one fabric and for all fabrics
- minimal total among evaluated methods
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import Literal

Method = Literal["tor", "middle", "mixed", "all"]


@dataclass(frozen=True)
class Room:
    """Room description used for capacity calculations."""

    name: str
    rows: int
    racks_per_row: int

    @property
    def racks(self) -> int:
        return self.rows * self.racks_per_row


@dataclass(frozen=True)
class SwitchProfile:
    """Switch profile with port count and reserved ports."""

    ports: int
    reserved_ports: int


@dataclass(frozen=True)
class Oversubscription:
    """Oversubscription expressed as uplink:downlink, e.g. 1:2."""

    uplink_units: int
    downlink_units: int

    @property
    def total_units(self) -> int:
        return self.uplink_units + self.downlink_units


@dataclass
class AccessSizingResult:
    """Sizing result for one access stage."""

    switch_count: int
    downlink_capacity_per_switch: int
    uplink_capacity_per_switch: int
    uplinks_required_total: int


@dataclass
class FabricResult:
    """Computed switch counts for a method."""

    method: str
    access_leaf: int
    l2_leaf: int
    l1_leaf: int
    spine: int
    total_single_fabric: int
    total_all_fabrics: int


def parse_room(spec: str, index: int) -> Room:
    """Parse room spec in rows:racks or name:rows:racks format."""
    parts = spec.split(":")
    if len(parts) == 2:
        rows_str, racks_str = parts
        name = f"room-{index}"
    elif len(parts) == 3:
        name, rows_str, racks_str = parts
    else:
        msg = f"Invalid --room '{spec}'. Use rows:racks or name:rows:racks"
        raise argparse.ArgumentTypeError(msg)

    try:
        rows = int(rows_str)
        racks = int(racks_str)
    except ValueError as exc:
        msg = f"Invalid --room '{spec}'. rows and racks must be integers"
        raise argparse.ArgumentTypeError(msg) from exc

    if rows <= 0 or racks <= 0:
        msg = f"Invalid --room '{spec}'. rows and racks must be > 0"
        raise argparse.ArgumentTypeError(msg)

    return Room(name=name, rows=rows, racks_per_row=racks)


def parse_oversubscription(value: str) -> Oversubscription:
    """Parse oversubscription string in uplink:downlink format."""
    parts = value.split(":")
    if len(parts) != 2:
        msg = f"Invalid oversubscription '{value}'. Use uplink:downlink, e.g. 1:2"
        raise argparse.ArgumentTypeError(msg)

    try:
        uplink, downlink = (int(parts[0]), int(parts[1]))
    except ValueError as exc:
        msg = f"Invalid oversubscription '{value}'. Values must be integers"
        raise argparse.ArgumentTypeError(msg) from exc

    if uplink <= 0 or downlink <= 0:
        msg = f"Invalid oversubscription '{value}'. Values must be > 0"
        raise argparse.ArgumentTypeError(msg)

    return Oversubscription(uplink_units=uplink, downlink_units=downlink)


def effective_ports(total_ports: int, reserved_ports: int, spare_ratio: float) -> int:
    """Compute usable ports after reserves and spare capacity."""
    if total_ports <= 0:
        msg = "Switch ports must be > 0"
        raise ValueError(msg)
    if reserved_ports < 0 or reserved_ports >= total_ports:
        msg = "reserved_ports must be >= 0 and < total_ports"
        raise ValueError(msg)
    if not 0 <= spare_ratio < 1:
        msg = "spare_ratio must be in [0, 1)"
        raise ValueError(msg)

    return math.floor((total_ports - reserved_ports) * (1 - spare_ratio))


def split_access_uplink(usable_ports: int, oversub: Oversubscription) -> tuple[int, int]:
    """Split usable ports into downlinks/uplinks using oversub ratio."""
    downlinks = math.floor(usable_ports * oversub.downlink_units / oversub.total_units)
    uplinks = usable_ports - downlinks
    return downlinks, uplinks


def size_access_stage(
    *,
    demand_access_ports: int,
    demand_racks: int,
    profile: SwitchProfile,
    oversub: Oversubscription,
    spare_ratio: float,
    racks_per_switch: int | None,
    min_uplink_switches: int,
) -> AccessSizingResult:
    """Size an access stage and return switch count and northbound uplink demand."""
    usable_ports = effective_ports(profile.ports, profile.reserved_ports, spare_ratio)
    down_cap, up_cap = split_access_uplink(usable_ports, oversub)

    if down_cap <= 0 or up_cap <= 0:
        msg = "Switch profile does not leave enough downlink/uplink ports after reserve and spare"
        raise ValueError(msg)

    by_ports = math.ceil(demand_access_ports / down_cap) if demand_access_ports > 0 else 0
    by_racks = 0
    if racks_per_switch is not None:
        if racks_per_switch <= 0:
            msg = "racks_per_switch must be > 0 when provided"
            raise ValueError(msg)
        by_racks = math.ceil(demand_racks / racks_per_switch) if demand_racks > 0 else 0

    switch_count = max(by_ports, by_racks)
    if demand_access_ports > 0:
        switch_count = max(1, switch_count)

    remaining_down = demand_access_ports
    uplinks_required_total = 0

    for _ in range(switch_count):
        used_down = min(down_cap, remaining_down)
        remaining_down -= used_down

        required_up = math.ceil(used_down * oversub.uplink_units / oversub.downlink_units) if used_down > 0 else 0
        if used_down > 0:
            required_up = max(required_up, min_uplink_switches)
        if required_up > up_cap:
            msg = (
                "Cannot satisfy uplink requirement for one switch. "
                "Reduce oversubscription, reduce spare ratio, or use a larger switch."
            )
            raise ValueError(msg)

        uplinks_required_total += required_up

    if remaining_down > 0:
        msg = "Internal sizing error: remaining demand after stage sizing"
        raise RuntimeError(msg)

    return AccessSizingResult(
        switch_count=switch_count,
        downlink_capacity_per_switch=down_cap,
        uplink_capacity_per_switch=up_cap,
        uplinks_required_total=uplinks_required_total,
    )


def size_spine(
    *,
    total_leaf_uplinks: int,
    spine_profile: SwitchProfile,
    spare_ratio: float,
    min_spines: int,
) -> int:
    """Size spine layer from total leaf uplink demand."""
    spine_ports = effective_ports(spine_profile.ports, spine_profile.reserved_ports, spare_ratio)
    if spine_ports <= 0:
        msg = "Spine effective ports must be > 0"
        raise ValueError(msg)
    if min_spines <= 0:
        msg = "min_spines must be > 0"
        raise ValueError(msg)

    needed_by_capacity = math.ceil(total_leaf_uplinks / spine_ports) if total_leaf_uplinks > 0 else 0
    return max(min_spines if total_leaf_uplinks > 0 else 0, needed_by_capacity)


def calculate_tor_or_middle(
    *,
    method: str,
    total_racks: int,
    access_demand_ports: int,
    access_profile: SwitchProfile,
    spine_profile: SwitchProfile,
    oversub: Oversubscription,
    spare_ratio: float,
    racks_per_access_switch: int,
    min_spines: int,
    fabrics: int,
) -> FabricResult:
    """Calculate 2-stage Clos for ToR or Middle-Rack cabling."""
    access = size_access_stage(
        demand_access_ports=access_demand_ports,
        demand_racks=total_racks,
        profile=access_profile,
        oversub=oversub,
        spare_ratio=spare_ratio,
        racks_per_switch=racks_per_access_switch,
        min_uplink_switches=min_spines,
    )

    spine = size_spine(
        total_leaf_uplinks=access.uplinks_required_total,
        spine_profile=spine_profile,
        spare_ratio=spare_ratio,
        min_spines=min_spines,
    )

    total_single = access.switch_count + spine
    return FabricResult(
        method=method,
        access_leaf=access.switch_count,
        l2_leaf=0,
        l1_leaf=0,
        spine=spine,
        total_single_fabric=total_single,
        total_all_fabrics=total_single * fabrics,
    )


def calculate_mixed(
    *,
    total_racks: int,
    access_demand_ports: int,
    leaf_profile: SwitchProfile,
    l2_profile: SwitchProfile,
    spine_profile: SwitchProfile,
    oversub: Oversubscription,
    spare_ratio: float,
    tor_racks_per_switch: int,
    middle_racks_per_switch: int,
    mixed_tor_share: float,
    min_spines: int,
    fabrics: int,
) -> FabricResult:
    """Calculate mixed topology: ToR direct + L2 leaf feeding L1 leaf."""
    if not 0 <= mixed_tor_share <= 1:
        msg = "mixed_tor_share must be in [0, 1]"
        raise ValueError(msg)

    tor_racks = round(total_racks * mixed_tor_share)
    middle_racks = total_racks - tor_racks

    # Split access demand proportional to rack split.
    tor_ports = round(access_demand_ports * mixed_tor_share)
    middle_ports = access_demand_ports - tor_ports

    tor_direct = size_access_stage(
        demand_access_ports=tor_ports,
        demand_racks=tor_racks,
        profile=leaf_profile,
        oversub=oversub,
        spare_ratio=spare_ratio,
        racks_per_switch=tor_racks_per_switch,
        min_uplink_switches=min_spines,
    )

    l2_stage = size_access_stage(
        demand_access_ports=middle_ports,
        demand_racks=middle_racks,
        profile=l2_profile,
        oversub=oversub,
        spare_ratio=spare_ratio,
        racks_per_switch=middle_racks_per_switch,
        min_uplink_switches=min_spines,
    )

    # L1 leafs terminate L2 uplinks and forward northbound to spine.
    l1_for_l2 = size_access_stage(
        demand_access_ports=l2_stage.uplinks_required_total,
        demand_racks=0,
        profile=leaf_profile,
        oversub=oversub,
        spare_ratio=spare_ratio,
        racks_per_switch=None,
        min_uplink_switches=min_spines,
    )

    spine_uplinks = tor_direct.uplinks_required_total + l1_for_l2.uplinks_required_total
    spine = size_spine(
        total_leaf_uplinks=spine_uplinks,
        spine_profile=spine_profile,
        spare_ratio=spare_ratio,
        min_spines=min_spines,
    )

    total_single = tor_direct.switch_count + l2_stage.switch_count + l1_for_l2.switch_count + spine
    return FabricResult(
        method="mixed",
        access_leaf=tor_direct.switch_count,
        l2_leaf=l2_stage.switch_count,
        l1_leaf=l1_for_l2.switch_count,
        spine=spine,
        total_single_fabric=total_single,
        total_all_fabrics=total_single * fabrics,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Clos switch calculator for ToR/Middle/Mixed topologies",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  1) Compare all methods for two rooms:\n"
            "     python scripts/clos_switch_calculator.py \\\n"
            "       --room roomA:8:30 --room roomB:6:24 --method all \\\n"
            "       --oversubscription 1:2 --homing dual --fabric-count 2 \\\n"
            "       --access-switch-ports 64 --spine-switch-ports 64 --l2-switch-ports 48\n\n"
            "  2) Force ToR-only sizing:\n"
            "     python scripts/clos_switch_calculator.py \\\n"
            "       --room 10:24 --method tor --tor-racks-per-switch 1\n\n"
            "  3) Middle-of-Row with stronger aggregation:\n"
            "     python scripts/clos_switch_calculator.py \\\n"
            "       --room 12:28 --method middle --middle-racks-per-switch 12\n\n"
            "  4) Mixed mode (40% ToR, 60% via L2->L1):\n"
            "     python scripts/clos_switch_calculator.py \\\n"
            "       --room 8:30 --method mixed --mixed-tor-share 0.4\n"
        ),
    )
    parser.add_argument(
        "--room",
        action="append",
        required=True,
        help="Room definition in rows:racks or name:rows:racks format. Repeat for many rooms.",
    )
    parser.add_argument(
        "--method",
        choices=["tor", "middle", "mixed", "all"],
        default="all",
        help="Cabling method to evaluate.",
    )
    parser.add_argument("--oversubscription", default="1:2", help="Oversubscription as uplink:downlink (default 1:2)")
    parser.add_argument("--ports-per-rack", type=int, default=1, help="Access ports required per rack per fabric")
    parser.add_argument("--homing", choices=["single", "dual"], default="single", help="Single or dual-homed racks")
    parser.add_argument("--fabric-count", type=int, default=1, help="Number of fabrics (e.g. 2 for A/B)")
    parser.add_argument("--spare-ratio", type=float, default=0.2, help="Capacity reserve ratio in [0,1)")

    parser.add_argument("--access-switch-ports", type=int, default=64, help="Leaf/access switch port count")
    parser.add_argument("--spine-switch-ports", type=int, default=64, help="Spine switch port count")
    parser.add_argument("--l2-switch-ports", type=int, default=48, help="L2 leaf port count for mixed mode")

    parser.add_argument("--reserved-access-ports", type=int, default=0, help="Reserved ports on access/leaf switch")
    parser.add_argument("--reserved-spine-ports", type=int, default=0, help="Reserved ports on spine switch")
    parser.add_argument("--reserved-l2-ports", type=int, default=0, help="Reserved ports on L2 leaf switch")

    parser.add_argument("--tor-racks-per-switch", type=int, default=1, help="Racks aggregated by one ToR switch")
    parser.add_argument("--middle-racks-per-switch", type=int, default=8, help="Racks aggregated by one middle switch")
    parser.add_argument(
        "--mixed-tor-share", type=float, default=0.5, help="Share of racks in ToR segment for mixed mode"
    )
    parser.add_argument("--min-spines", type=int, default=2, help="Minimum spine count for redundancy")

    return parser


def print_summary(rooms: list[Room], access_demand_ports: int, results: list[FabricResult]) -> None:
    """Print human-readable summary."""
    total_racks = sum(room.racks for room in rooms)
    print("Input summary")
    print("-------------")
    for room in rooms:
        print(f"- {room.name}: rows={room.rows}, racks_per_row={room.racks_per_row}, racks={room.racks}")
    print(f"Total racks: {total_racks}")
    print(f"Total access port demand per fabric: {access_demand_ports}")
    print()

    print("Results")
    print("-------")
    for result in results:
        print(f"Method: {result.method}")
        print(f"  Access leaf: {result.access_leaf}")
        if result.l2_leaf:
            print(f"  L2 leaf: {result.l2_leaf}")
        if result.l1_leaf:
            print(f"  L1 leaf: {result.l1_leaf}")
        print(f"  Spine: {result.spine}")
        print(f"  Total (single fabric): {result.total_single_fabric}")
        print(f"  Total (all fabrics): {result.total_all_fabrics}")
        print()

    best = min(results, key=lambda item: item.total_all_fabrics)
    print(f"Minimal switch count: {best.total_all_fabrics} (method={best.method})")


def main() -> None:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    rooms = [parse_room(spec, idx + 1) for idx, spec in enumerate(args.room)]
    oversub = parse_oversubscription(args.oversubscription)

    total_racks = sum(room.racks for room in rooms)
    if args.ports_per_rack <= 0:
        msg = "ports-per-rack must be > 0"
        raise ValueError(msg)

    if args.fabric_count <= 0:
        msg = "fabric-count must be > 0"
        raise ValueError(msg)

    homing_factor = 2 if args.homing == "dual" else 1
    access_demand_ports = total_racks * args.ports_per_rack * homing_factor

    access_profile = SwitchProfile(ports=args.access_switch_ports, reserved_ports=args.reserved_access_ports)
    spine_profile = SwitchProfile(ports=args.spine_switch_ports, reserved_ports=args.reserved_spine_ports)
    l2_profile = SwitchProfile(ports=args.l2_switch_ports, reserved_ports=args.reserved_l2_ports)

    results: list[FabricResult] = []

    methods: list[Method]
    if args.method == "all":
        methods = ["tor", "middle", "mixed"]
    else:
        methods = [args.method]

    for method in methods:
        if method == "tor":
            results.append(
                calculate_tor_or_middle(
                    method="tor",
                    total_racks=total_racks,
                    access_demand_ports=access_demand_ports,
                    access_profile=access_profile,
                    spine_profile=spine_profile,
                    oversub=oversub,
                    spare_ratio=args.spare_ratio,
                    racks_per_access_switch=args.tor_racks_per_switch,
                    min_spines=args.min_spines,
                    fabrics=args.fabric_count,
                )
            )
        elif method == "middle":
            results.append(
                calculate_tor_or_middle(
                    method="middle",
                    total_racks=total_racks,
                    access_demand_ports=access_demand_ports,
                    access_profile=access_profile,
                    spine_profile=spine_profile,
                    oversub=oversub,
                    spare_ratio=args.spare_ratio,
                    racks_per_access_switch=args.middle_racks_per_switch,
                    min_spines=args.min_spines,
                    fabrics=args.fabric_count,
                )
            )
        elif method == "mixed":
            results.append(
                calculate_mixed(
                    total_racks=total_racks,
                    access_demand_ports=access_demand_ports,
                    leaf_profile=access_profile,
                    l2_profile=l2_profile,
                    spine_profile=spine_profile,
                    oversub=oversub,
                    spare_ratio=args.spare_ratio,
                    tor_racks_per_switch=args.tor_racks_per_switch,
                    middle_racks_per_switch=args.middle_racks_per_switch,
                    mixed_tor_share=args.mixed_tor_share,
                    min_spines=args.min_spines,
                    fabrics=args.fabric_count,
                )
            )

    print_summary(rooms, access_demand_ports, results)


if __name__ == "__main__":
    main()
