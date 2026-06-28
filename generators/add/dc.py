"""Infrastructure generator for data center topology."""

from typing import Any, Literal, cast

from utils.data_cleaning import clean_data

from ..common import CommonGenerator, DeviceOptions
from ..helpers import calculate_super_spine_loopback_prefix, name_to_asn_range
from ..helpers.routing import RoutingStrategy
from ..models import DCModel
from ..protocols import RoutingAutonomousSystem, RoutingOSPFArea, TopologyPod
from ..types import RoutingOptions


class DCTopologyGenerator(CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

    async def update_checksum(self) -> None:
        """Update checksum for all pods in the data center.

        The checksum is based on DC configuration.
        """
        pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id])

        fabric_checksum = self.calculate_checksum()

        pods_to_update = [pod for pod in pods if pod.checksum.value != fabric_checksum]

        for pod in pods_to_update:
            pod.checksum.value = fabric_checksum
            await pod.save(allow_upsert=True)
            self.logger.info(f"Checksum updated: {pod.name.value} → {fabric_checksum} (triggers pod re-generation)")

        self.logger.info(
            f"DC checksum propagation completed: {len([p for p in pods if p.checksum.value == fabric_checksum])} "
            f"pod(s) updated to checksum {fabric_checksum}"
        )

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology."""

        try:
            deployment_list = clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error("No TopologyDeployment data found in GraphQL response")
                return

            self.data = DCModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Processing Data Center: {self.data.name}")

        # Add existing pods to group context to prevent deletion
        existing_pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id])
        for pod in existing_pods:
            self.client.group_context.related_node_ids.append(pod.id)

        dc_id = self.data.id
        self.deployment_id = dc_id  # Store for cable linking
        self.fabric_name = self.data.name.lower()
        dc_index = self.data.index  # Get DC index for unique device naming
        amount_of_super_spines = self.data.amount_of_super_spines
        super_spine_template = self.data.super_spine_template
        self.logger.info(f"Generating topology for data center {self.fabric_name.upper()}")
        indexes: list[int] = [dc_index]

        if not self.data.design:
            self.logger.error(f"Cannot create pools for DC {self.fabric_name.upper()}: design relationship is required")
            return

        if amount_of_super_spines > self.data.design.max_super_spines_per_fabric:
            raise RuntimeError(
                f"DC {self.fabric_name.upper()} requests {amount_of_super_spines} super-spines but the assigned "
                f"design allows at most {self.data.design.max_super_spines_per_fabric}"
            )

        naming_convention = self.data.naming_convention
        is_ipv6 = self.data.design.is_ipv6
        is_dual_stack = self.data.design.is_dual_stack

        # Pool prefix lengths set explicitly on DC instance (operator knows their capacity plan).
        # IPv6: technical and loopback get +96 offset (128-32 base difference).
        # Management is always IPv4.
        base_technical = self.data.technical_prefix_length
        base_loopback = self.data.loopback_prefix_length
        management_prefix = self.data.management_prefix_length

        if is_ipv6:
            technical_prefix = base_technical + 96
            loopback_prefix = base_loopback + 96
        elif is_dual_stack:
            technical_prefix = base_technical + 96
            loopback_prefix = base_loopback
        else:
            technical_prefix = base_technical
            loopback_prefix = base_loopback

        pools_to_allocate: dict[str, int] = {
            "technical": technical_prefix,
            "loopback": loopback_prefix,
            "management": management_prefix,
        }
        if amount_of_super_spines > 0 and super_spine_template:
            super_spine_loopback_prefix = calculate_super_spine_loopback_prefix(
                max_super_spines=amount_of_super_spines,
                ipv6=is_ipv6,
            )
            pools_to_allocate["super-spine-loopback"] = super_spine_loopback_prefix
            self.logger.info(
                f"Creating pools from design: technical=/{technical_prefix}, "
                f"loopback=/{loopback_prefix}, management=/{management_prefix}, "
                f"super-spine-loopback=/{super_spine_loopback_prefix}"
            )
        else:
            self.logger.info(
                f"Creating pools from design: technical=/{technical_prefix}, "
                f"loopback=/{loopback_prefix}, management=/{management_prefix} (no super-spines)"
            )

        dc_pools = await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=pools_to_allocate,
            ipv6=is_ipv6,
            dual_stack=is_dual_stack,
        )

        # Update DC with pool references (single fetch + save)
        dc = await self.client.get(kind="TopologyDataCenter", id=dc_id)
        if dc:
            pool_attr_map: dict[str, str] = {
                "loopback": "loopback_pool",
                "management": "management_pool",
                "technical": "technical_pool",
            }
            for pool_name, pool_obj in dc_pools.items():
                if pool_name in pool_attr_map:
                    setattr(dc, pool_attr_map[pool_name], {"id": pool_obj.id, "hfid": [pool_obj.hfid]})
            await dc.save(allow_upsert=True)

        # Derive deterministic ASN range from DC name (unique per site)
        max_pods = self.data.design.max_pods
        max_spines_per_pod = self.data.design.max_spines_per_pod
        asn_start, asn_end = name_to_asn_range(
            dc_name=self.data.name,
            max_pods=max_pods,
            amount_of_super_spines=amount_of_super_spines,
            max_spines_per_pod=max_spines_per_pod,
        )

        # Only create ASN pool for eBGP-based strategies (one pool per DC, shared by all devices)
        # ospf-ibgp uses OSPF underlay + shared overlay AS — no per-device pools needed
        routing_strategy = self.data.design.routing_strategy
        ss_asn_pool_id: str | None = None
        if routing_strategy in (RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value):
            asn_pool_obj = await self.upsert_asn_pool(
                pool_name=f"{self.fabric_name}-asn-pool",
                description=f"ASN pool for {self.fabric_name.upper()} fabric",
                start_range=asn_start,
                end_range=asn_end,
                parent_kind="TopologyDataCenter",
                parent_id=dc_id,
                parent_attr="super_spine_asn_pool",
            )
            if asn_pool_obj:
                ss_asn_pool_id = asn_pool_obj.id

        # Create VLAN pool for segment activation (per-DC VLAN ID allocation)
        await self.upsert_number_pool(
            pool_name=f"{self.fabric_name}-vlan-pool",
            description=f"VLAN ID pool for {self.fabric_name.upper()}",
            start_range=100,
            end_range=3999,
            node="ManagedSegmentDeployment",
            node_attribute="vlan_id",
            parent_kind="TopologyDataCenter",
            parent_id=dc_id,
            parent_attr="vlan_pool",
        )

        # Create VNI and L3 VNI pools only when overlay is VXLAN-EVPN
        overlay_technology = self.data.overlay_technology
        if overlay_technology == "vxlan_evpn":
            await self.upsert_number_pool(
                pool_name=f"{self.fabric_name}-vni-pool",
                description=f"L2 VNI pool for {self.fabric_name.upper()}",
                start_range=10001,
                end_range=16777215,
                node="ManagedSegmentDeployment",
                node_attribute="vni",
                parent_kind="TopologyDataCenter",
                parent_id=dc_id,
                parent_attr="vni_pool",
            )
            await self.upsert_number_pool(
                pool_name=f"{self.fabric_name}-l3vni-pool",
                description=f"L3 VNI pool for {self.fabric_name.upper()} VRFs",
                start_range=50001,
                end_range=59999,
                node="BuiltinIPNamespace",
                node_attribute="l3_vni",
                parent_kind="TopologyDataCenter",
                parent_id=dc_id,
                parent_attr="l3_vni_pool",
            )

        super_spine_names: list[str] = []
        if amount_of_super_spines > 0 and super_spine_template:
            super_spine_names = await self.create_devices(
                deployment_id=dc_id,
                device_role="super-spine",
                amount=amount_of_super_spines,
                template=super_spine_template.model_dump(),
                naming_convention=cast(
                    Literal["standard", "hierarchical", "flat"],
                    naming_convention.lower(),
                ),
                options=DeviceOptions(
                    indexes=indexes,
                    allocate_loopback=True,
                    loopback_pool=dc_pools.get("super-spine-loopback"),
                    loopback_prefix_length=128 if is_ipv6 else 32,
                    management_pool=dc_pools.get("management"),
                ),
            )

        # Create shared routing objects (overlay AS, OSPF area) at the DC level
        # so pod/rack generators always find them and never create duplicates.
        # overlay_asn is asn_end + 1 to avoid collision with the per-device pool range [asn_start, asn_end]
        await self._create_shared_routing_objects(overlay_asn=asn_end + 1)

        # Create super-spine routing objects here so they exist before any pod generator runs.
        # For eBGP strategies: underlay + overlay BGP processes.
        # For ospf-ibgp: overlay BGP only — super-spines sit above the OSPF domain and are
        # skipped as top_devices in pod-level routing, so their overlay BGP must be seeded here.
        if super_spine_names and routing_strategy in (
            RoutingStrategy.EBGP_EBGP.value,
            RoutingStrategy.EBGP_IBGP.value,
            RoutingStrategy.OSPF_IBGP.value,
        ):
            routing_opts = RoutingOptions(design=self.data.design, asn_pool=ss_asn_pool_id)
            if routing_strategy == RoutingStrategy.OSPF_IBGP.value:
                routing_opts["skip_underlay"] = True
            await self.create_routing(
                bottom_devices=super_spine_names,
                top_devices=[],
                options=routing_opts,
            )

        await self.update_checksum()

    async def _create_shared_routing_objects(self, overlay_asn: int) -> None:
        """Create fabric-wide shared routing objects once at DC level.

        Based on the routing strategy:
        - ``*-ibgp``: creates a single shared RoutingAutonomousSystem for iBGP overlay
        - ``ospf-*``: creates a single shared RoutingOSPFArea (area 0) for OSPF underlay

        These objects are created idempotently (allow_upsert) so re-running
        the DC generator is safe.
        """
        if not self.data.design:
            return

        strategy = self.data.design.routing_strategy

        # iBGP overlay → ensure exactly one shared ASN exists with deterministic value
        if strategy in (RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value):
            overlay_desc = f"{self.fabric_name} overlay ASN for iBGP EVPN"
            try:
                existing = await self.client.filters(
                    kind=RoutingAutonomousSystem,
                    description__value=overlay_desc,
                )
                if existing:
                    as_obj = existing[0]
                    as_obj.asn.value = overlay_asn
                    await as_obj.save(allow_upsert=True)
                    self.logger.info(f"Updated shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
                else:
                    as_obj = await self.client.create(
                        kind=RoutingAutonomousSystem,
                        data={"asn": overlay_asn, "description": overlay_desc},
                    )
                    await as_obj.save(allow_upsert=True)
                    self.logger.info(f"Created shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
                self.client.group_context.related_node_ids.append(as_obj.id)
            except Exception as e:
                self.logger.error(f"Failed to create shared overlay AS: {e}")

        # OSPF underlay → create shared area 0
        if strategy == RoutingStrategy.OSPF_IBGP.value:
            area_name = f"{self.fabric_name}-ospf-area-0"
            try:
                area_obj = await self.client.create(
                    kind=RoutingOSPFArea,
                    data={
                        "name": area_name,
                        "area": 0,
                        "area_type": "standard",
                        "description": f"OSPF backbone area for {self.fabric_name}",
                    },
                )
                await area_obj.save(allow_upsert=True)
                self.logger.info(f"Created shared OSPF area: {area_name} ({area_obj.id})")
                self.client.group_context.related_node_ids.append(area_obj.id)
            except Exception as e:
                self.logger.error(f"Failed to create shared OSPF area: {e}")
