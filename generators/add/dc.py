"""Infrastructure generator for data center topology."""

import asyncio
import secrets
from typing import Any, Literal, cast

from infrahub_sdk.task.models import TaskFilter, TaskState

from utils.data_cleaning import clean_data

from ..common import CablingOptions, CommonGenerator, DeviceOptions
from ..helpers import calculate_super_spine_loopback_prefix, name_to_asn_range
from ..helpers.routing import RoutingStrategy
from ..models import DCModel
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    RoutingAutonomousSystem,
    RoutingOSPFArea,
    RoutingPassword,
    TopologyPod,
)
from ..types import RoutingOptions

_POD_TASK_INITIAL_DELAY = 5.0  # seconds — lets the checksum-bump-triggered add_pod tasks get scheduled
_POD_TASK_WAIT_MAX_ATTEMPTS = 12
_POD_TASK_WAIT_POLL_INTERVAL = 5.0  # seconds
_POD_TASK_STABLE_ZERO_COUNT = 2  # consecutive zero-in-flight checks before considering pods done
_IN_FLIGHT_TASK_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]


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
        dc_design = self.data.design
        is_ipv6 = dc_design.is_ipv6
        is_dual_stack = dc_design.is_dual_stack

        # Prefix lengths come from the design; the DC instance can override by pre-attaching pools.
        # Values must already match the underlay_protocol (IPv4 or IPv6) — no conversion needed.
        # Management is always IPv4.
        technical_prefix = dc_design.technical_prefix_length
        loopback_prefix = dc_design.loopback_prefix_length
        management_prefix = dc_design.management_prefix_length

        # Collect pools that already exist on the DC instance — those are reused as-is.
        # Only pools that are absent need to be allocated from the design defaults.
        existing_dc_pools: dict[str, Any] = {}
        pool_name_to_attr = {
            "technical": "technical_pool",
            "loopback": "loopback_pool",
            "management": "management_pool",
        }
        for pool_key, attr in pool_name_to_attr.items():
            existing = getattr(self.data, attr, None)
            if existing:
                existing_dc_pools[pool_key] = existing

        pools_to_allocate: dict[str, int] = {}
        for pool_key, prefix in [
            ("technical", technical_prefix),
            ("loopback", loopback_prefix),
            ("management", management_prefix),
        ]:
            if pool_key not in existing_dc_pools:
                pools_to_allocate[pool_key] = prefix

        design_mode = "back-to-back" if getattr(dc_design, "max_super_spines_per_fabric", 0) == 0 else "super-spine"
        if amount_of_super_spines > 0 and super_spine_template and design_mode != "back-to-back":
            super_spine_loopback_prefix = calculate_super_spine_loopback_prefix(
                max_super_spines=amount_of_super_spines,
                ipv6=is_ipv6,
            )
            pools_to_allocate["super-spine-loopback"] = super_spine_loopback_prefix

        reused = list(existing_dc_pools.keys())
        creating = list(pools_to_allocate.keys())
        if reused:
            self.logger.info(f"Reusing existing DC pools: {reused}; creating: {creating}")
        else:
            self.logger.info(f"Creating pools from design: {creating}")

        new_pools = await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=pools_to_allocate,
            ipv6=is_ipv6,
            dual_stack=is_dual_stack,
        )

        # Merge existing + newly created into dc_pools for downstream use
        dc_pools: dict[str, Any] = {**existing_dc_pools, **new_pools}

        # Update DC with pool references for any newly created pools
        if new_pools:
            dc = await self.client.get(kind="TopologyDataCenter", id=dc_id)
            if dc:
                pool_attr_map: dict[str, str] = {
                    "loopback": "loopback_pool",
                    "management": "management_pool",
                    "technical": "technical_pool",
                }
                for pool_name, pool_obj in new_pools.items():
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
        if design_mode == "back-to-back":
            self.logger.info(
                f"DC {self.fabric_name}: design_mode=back-to-back — "
                "skipping super-spine tier, spines will connect directly across pods"
            )
        elif amount_of_super_spines > 0 and super_spine_template:
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
                p2p_interfaces=[],
                bottom_role="super-spine",
            )

        await self.update_checksum()

        # Back-to-back inter-pod spine cabling: designs whose super-spine tier is either
        # absent by design (design_mode=back-to-back) or simply unconfigured for this DC
        # (super_spine_names ended up empty above) need spines to connect directly across
        # pods instead of via a super-spine. Done here, once, after every pod's own
        # generator run has finished — rather than each pod generator racing its siblings
        # independently — so there is exactly one writer for this cabling/routing, no
        # inter-generator concurrency to reason about.
        if not super_spine_names and len(existing_pods) >= 2:
            await self._cable_pods_back_to_back(existing_pods, dc_design, ss_asn_pool_id, is_ipv6)

    async def _wait_for_pod_generators(self, pod_ids: list[str]) -> None:
        """Poll until no in-flight generator tasks remain for the given pods.

        ``update_checksum()`` bumps every pod's checksum, which Infrahub's own event
        system picks up and schedules ``add_pod`` for asynchronously — outside this
        generator's own call stack. Mirrors the shape of the integration-test harness's
        ``wait_for_tasks_completion`` (initial delay for scheduling + stable-zero-count
        polling) using the same ``client.task.filter``/``TaskFilter``/``TaskState`` API,
        without depending on test-only code.
        """
        await asyncio.sleep(_POD_TASK_INITIAL_DELAY)

        consecutive_zero = 0
        for attempt in range(1, _POD_TASK_WAIT_MAX_ATTEMPTS + 1):
            in_flight = await self.client.task.filter(
                filter=TaskFilter(
                    state=_IN_FLIGHT_TASK_STATES,
                    branch=self.branch_name,
                    related_node__ids=pod_ids,
                ),
            )
            if not in_flight:
                consecutive_zero += 1
                if consecutive_zero >= _POD_TASK_STABLE_ZERO_COUNT:
                    self.logger.info(
                        f"DC {self.fabric_name}: all pod generators finished "
                        f"({consecutive_zero} consecutive zero checks)"
                    )
                    return
            else:
                consecutive_zero = 0
                self.logger.info(
                    f"DC {self.fabric_name}: waiting for {len(in_flight)} pod generator task(s) "
                    f"to finish (attempt {attempt}/{_POD_TASK_WAIT_MAX_ATTEMPTS})"
                )
            await asyncio.sleep(_POD_TASK_WAIT_POLL_INTERVAL)

        self.logger.warning(
            f"DC {self.fabric_name}: pod generators still running after {_POD_TASK_WAIT_MAX_ATTEMPTS} "
            "attempts — proceeding with whatever pod data exists"
        )

    async def _cable_pods_back_to_back(
        self,
        pods: list[Any],
        dc_design: Any,
        asn_pool_id: str | None,
        is_ipv6: bool,
    ) -> None:
        """Cable every pod's spines to every lower-index pod's spines, in one pass.

        Waits for all pods' own generator runs to finish first (see
        ``_wait_for_pod_generators``), then fetches each pod's spine devices, an uplink
        interface name (shared by every spine in that pod — same template), and technical
        pool live, and builds the full pairwise mesh (index_i > index_j, mirroring the
        "only connect to lower-index" dedup rule previously enforced per-pod-generator).
        """
        pod_ids = [pod.id for pod in pods]
        await self._wait_for_pod_generators(pod_ids)

        pod_spines: dict[str, list[str]] = {}
        pod_uplink_interfaces: dict[str, list[str]] = {}
        pod_technical_pool: dict[str, str | None] = {}
        pod_index: dict[str, int] = {}

        for pod in pods:
            pod_index[pod.id] = pod.index.value

            spines = await self.client.filters(
                kind=DcimPhysicalDevice,
                deployment__ids=[pod.id],
                role__value="spine",
            )
            pod_spines[pod.id] = [s.name.value for s in spines]

            # Every spine in a pod shares the same template, so the set of distinct
            # uplink interface names is identical across devices — one query for the
            # whole pod, not per-device.
            uplinks = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=pod_spines[pod.id],
                role__value="uplink",
            )
            pod_uplink_interfaces[pod.id] = sorted({iface.name.value for iface in uplinks})

            pod_obj = await self.client.get(kind=TopologyPod, id=pod.id, include=["prefix_pool"])
            pod_technical_pool[pod.id] = pod_obj.prefix_pool.id if pod_obj.prefix_pool else None

        dc_max_spines = dc_design.max_spines_per_pod if dc_design else 0
        p2p_prefix_length = 127 if is_ipv6 else 31

        for pod_i in pods:
            spines_i = pod_spines[pod_i.id]
            if not spines_i or not pod_uplink_interfaces[pod_i.id]:
                continue
            for pod_j in pods:
                if pod_index[pod_j.id] >= pod_index[pod_i.id]:
                    continue  # Only connect to lower-index pods — each pair cabled exactly once
                spines_j = pod_spines[pod_j.id]
                if not spines_j or not pod_uplink_interfaces[pod_j.id]:
                    self.logger.warning(
                        f"DC {self.fabric_name}: pod idx={pod_index[pod_j.id]} has no spine devices — "
                        f"skipping inter-pod cabling from pod idx={pod_index[pod_i.id]}"
                    )
                    continue

                cabling_offset = (pod_index[pod_j.id] - 1) * dc_max_spines
                self.logger.info(
                    f"DC {self.fabric_name}: cabling pod idx={pod_index[pod_i.id]} → "
                    f"pod idx={pod_index[pod_j.id]} [{len(spines_i)} spines → {len(spines_j)} spines, "
                    f"offset={cabling_offset}]"
                )
                routing_opts = RoutingOptions(design=dc_design, asn_pool=asn_pool_id) if dc_design else RoutingOptions()
                p2p_pairs = await self.create_cabling(
                    bottom_devices=spines_i,
                    bottom_interfaces=pod_uplink_interfaces[pod_i.id],
                    top_devices=spines_j,
                    top_interfaces=pod_uplink_interfaces[pod_j.id],
                    strategy="pod",
                    options=CablingOptions(
                        cabling_offset=cabling_offset,
                        pool=pod_technical_pool[pod_i.id],
                        p2p_prefix_length=p2p_prefix_length,
                    ),
                )
                if routing_opts.get("design"):
                    await self.create_routing(
                        bottom_devices=spines_i,
                        top_devices=spines_j,
                        options=routing_opts,
                        p2p_interfaces=p2p_pairs,
                        bottom_role="spine",
                        top_role="spine",
                    )

    async def _ensure_routing_password(self, name: str, description: str) -> None:
        """Find-or-create a shared RoutingPassword by deterministic name.

        Query-first: an existing password's value is never regenerated or
        upserted, since ``secrets.token_urlsafe()`` is non-deterministic —
        re-running this on every DC generate would silently rotate the
        BGP/OSPF auth key underneath already-deployed devices.
        """
        try:
            existing = await self.client.get(kind=RoutingPassword, name__value=name, raise_when_missing=False)
            if existing:
                self.client.group_context.related_node_ids.append(existing.id)
                self.logger.info(f"Found existing RoutingPassword: {name} ({existing.id})")
                return
        except Exception as e:
            self.logger.warning(f"Error querying RoutingPassword {name}: {e}")
            return

        try:
            obj = await self.client.create(
                kind=RoutingPassword,
                data={
                    "name": name,
                    "password": secrets.token_urlsafe(24),
                    "description": description,
                },
            )
            await obj.save(allow_upsert=True)
            self.client.group_context.related_node_ids.append(obj.id)
            self.logger.info(f"Created shared RoutingPassword: {name} ({obj.id})")
        except Exception as e:
            self.logger.error(f"Failed to create shared RoutingPassword {name}: {e}")

    async def _create_shared_routing_objects(self, overlay_asn: int) -> None:
        """Create fabric-wide shared routing objects once at DC level.

        Based on the routing strategy:
        - ``*-ibgp``: creates a single shared RoutingAutonomousSystem for iBGP overlay
        - ``ospf-*``: creates a single shared RoutingOSPFArea (area 0) for OSPF underlay
        - all strategies: creates shared underlay + overlay BGP/OSPF auth keys
          (RoutingPassword), each referenced by every underlay/overlay peering.

        These objects are created idempotently (allow_upsert) so re-running
        the DC generator is safe. RoutingPassword is the one exception: its
        secret value is generated once and never touched again on re-run
        (see ``_ensure_routing_password``).
        """
        if not self.data.design:
            return

        strategy = self.data.design.routing_strategy

        await self._ensure_routing_password(
            name=f"{self.fabric_name}-underlay-key",
            description=f"Shared eBGP/OSPF underlay auth key for {self.fabric_name}",
        )
        await self._ensure_routing_password(
            name=f"{self.fabric_name}-overlay-key",
            description=f"Shared BGP overlay/EVPN auth key for {self.fabric_name}",
        )

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
