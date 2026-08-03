"""Generator: office/campus sizing quotation.

Computes AP/access/distribution/core quantities using the same sizing model as
scripts/campus_office_network_calculator.py and writes results into generic
CustomerQuotation fields:
- estimated_total_cost
- CustomerQuotationLineItem children
"""

from __future__ import annotations

import math
from typing import Any, TypedDict, cast

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator

from utils.data_cleaning import clean_data

from ..logger import FailOnErrorLoggerMixin
from ..protocols import (
    CustomerQuotationCampusBuilding,
    CustomerQuotationCampusDesign,
    CustomerQuotationCampusFloor,
    CustomerQuotationLineItem,
    CustomerQuotationOffice,
)


class OfficeLineItem(TypedDict):
    role: str
    quantity: int
    device_type_id: str | None
    unit_price: float | None


class OfficeQuotationGenerator(FailOnErrorLoggerMixin, InfrahubGenerator):
    """Compute office/campus sizing recommendation for one CustomerQuotationOffice."""

    _KIND_PROPOSED_CAMPUS_DESIGN = CustomerQuotationCampusDesign
    _KIND_PROPOSED_CAMPUS_BUILDING = CustomerQuotationCampusBuilding
    _KIND_PROPOSED_CAMPUS_FLOOR = CustomerQuotationCampusFloor

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        quotations = cleaned.get("CustomerQuotationOffice", [])
        if not quotations:
            self.logger.info("No CustomerQuotationOffice found in query response - skipping")
            return

        quotation = quotations[0]
        quotation_id: str = quotation["id"]
        quotation_name: str = quotation["name"]
        self.logger.info(f"Processing office quotation: {quotation_name}")

        buildings: list[dict[str, Any]] = quotation.get("buildings", [])
        if not buildings:
            self.logger.error(f"Quotation {quotation_name}: no buildings defined for office sizing")
            return

        users_per_ap = max(1, int(quotation.get("users_per_ap") or 35))
        m2_per_ap = max(1.0, float(quotation.get("m2_per_ap") or 120.0))
        endpoint_growth_ratio = self._as_ratio(
            quotation.get("endpoint_growth_ratio"),
            default=0.2,
            maximum=5.0,
        )

        access_switch_port_density = max(1, int(quotation.get("access_switch_port_density") or 48))
        access_switch_reserved_ports = max(0, int(quotation.get("access_switch_reserved_ports") or 4))
        access_switch_utilization_target = self._as_ratio(
            quotation.get("access_switch_utilization_target"),
            default=0.85,
            minimum=0.1,
            maximum=1.0,
        )

        distribution_switch_port_density = max(1, int(quotation.get("distribution_switch_port_density") or 48))
        distribution_switch_reserved_ports = max(
            0,
            int(
                quotation.get("dist_switch_reserved_ports") or quotation.get("distribution_switch_reserved_ports") or 6
            ),
        )
        distribution_switch_utilization_target = self._as_ratio(
            quotation.get("dist_switch_util_target") or quotation.get("distribution_switch_utilization_target"),
            default=0.8,
            minimum=0.1,
            maximum=1.0,
        )

        core_router_port_density = max(1, int(quotation.get("core_router_port_density") or 32))
        core_router_reserved_ports = max(0, int(quotation.get("core_router_reserved_ports") or 4))
        core_router_utilization_target = self._as_ratio(
            quotation.get("core_router_utilization_target"),
            default=0.75,
            minimum=0.1,
            maximum=1.0,
        )

        uplinks_per_access_switch = max(1, int(quotation.get("uplinks_per_access_switch") or 2))
        min_distribution_switches = max(1, int(quotation.get("min_distribution_switches") or 2))
        dist_to_core_uplinks_per_distribution = max(
            1,
            int(
                quotation.get("dist_core_uplinks_per_dist")
                or quotation.get("dist_to_core_uplinks_per_distribution")
                or 2
            ),
        )
        min_core_routers = max(1, int(quotation.get("min_core_routers") or 2))

        wifi_mbps_per_user = max(0.0, float(quotation.get("wifi_mbps_per_user") or 6.0))
        conference_room_mbps = max(0.0, float(quotation.get("conference_room_mbps") or 8.0))
        printer_mbps = max(0.0, float(quotation.get("printer_mbps") or 0.5))
        user_concurrency = self._as_ratio(quotation.get("user_concurrency"), default=0.35)
        conference_concurrency = self._as_ratio(quotation.get("conference_concurrency"), default=0.3)
        printer_concurrency = self._as_ratio(quotation.get("printer_concurrency"), default=0.1)

        access_to_distribution_oversub = max(0.1, float(quotation.get("access_to_distribution_oversub") or 4.0))
        distribution_to_core_oversub = max(0.1, float(quotation.get("distribution_to_core_oversub") or 2.0))

        access_effective_ports = self._effective_ports(
            port_density=access_switch_port_density,
            reserved_ports=access_switch_reserved_ports,
            utilization_target=access_switch_utilization_target,
        )
        distribution_effective_ports = self._effective_ports(
            port_density=distribution_switch_port_density,
            reserved_ports=distribution_switch_reserved_ports,
            utilization_target=distribution_switch_utilization_target,
        )
        core_effective_ports = self._effective_ports(
            port_density=core_router_port_density,
            reserved_ports=core_router_reserved_ports,
            utilization_target=core_router_utilization_target,
        )

        total_access_points = 0
        total_access_switches = 0
        total_distribution_switches = 0
        total_distribution_to_core_uplinks = 0
        total_demand_mbps = 0.0
        total_floors = 0
        building_plans: list[dict[str, Any]] = []

        for building in buildings:
            floors = max(1, int(building.get("floors") or 1))
            people_per_floor = max(0, int(building.get("people_per_floor") or 0))
            area_m2_per_floor = max(0.0, float(building.get("area_m2_per_floor") or 0.0))
            conference_rooms_per_floor = max(0, int(building.get("conference_rooms_per_floor") or 0))
            printers_per_floor = max(0, int(building.get("printers_per_floor") or 0))
            building_name = building.get("name") or f"building-{len(building_plans) + 1}"

            ap_by_people = math.ceil(people_per_floor / users_per_ap) if people_per_floor > 0 else 0
            ap_by_area = math.ceil(area_m2_per_floor / m2_per_ap) if area_m2_per_floor > 0 else 0
            floor_aps = max(ap_by_people, ap_by_area)

            floor_wired_endpoints = floor_aps + conference_rooms_per_floor + printers_per_floor
            floor_wired_design = math.ceil(floor_wired_endpoints * (1 + endpoint_growth_ratio))
            floor_demand_mbps = (
                people_per_floor * wifi_mbps_per_user * user_concurrency
                + conference_rooms_per_floor * conference_room_mbps * conference_concurrency
                + printers_per_floor * printer_mbps * printer_concurrency
            )

            access_switches_per_floor = (
                math.ceil(floor_wired_design / access_effective_ports) if floor_wired_design > 0 else 0
            )
            access_switches = access_switches_per_floor * floors

            access_to_distribution_uplinks = access_switches * uplinks_per_access_switch
            distribution_needed_by_ports = (
                math.ceil(access_to_distribution_uplinks / distribution_effective_ports)
                if access_to_distribution_uplinks > 0
                else 0
            )
            distribution_switches = max(
                min_distribution_switches if access_switches > 0 else 0,
                distribution_needed_by_ports,
            )
            distribution_to_core_uplinks = distribution_switches * dist_to_core_uplinks_per_distribution

            total_users = people_per_floor * floors
            total_conference = conference_rooms_per_floor * floors
            total_printers = printers_per_floor * floors
            demand_mbps = (
                total_users * wifi_mbps_per_user * user_concurrency
                + total_conference * conference_room_mbps * conference_concurrency
                + total_printers * printer_mbps * printer_concurrency
            )

            _ = demand_mbps / access_to_distribution_oversub
            _ = demand_mbps / distribution_to_core_oversub

            total_access_points += floor_aps * floors
            total_access_switches += access_switches
            total_distribution_switches += distribution_switches
            total_distribution_to_core_uplinks += distribution_to_core_uplinks
            total_demand_mbps += demand_mbps
            total_floors += floors

            floor_plans = [
                {
                    "index": floor_index,
                    "access_points": floor_aps,
                    "wired_endpoints": floor_wired_design,
                    "access_switches": access_switches_per_floor,
                    "demand_mbps": floor_demand_mbps,
                }
                for floor_index in range(1, floors + 1)
            ]
            building_plans.append(
                {
                    "id": building.get("id"),
                    "name": building_name,
                    "floors": floors,
                    "access_points": floor_aps * floors,
                    "access_switches": access_switches,
                    "distribution_switches": distribution_switches,
                    "demand_mbps": demand_mbps,
                    "floors_breakdown": floor_plans,
                }
            )

        total_core_routers = (
            max(min_core_routers, math.ceil(total_distribution_to_core_uplinks / core_effective_ports))
            if total_distribution_to_core_uplinks > 0
            else 0
        )

        device_types = cleaned.get("DcimDeviceType", [])
        prices = {dt["id"]: dt["unit_price"] for dt in device_types if dt.get("unit_price") is not None}
        names = {dt["id"]: dt.get("name") for dt in device_types}
        manufacturers = {
            dt["id"]: dt.get("manufacturer", {}).get("name") for dt in device_types if dt.get("manufacturer")
        }
        templates = cleaned.get("TemplateDcimPhysicalDevice", [])
        preferred_vendor = quotation.get("preferred_switch_vendor") or None

        ap_device_type_id, ap_unit_price = self._pick_cheapest_access_point(
            names=names,
            prices=prices,
            manufacturers=manufacturers,
            preferred_vendor=preferred_vendor,
        )
        access_switch_type_id, access_switch_unit_price = self._pick_cheapest_by_template_roles(
            template_rows=templates,
            prices=prices,
            manufacturers=manufacturers,
            roles={"leaf", "tor", "access-leaf", "l2-leaf"},
            preferred_vendor=preferred_vendor,
        )
        distribution_switch_type_id, distribution_switch_unit_price = self._pick_cheapest_by_template_roles(
            template_rows=templates,
            prices=prices,
            manufacturers=manufacturers,
            roles={"spine", "border-spine"},
            preferred_vendor=preferred_vendor,
        )
        core_router_type_id, core_router_unit_price = self._pick_cheapest_by_template_roles(
            template_rows=templates,
            prices=prices,
            manufacturers=manufacturers,
            roles={"super-spine", "hyper-spine", "edge"},
            preferred_vendor=preferred_vendor,
        )

        office_items: list[OfficeLineItem] = [
            {
                "role": "access-point",
                "quantity": total_access_points,
                "device_type_id": ap_device_type_id,
                "unit_price": ap_unit_price,
            },
            {
                "role": "access-switch",
                "quantity": total_access_switches,
                "device_type_id": access_switch_type_id,
                "unit_price": access_switch_unit_price,
            },
            {
                "role": "distribution-switch",
                "quantity": total_distribution_switches,
                "device_type_id": distribution_switch_type_id,
                "unit_price": distribution_switch_unit_price,
            },
            {
                "role": "core-router",
                "quantity": total_core_routers,
                "device_type_id": core_router_type_id,
                "unit_price": core_router_unit_price,
            },
        ]

        estimated_total_cost = 0.0
        for item in office_items:
            unit_price = item["unit_price"]
            quantity = item["quantity"]
            if unit_price is not None and quantity > 0:
                estimated_total_cost += unit_price * quantity

        quotation_obj: Any = await self.client.get(kind=CustomerQuotationOffice, id=quotation_id)
        quotation_obj.estimated_total_cost.value = int(estimated_total_cost)
        await quotation_obj.save(allow_upsert=True)

        await self._save_office_line_items(quotation_id=quotation_id, items=office_items)
        await self._save_proposed_campus_design(
            quotation_id=quotation_id,
            building_plans=building_plans,
            total_buildings=len(building_plans),
            total_floors=total_floors,
            total_access_points=total_access_points,
            total_access_switches=total_access_switches,
            total_distribution_switches=total_distribution_switches,
            total_core_routers=total_core_routers,
            total_demand_mbps=total_demand_mbps,
        )

        self.logger.info(
            f"Office quotation {quotation_name}: AP={total_access_points}, access={total_access_switches}, "
            f"distribution={total_distribution_switches}, core={total_core_routers}, buildings={len(building_plans)}, "
            f"estimated_total_cost=${estimated_total_cost:,.0f}, demand={total_demand_mbps / 1000:.2f}Gbps"
        )

    @staticmethod
    def _as_ratio(
        value: Any,
        default: float,
        minimum: float = 0.0,
        maximum: float = 1.0,
    ) -> float:
        """Parse ratio values from either legacy decimal form or integer percent form.

        Examples:
        - 0.35 -> 0.35
        - 35 -> 0.35
        """
        if value is None:
            parsed = default
        else:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = default

        if parsed > 1.0:
            parsed = parsed / 100.0

        return min(maximum, max(minimum, parsed))

    @staticmethod
    def _effective_ports(port_density: int, reserved_ports: int, utilization_target: float) -> int:
        usable = (port_density - reserved_ports) * utilization_target
        return max(1, math.floor(usable))

    @staticmethod
    def _pick_cheapest_by_template_roles(
        template_rows: list[dict[str, Any]],
        prices: dict[str, float],
        manufacturers: dict[str, str | None],
        roles: set[str],
        preferred_vendor: str | None,
    ) -> tuple[str | None, float | None]:
        candidate_ids: set[str] = set()
        for row in template_rows:
            role = row.get("role")
            device_type = row.get("device_type") or {}
            device_type_id = device_type.get("id")
            if role in roles and device_type_id:
                candidate_ids.add(device_type_id)

        best_id: str | None = None
        best_price: float | None = None
        for device_type_id in candidate_ids:
            price = prices.get(device_type_id)
            if price is None:
                continue
            if preferred_vendor and manufacturers.get(device_type_id) != preferred_vendor:
                continue
            if best_price is None or price < best_price:
                best_price = price
                best_id = device_type_id

        if best_id is None and preferred_vendor:
            for device_type_id in candidate_ids:
                price = prices.get(device_type_id)
                if price is None:
                    continue
                if best_price is None or price < best_price:
                    best_price = price
                    best_id = device_type_id

        return best_id, best_price

    @staticmethod
    def _pick_cheapest_access_point(
        names: dict[str, str | None],
        prices: dict[str, float],
        manufacturers: dict[str, str | None],
        preferred_vendor: str | None,
    ) -> tuple[str | None, float | None]:
        def _is_ap(name: str) -> bool:
            upper = name.upper()
            if "APIC" in upper:
                return False
            return (
                "AXI" in upper
                or upper.startswith("AP")
                or "-AP" in upper
                or "AP-" in upper
                or "ACCESS POINT" in upper
                or "WIRELESS AP" in upper
            )

        candidates = [device_type_id for device_type_id, name in names.items() if name and _is_ap(name)]
        best_id: str | None = None
        best_price: float | None = None

        for device_type_id in candidates:
            price = prices.get(device_type_id)
            if price is None:
                continue
            if preferred_vendor and manufacturers.get(device_type_id) != preferred_vendor:
                continue
            if best_price is None or price < best_price:
                best_price = price
                best_id = device_type_id

        if best_id is None and preferred_vendor:
            for device_type_id in candidates:
                price = prices.get(device_type_id)
                if price is None:
                    continue
                if best_price is None or price < best_price:
                    best_price = price
                    best_id = device_type_id

        return best_id, best_price

    async def _save_office_line_items(self, quotation_id: str, items: list[OfficeLineItem]) -> None:
        batch = await self.client.create_batch()
        for item in items:
            quantity = int(item.get("quantity") or 0)
            if quantity <= 0:
                continue

            unit_price = item.get("unit_price")
            total_cost = (unit_price * quantity) if unit_price is not None else None
            obj = await self.client.create(
                kind=CustomerQuotationLineItem,
                data={
                    "quotation": {"id": quotation_id},
                    "role": item["role"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_cost": total_cost,
                    "device_type": {"id": item["device_type_id"]} if item.get("device_type_id") else None,
                },
            )
            batch.add(task=obj.save, allow_upsert=True, node=obj)

        async for node, error in batch.execute():
            if error:
                self.logger.error(f"Failed to save office line item [{node.get_kind()}]: {error}")
                raise ValidationError(str(error))

    async def _save_proposed_campus_design(
        self,
        quotation_id: str,
        building_plans: list[dict[str, Any]],
        total_buildings: int,
        total_floors: int,
        total_access_points: int,
        total_access_switches: int,
        total_distribution_switches: int,
        total_core_routers: int,
        total_demand_mbps: float,
    ) -> None:
        existing = await self.client.filters(
            kind=self._KIND_PROPOSED_CAMPUS_DESIGN,
            quotation__ids=[quotation_id],
        )
        design_obj = await self.client.create(
            kind=self._KIND_PROPOSED_CAMPUS_DESIGN,
            data={
                **({"id": existing[0].id} if existing else {}),
                "quotation": {"id": quotation_id},
                "total_buildings": total_buildings,
                "total_floors": total_floors,
                "total_access_points": total_access_points,
                "total_access_switches": total_access_switches,
                "total_distribution_switches": total_distribution_switches,
                "total_core_routers": total_core_routers,
                "total_demand_mbps": int(round(total_demand_mbps)),
            },
        )
        await design_obj.save(allow_upsert=True)

        if not design_obj.id:
            raise ValidationError("Failed to persist campus design: missing id after save")

        await self._save_proposed_campus_buildings(design_id=cast(str, design_obj.id), building_plans=building_plans)

    async def _save_proposed_campus_buildings(self, design_id: str, building_plans: list[dict[str, Any]]) -> None:
        existing_rows = await self.client.filters(
            kind=self._KIND_PROPOSED_CAMPUS_BUILDING,
            design__ids=[design_id],
        )
        existing_by_name: dict[str, Any] = {}
        for row in existing_rows:
            name_attr = getattr(row, "name", None)
            name_value = getattr(name_attr, "value", None)
            if isinstance(name_value, str) and name_value:
                existing_by_name[name_value] = row

        batch = await self.client.create_batch()
        rows_by_name: dict[str, Any] = {}
        for plan in building_plans:
            existing = existing_by_name.get(plan["name"])
            row = await self.client.create(
                kind=self._KIND_PROPOSED_CAMPUS_BUILDING,
                data={
                    **({"id": existing.id} if existing else {}),
                    "design": {"id": design_id},
                    "name": plan["name"],
                    "floors": plan["floors"],
                    "access_points": plan["access_points"],
                    "access_switches": plan["access_switches"],
                    "distribution_switches": plan["distribution_switches"],
                    "demand_mbps": int(round(plan["demand_mbps"])),
                    "source_building": {"id": plan["id"]} if plan.get("id") else None,
                },
            )
            batch.add(task=row.save, allow_upsert=True, node=row)
            rows_by_name[plan["name"]] = row

        async for node, error in batch.execute():
            if error:
                self.logger.error(f"Failed to save proposed campus building [{node.get_kind()}]: {error}")
                raise ValidationError(str(error))

        building_id_by_name: dict[str, str] = {}
        for name, row in rows_by_name.items():
            row_id = getattr(row, "id", None)
            if isinstance(row_id, str) and row_id:
                building_id_by_name[name] = row_id

        for plan in building_plans:
            building_id = building_id_by_name.get(plan["name"])
            if not building_id:
                continue
            await self._save_proposed_campus_floors(
                building_id=building_id,
                floors_breakdown=plan.get("floors_breakdown", []),
            )

    async def _save_proposed_campus_floors(self, building_id: str, floors_breakdown: list[dict[str, Any]]) -> None:
        existing_rows = await self.client.filters(
            kind=self._KIND_PROPOSED_CAMPUS_FLOOR,
            building__ids=[building_id],
        )
        existing_by_index: dict[int, Any] = {}
        for row in existing_rows:
            index_attr = getattr(row, "index", None)
            index_value = getattr(index_attr, "value", None)
            if isinstance(index_value, int):
                existing_by_index[index_value] = row

        batch = await self.client.create_batch()
        for floor in floors_breakdown:
            floor_index = int(floor["index"])
            existing = existing_by_index.get(floor_index)
            row = await self.client.create(
                kind=self._KIND_PROPOSED_CAMPUS_FLOOR,
                data={
                    **({"id": existing.id} if existing else {}),
                    "building": {"id": building_id},
                    "index": floor_index,
                    "access_points": int(floor.get("access_points") or 0),
                    "wired_endpoints": int(floor.get("wired_endpoints") or 0),
                    "access_switches": int(floor.get("access_switches") or 0),
                    "demand_mbps": int(round(float(floor.get("demand_mbps") or 0.0))),
                },
            )
            batch.add(task=row.save, allow_upsert=True, node=row)

        async for node, error in batch.execute():
            if error:
                self.logger.error(f"Failed to save proposed campus floor [{node.get_kind()}]: {error}")
                raise ValidationError(str(error))
