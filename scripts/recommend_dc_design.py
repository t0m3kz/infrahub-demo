#!/usr/bin/env python3
"""Recommend a full, redundant DC fabric build-out for a given server fan-out.

Reads device port counts/speeds directly from the bootstrap device templates
(data/bootstrap/10_physical_devices_templates_*.yaml) and unit_price from
data/bootstrap/06_device_types.yml — no live Infrahub connection needed.
The sizing algorithm itself lives in generators/helpers/quotation.py, shared
with the add_quotation generator (generators/quotation.py), which runs the
same computation against live Infrahub data via GraphQL instead of local
YAML files. This script only owns the YAML-loading and CLI/reporting layers;
device_type is keyed by its `name` field, treated as an id (unique in the
bootstrap YAML) — the generator instead keys by the real Infrahub node id.

Given a target server count + required customer-facing port speed, sizes the
whole fabric bottom-up, cheapest device type first — see
generators/helpers/quotation.py's docstring for the tier-by-tier algorithm.

Finally, checks the computed spine/super-spine/border-leaf counts against the
4 DC_SIZE_LAYOUTS size templates (generators/models.py)
and recommends the smallest (cheapest) one whose caps aren't exceeded.

Vendor comparison (--compare-vendors): switch-fabric tiers (leaf, spine,
super-spine, border-leaf) are re-priced with each candidate restricted to a
single manufacturer (a real fabric is normally single-vendor for
support/maintenance reasons), and the cheapest-per-tier build is printed for
every manufacturer that can cover all required tiers, cheapest total first.
Firewall/load-balancer are priced independently of switch vendor in both
modes — no switch manufacturer in this project also makes firewalls/load
balancers (those come from Check Point/Juniper/F5/Citrix), so there is no
single-vendor constraint to apply there.

--firewall-vendor/--lb-vendor pin those two tiers to a preferred manufacturer
(e.g. an existing support contract) instead of always picking the cheapest —
they apply in both single and --compare-vendors mode, independently of the
switch-fabric vendor.

KNOWN GAP: this CLI only sizes ONE port speed per run (--speed). The
add_quotation generator supports multiple port speeds in a single
quotation (one independent leaf tier per speed, summed for spine sizing —
see generators/helpers/quotation.py's build_multi_speed_fabric/
build_room_pods), because CustomerQuotationRoom now carries its own
port_count_10g/25g/40g/100g/400g fields. This CLI hasn't been extended to
accept multiple --speed/--servers pairs in one invocation; run it once per
speed and sum manually if you need a multi-speed estimate offline.

Usage:
    uv run python scripts/recommend_dc_design.py --servers 40 --speed 100gbase-x-qsfp28
    uv run python scripts/recommend_dc_design.py --servers 200 --speed 25gbase-x-sfp28 --pods 4
    uv run python scripts/recommend_dc_design.py --servers 40 --speed 100gbase-x-qsfp28 --compare-vendors
    uv run python scripts/recommend_dc_design.py --servers 40 --speed 100gbase-x-qsfp28 \\
        --firewall-vendor "Check Point" --lb-vendor "F5 Networks"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generators.helpers.quotation import (  # noqa: E402
    SWITCH_FABRIC_ROLES,
    DeviceTemplate,
    PortGroup,
    Recommender,
    TierResult,
    build_switch_fabric,
    recommend_design,
    recommend_pod_design,
)
from generators.models import DC_SIZE_LAYOUTS, POD_LAYOUTS  # noqa: E402

BOOTSTRAP_DIR = Path(__file__).resolve().parent.parent / "data" / "bootstrap"


def _expand_range_count(name: str) -> int:
    """Count physical ports implied by a template_name/name like 'Ethernet[1-24]/1'.

    Falls back to 1 if there's no [a-b] range (a single named port). Only
    needed here — the generator's GraphQL data arrives pre-expanded (one
    interface node per port), so generators/helpers/quotation.py never needs
    this.
    """
    matches = re.findall(r"\[(\d+)-(\d+)\]", name)
    if not matches:
        return 1
    return sum(int(hi) - int(lo) + 1 for lo, hi in matches)


def load_unit_prices() -> dict[str, float]:
    path = BOOTSTRAP_DIR / "06_device_types.yml"
    data = yaml.safe_load(path.read_text())
    return {entry["name"]: float(entry["unit_price"]) for entry in data["spec"]["data"] if entry.get("unit_price")}


def load_manufacturers() -> dict[str, str]:
    path = BOOTSTRAP_DIR / "06_device_types.yml"
    data = yaml.safe_load(path.read_text())
    return {entry["name"]: entry["manufacturer"] for entry in data["spec"]["data"] if entry.get("manufacturer")}


def load_device_templates() -> list[DeviceTemplate]:
    """Device_type `name` is treated as the id — unique in the bootstrap
    YAML, and this script only ever displays it as a label anyway."""
    manufacturers = load_manufacturers()
    templates: list[DeviceTemplate] = []
    for path in sorted(BOOTSTRAP_DIR.glob("10_physical_devices_templates_*.yaml")):
        data = yaml.safe_load(path.read_text())
        for device in data.get("spec", {}).get("data", []):
            role = device.get("role")
            device_type = device.get("device_type")
            if not role or not device_type:
                continue
            ports: dict[str, PortGroup] = {}
            for iface in device.get("interfaces", {}).get("data", []):
                iface_role = iface.get("role")
                if not iface_role:
                    continue
                count = _expand_range_count(iface.get("name", ""))
                speed = iface.get("interface_type", "")
                if iface_role in ports:
                    ports[iface_role].count += count
                else:
                    ports[iface_role] = PortGroup(count=count, speed=speed)
            templates.append(
                DeviceTemplate(
                    device_type_id=device_type,
                    role=role,
                    manufacturer=manufacturers.get(device_type),
                    ports=ports,
                )
            )
    return templates


def load_dc_designs() -> list[dict]:
    """DC_SIZE_LAYOUTS (generators/models.py), same numbers the retired
    TopologyDataCenterDesign bootstrap node used to carry. Dict insertion
    order (S, M, L, XL) is preserved by Python 3.7+, but recommend_design()
    doesn't rely on that ordering anyway."""
    return [{"name": name, **fields} for name, fields in DC_SIZE_LAYOUTS.items()]


def load_pod_designs() -> list[dict]:
    """POD_LAYOUTS (generators/models.py), same numbers the retired
    TopologyPodDesign bootstrap node used to carry."""
    return [{"name": name, **fields} for name, fields in POD_LAYOUTS.items()]


def _print_tier(result: TierResult) -> None:
    if result.device_type_id is None:
        print(f"  {result.tier:<14} no candidate found")
        return
    price = f"${result.unit_price:,.0f}" if result.unit_price is not None else "n/a"
    total = f"${result.total_cost:,.0f}" if result.total_cost is not None else "n/a"
    print(f"  {result.tier:<14} {result.count:>3}x {result.device_type_id:<26} @ {price:>10} = {total:>12}")


def _report(tiers: list[TierResult], firewall_result: TierResult, load_balancer_result: TierResult) -> float:
    all_tiers = [*tiers, firewall_result, load_balancer_result]
    for t in all_tiers:
        _print_tier(t)
    total = sum(t.total_cost for t in all_tiers if t.total_cost is not None)
    print(f"\n  {'TOTAL':<14} {'':>32} {'':>10}   ${total:,.0f}")
    return total


def run_single(
    rec: Recommender,
    servers: int,
    speed: str,
    pods: int,
    firewall_vendor: str | None = None,
    lb_vendor: str | None = None,
) -> None:
    tiers = build_switch_fabric(rec, servers, speed, pods, manufacturer=None)
    if tiers[0].device_type_id is None:
        print(f"No leaf/tor device template found with a customer-facing '{speed}' port.")
        return
    firewall_result = rec.cheapest_pair("firewall", firewall_vendor)
    load_balancer_result = rec.cheapest_pair("load-balancer", lb_vendor)
    if firewall_result.device_type_id is None and firewall_vendor:
        print(f"No firewall device template found for vendor '{firewall_vendor}'.")
        return
    if load_balancer_result.device_type_id is None and lb_vendor:
        print(f"No load-balancer device template found for vendor '{lb_vendor}'.")
        return

    print(f"Fabric build-out for {servers} servers @ {speed} ({pods} pod(s)):\n")
    _report(tiers, firewall_result, load_balancer_result)

    leaf_result, spine_result, super_spine_result, border_leaf_result = tiers
    design = recommend_design(
        load_dc_designs(),
        spine_count=spine_result.count,
        super_spine_count=super_spine_result.count,
        border_leaf_count=border_leaf_result.count,
    )
    if design:
        print(f"\nRecommended DC size: {design['name']}")
    else:
        print("\nNo existing S/M/L/XL design fits this fabric's tier counts — needs a custom design.")

    pod_design = recommend_pod_design(load_pod_designs(), leaf_count=leaf_result.count, spine_count=spine_result.count)
    if pod_design:
        print(f"Recommended pod layout: {pod_design['name']}")
    else:
        print("No existing pod layout fits this pod's leaf/spine counts — needs a custom layout.")


def run_compare_vendors(
    rec: Recommender,
    servers: int,
    speed: str,
    pods: int,
    firewall_vendor: str | None = None,
    lb_vendor: str | None = None,
) -> None:
    manufacturers = rec.manufacturers_for(SWITCH_FABRIC_ROLES)
    if not manufacturers:
        print("No switch manufacturers found in bootstrap device templates.")
        return

    firewall_result = rec.cheapest_pair("firewall", firewall_vendor)
    load_balancer_result = rec.cheapest_pair("load-balancer", lb_vendor)
    if firewall_result.device_type_id is None and firewall_vendor:
        print(f"No firewall device template found for vendor '{firewall_vendor}'.")
        return
    if load_balancer_result.device_type_id is None and lb_vendor:
        print(f"No load-balancer device template found for vendor '{lb_vendor}'.")
        return
    fixed_extra_cost = sum(t.total_cost for t in (firewall_result, load_balancer_result) if t.total_cost is not None)

    results: list[tuple[str, list[TierResult], float]] = []
    for manufacturer in manufacturers:
        tiers = build_switch_fabric(rec, servers, speed, pods, manufacturer=manufacturer)
        if tiers[0].device_type_id is None or any(t.device_type_id is None for t in tiers if t.tier != "super-spine"):
            continue  # this vendor can't cover every required tier at this speed
        switch_cost = sum(t.total_cost for t in tiers if t.total_cost is not None)
        results.append((manufacturer, tiers, switch_cost + fixed_extra_cost))

    if not results:
        print(f"No single manufacturer can cover every required tier for '{speed}'.")
        return

    results.sort(key=lambda r: r[2])

    print(f"Vendor comparison for {servers} servers @ {speed} ({pods} pod(s)):\n")
    for manufacturer, tiers, total in results:
        print(f"== {manufacturer} ==")
        _report(tiers, firewall_result, load_balancer_result)
        print()

    best_manufacturer, best_tiers, best_total = results[0]
    print(f"Optimal vendor: {best_manufacturer} — total ${best_total:,.0f}")

    leaf_result, spine_result, super_spine_result, border_leaf_result = best_tiers
    design = recommend_design(
        load_dc_designs(),
        spine_count=spine_result.count,
        super_spine_count=super_spine_result.count,
        border_leaf_count=border_leaf_result.count,
    )
    if design:
        print(f"Recommended DC size: {design['name']}")

    pod_design = recommend_pod_design(load_pod_designs(), leaf_count=leaf_result.count, spine_count=spine_result.count)
    if pod_design:
        print(f"Recommended pod layout: {pod_design['name']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--servers", type=int, required=True, help="Number of end-host servers to connect")
    parser.add_argument("--speed", required=True, help="Required customer-facing port speed, e.g. 100gbase-x-qsfp28")
    parser.add_argument("--pods", type=int, default=1, help="Number of pods sharing a super-spine tier (default: 1)")
    parser.add_argument(
        "--compare-vendors",
        action="store_true",
        help="Price the switch fabric (leaf/spine/super-spine/border-leaf) separately per manufacturer "
        "and report the cheapest single-vendor build.",
    )
    parser.add_argument(
        "--firewall-vendor",
        default=None,
        help="Pin the firewall pick to this manufacturer (e.g. 'Check Point', 'Juniper') "
        "instead of the cheapest available.",
    )
    parser.add_argument(
        "--lb-vendor",
        default=None,
        help="Pin the load-balancer pick to this manufacturer (e.g. 'F5 Networks', 'Citrix') "
        "instead of the cheapest available.",
    )
    args = parser.parse_args()

    rec = Recommender(templates=load_device_templates(), prices=load_unit_prices())
    if args.compare_vendors:
        run_compare_vendors(rec, args.servers, args.speed, args.pods, args.firewall_vendor, args.lb_vendor)
    else:
        run_single(rec, args.servers, args.speed, args.pods, args.firewall_vendor, args.lb_vendor)


if __name__ == "__main__":
    main()
