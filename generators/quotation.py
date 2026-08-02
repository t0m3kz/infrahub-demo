"""Generator: DC-fabric sizing/pricing for a CustomerQuotationDC.

Reads the requesting CustomerQuotationDC plus its rooms (the actual sizing
unit — see schemas/extensions/quotation/quotation.yml's module docstring)
and the live device catalog (DcimDeviceType, TemplateDcimPhysicalDevice —
see queries/quotation/add_quotation.gql), runs the same sizing algorithm
scripts/recommend_dc_design.py uses offline for a single speed
(generators/helpers/quotation.py), generalized here to multiple port
speeds per room, and writes the result into:

- CustomerQuotation.estimated_total_cost + CustomerQuotationLineItem
  children — DC-wide totals: one "leaf" row per port speed actually used
  anywhere (tagged with `speed`), plus single spine/super-spine/
  border-leaf/firewall/load-balancer rows sized from the summed leaf count.
- CustomerQuotationProposedDesign (one per quotation, cardinality one):
  recommended_switch_vendor. The recommended DC/pod layout is logged but not
  persisted (DC_SIZE_LAYOUTS/POD_LAYOUTS in generators/models.py are plain
  Python dicts, not Infrahub nodes, since TopologyDataCenterDesign/
  TopologyPodDesign were retired in favor of TopologyDataCenter.size /
  TopologyPod.layout Dropdowns).
- CustomerQuotationProposedPod children of that design (one per
  quotation.pod_count, each mapped 1:1 to a room — see
  generators/helpers/quotation.py's build_room_pods for the exact
  room-to-pod mismatch policy): each pod's own leaf_count/spine_count/
  recommended_pod_layout, sized from its own room's port/rack counts,
  independent of every other pod.
- CustomerQuotationProposedRack children of each pod (one per compute/
  storage rack declared on that pod's own room): each rack tagged
  compute/storage and linked to that same room.

Idempotent throughout: CustomerQuotationLineItem is upserted by (quotation,
role, speed); CustomerQuotationProposedPod/-Rack have no human_friendly_id
(they're generator-only, never referenced by name in bootstrap data), so
this generator queries existing rows by their uniqueness_constraints
(design+index / pod+index) and passes the existing id back into create()
for an id-pinned upsert — same pattern generators/devices.py uses for
devices with server-generated names.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator

from utils.data_cleaning import clean_data

from .helpers.quotation import (
    PORT_SPEEDS,
    Recommender,
    TierResult,
    assign_racks_to_rooms,
    build_multi_speed_fabric,
    build_room_pods,
    device_templates_from_graphql,
    recommend_design,
    recommend_pod_design,
    validate_room_capacity,
)
from .logger import FailOnErrorLoggerMixin
from .models import DC_SIZE_LAYOUTS, POD_LAYOUTS
from .protocols import (
    CustomerQuotationDC,
    CustomerQuotationLineItem,
    CustomerQuotationProposedDesign,
    CustomerQuotationProposedPod,
    CustomerQuotationProposedRack,
)

_DC_SIZE_LAYOUT_ENTRIES = [{"name": name, **fields} for name, fields in DC_SIZE_LAYOUTS.items()]
_POD_LAYOUT_ENTRIES = [{"name": name, **fields} for name, fields in POD_LAYOUTS.items()]


class QuotationGenerator(FailOnErrorLoggerMixin, InfrahubGenerator):
    """Compute a DC-fabric sizing/pricing recommendation for one CustomerQuotationDC."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        quotations = cleaned.get("CustomerQuotationDC", [])
        if not quotations:
            self.logger.error("No CustomerQuotationDC found in query response")
            return
        quotation = quotations[0]
        quotation_id: str = quotation["id"]
        quotation_name: str = quotation["name"]

        self.logger.info(f"Processing quotation: {quotation_name}")

        device_types = cleaned.get("DcimDeviceType", [])
        prices = {dt["id"]: dt["unit_price"] for dt in device_types if dt.get("unit_price")}
        manufacturers = {dt["id"]: dt["manufacturer"]["name"] for dt in device_types if dt.get("manufacturer")}
        templates = device_templates_from_graphql(cleaned.get("TemplateDcimPhysicalDevice", []), manufacturers)
        rec = Recommender(templates=templates, prices=prices)

        pods: int = quotation.get("pod_count") or 1
        rooms: list[dict] = quotation.get("rooms", [])
        switch_vendor: str | None = quotation.get("preferred_switch_vendor") or None
        firewall_vendor: str | None = quotation.get("preferred_firewall_vendor") or None
        lb_vendor: str | None = quotation.get("preferred_load_balancer_vendor") or None
        topology_strategy = quotation.get("topology_strategy") or "auto"
        if topology_strategy not in {"auto", "back_to_back", "classic_3tier"}:
            topology_strategy = "auto"
        growth_buffer_percent = quotation.get("growth_buffer_percent") or 0
        growth_buffer_percent = max(0, min(200, int(growth_buffer_percent)))
        room_assignment_strategy = "round_robin"
        for room in rooms:
            strategy = room.get("rack_assignment_strategy")
            if strategy in {"round_robin", "sequential", "dedicated_room_per_pod"}:
                room_assignment_strategy = strategy
                break

        if len(rooms) > pods:
            self.logger.warning(
                f"Quotation {quotation_name}: {len(rooms)} rooms declared but pod_count={pods} — "
                f"{len(rooms) - pods} room(s) won't be sized"
            )

        room_check = validate_room_capacity(rooms)
        if not room_check["fits"]:
            self.logger.warning(
                f"Quotation {quotation_name}: declared rooms only fit "
                f"{room_check['total_room_capacity']} racks, but "
                f"{room_check['required_racks']} compute+storage racks were requested "
                f"(short by {room_check['shortfall']})"
            )

        room_pods = build_room_pods(
            rec,
            rooms,
            pods,
            manufacturer=switch_vendor,
            assignment_strategy=room_assignment_strategy,
            growth_buffer_percent=growth_buffer_percent,
        )

        # DC-wide totals: sum every pod's own room port_counts, then price
        # the combined fabric once — mirrors build_room_pods's own per-pod
        # build_multi_speed_fabric call, just fed the DC-wide sum.
        dc_port_counts = {speed: 0 for speed in PORT_SPEEDS.values()}
        for pod in room_pods:
            for speed, count in pod["port_counts"].items():
                dc_port_counts[speed] += count

        leaf_results, spine_result, super_spine_result, border_leaf_result = build_multi_speed_fabric(
            rec,
            dc_port_counts,
            pods,
            manufacturer=switch_vendor,
            topology_strategy=topology_strategy,
        )
        if not leaf_results:
            self.logger.error(
                f"Quotation {quotation_name}: no leaf/tor device template found for any requested port "
                "speed — cannot size this fabric"
            )
            return

        firewall_result = rec.cheapest_pair("firewall", firewall_vendor)
        load_balancer_result = rec.cheapest_pair("load-balancer", lb_vendor)
        leaf_speeds = [speed for speed, count in dc_port_counts.items() if count > 0]
        tiers_with_speed: list[tuple[TierResult, str | None]] = [
            *zip(leaf_results, leaf_speeds),
            (spine_result, None),
            (super_spine_result, None),
            (border_leaf_result, None),
            (firewall_result, None),
            (load_balancer_result, None),
        ]

        chosen_vendor = switch_vendor or manufacturers.get(leaf_results[0].device_type_id)
        estimated_total_cost = sum(t.total_cost for t, _ in tiers_with_speed if t.total_cost is not None)

        dc_design = recommend_design(
            _DC_SIZE_LAYOUT_ENTRIES,
            spine_count=spine_result.count,
            super_spine_count=super_spine_result.count,
            border_leaf_count=border_leaf_result.count,
        )

        quotation_obj = await self.client.get(kind=CustomerQuotationDC, id=quotation_id)
        quotation_obj.estimated_total_cost.value = int(estimated_total_cost)
        await quotation_obj.save(allow_upsert=True)

        await self._save_line_items(quotation_id, tiers_with_speed)

        design_id = await self._save_proposed_design(quotation_id, chosen_vendor)
        await self._save_proposed_pods(design_id, room_pods, _POD_LAYOUT_ENTRIES)

        self.logger.info(
            f"Quotation {quotation_name}: estimated_total_cost=${estimated_total_cost:,.0f}, "
            f"dc_design={dc_design['name'] if dc_design else 'none'}, "
            f"pods={len(room_pods)}"
        )

    async def _save_line_items(self, quotation_id: str, tiers_with_speed: list[tuple[TierResult, str | None]]) -> None:
        batch = await self.client.create_batch()
        for tier, speed in tiers_with_speed:
            obj = await self.client.create(
                kind=CustomerQuotationLineItem,
                data={
                    "quotation": {"id": quotation_id},
                    "role": tier.tier,
                    "speed": speed,
                    "quantity": tier.count or 1,
                    "unit_price": tier.unit_price,
                    "total_cost": tier.total_cost,
                    "device_type": {"id": tier.device_type_id} if tier.device_type_id else None,
                },
            )
            batch.add(task=obj.save, allow_upsert=True, node=obj)

        async for node, error in batch.execute():
            if error:
                self.logger.error(f"Failed to save line item [{node.get_kind()}]: {error}")
                raise ValidationError(str(error))

    async def _save_proposed_design(self, quotation_id: str, chosen_vendor: str | None) -> str:
        """Upsert the single CustomerQuotationProposedDesign for this quotation.

        No human_friendly_id (see module docstring) — query by its
        (cardinality-one) `quotation` relationship first, then pass the
        existing id (if any) into create() for an id-pinned upsert.
        """
        existing = await self.client.filters(kind=CustomerQuotationProposedDesign, quotation__ids=[quotation_id])
        design_obj = await self.client.create(
            kind=CustomerQuotationProposedDesign,
            data={
                **({"id": existing[0].id} if existing else {}),
                "quotation": {"id": quotation_id},
            },
        )
        await design_obj.save(allow_upsert=True)
        return design_obj.id

    async def _save_proposed_pods(self, design_id: str, room_pods: list[dict], pod_designs: list[dict]) -> None:
        existing_pods = await self.client.filters(kind=CustomerQuotationProposedPod, design__ids=[design_id])
        existing_by_index = {pod.index.value: pod for pod in existing_pods}

        batch = await self.client.create_batch()
        objs_by_index: dict[int, Any] = {}
        for pod in room_pods:
            pod_design = recommend_pod_design(pod_designs, leaf_count=pod["leaf_count"], spine_count=pod["spine_count"])
            existing = existing_by_index.get(pod["index"])
            obj = await self.client.create(
                kind=CustomerQuotationProposedPod,
                data={
                    **({"id": existing.id} if existing else {}),
                    "design": {"id": design_id},
                    "index": pod["index"],
                    "compute_rack_share": pod["compute_rack_share"],
                    "storage_rack_share": pod["storage_rack_share"],
                    "leaf_count": pod["leaf_count"],
                    "spine_count": pod["spine_count"],
                    "recommended_pod_design": {"id": pod_design["id"]} if pod_design else None,
                },
            )
            batch.add(task=obj.save, allow_upsert=True, node=obj)
            objs_by_index[pod["index"]] = obj

        async for node, error in batch.execute():
            if error:
                self.logger.error(f"Failed to save proposed pod [{node.get_kind()}]: {error}")
                raise ValidationError(str(error))

        # Read ids only after execute() has run — a freshly created node's id
        # is None until save() actually persists it server-side.
        pod_id_by_index: dict[int, str] = {index: obj.id for index, obj in objs_by_index.items()}

        for pod in room_pods:
            # Each pod is already mapped to exactly one room by build_room_pods
            # — its racks all go there, no round-robin across the full room
            # list needed at this layer.
            assigned_room = {"id": pod["room_id"]} if pod["room_id"] else None
            racks = assign_racks_to_rooms(
                pod["compute_rack_share"],
                pod["storage_rack_share"],
                rooms=[assigned_room] if assigned_room else [],
                assignment_strategy=pod.get("rack_assignment_strategy", "round_robin"),
            )
            await self._save_proposed_racks(pod_id_by_index[pod["index"]], racks)

    async def _save_proposed_racks(self, pod_id: str, racks: list[dict]) -> None:
        if not racks:
            return
        existing_racks = await self.client.filters(kind=CustomerQuotationProposedRack, pod__ids=[pod_id])
        existing_by_index = {rack.index.value: rack for rack in existing_racks}

        batch = await self.client.create_batch()
        for rack in racks:
            existing = existing_by_index.get(rack["index"])
            obj = await self.client.create(
                kind=CustomerQuotationProposedRack,
                data={
                    **({"id": existing.id} if existing else {}),
                    "pod": {"id": pod_id},
                    "index": rack["index"],
                    "rack_type": rack["rack_type"],
                    "room": {"id": rack["room_id"]} if rack["room_id"] else None,
                },
            )
            batch.add(task=obj.save, allow_upsert=True, node=obj)

        async for node, error in batch.execute():
            if error:
                self.logger.error(f"Failed to save proposed rack [{node.get_kind()}]: {error}")
                raise ValidationError(str(error))
