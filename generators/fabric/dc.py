"""Infrastructure generator for data center topology."""

from collections.abc import Mapping
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from generators.fabric.fabric_shared import ServiceDevicesMixin
from generators.helpers.routing import RoutingStrategy, loopback_prefix_length, p2p_prefix_length
from generators.helpers.template_interfaces import template_interface_names_by_role
from generators.mixins.common import CommonGenerator
from generators.mixins.connections import CablingMixin
from generators.mixins.devices import DeviceMixin
from generators.mixins.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.mixins.pod_config import templates_by_role
from generators.mixins.pools import PoolMixin
from generators.mixins.routing import RoutingMixin
from generators.mixins.types import CablingOptions, DeviceOptions, RoutingOptions, naming_convention_of
from generators.model import DcFabricModel
from generators.protocols import TopologyDataCenter
from utils.data_cleaning import clean_data
from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES
from utils.limits import SITE_VNI_END, SITE_VNI_START

_DC_VALID_FABRIC_ROLES = frozenset(
    {"super-spine", "hyper-spine", "border-leaf", "internal-firewall", "external-firewall", "load-balancer"}
)


# Strategies under which dc.py pre-seeds its own DC-scoped tiers' BGP.
_SEEDED_STRATEGIES = (
    RoutingStrategy.EBGP_EBGP.value,
    RoutingStrategy.EBGP_IBGP.value,
    RoutingStrategy.OSPF_IBGP.value,
)


class TopologyDcData(TypedDict, total=False):
    """One clean_data()-processed TopologyDataCenter entry (queries/topology/add/dc.gql); no runtime validation."""

    id: str
    name: str
    index: int
    design: dict[str, Any]
    naming_convention: str
    connectivity_mode: str
    underlay_protocol: str
    routing_strategy: str
    fabric_templates: list[dict[str, Any]]
    loopback_pool: dict[str, Any] | None
    technical_pool: dict[str, Any] | None
    management_pool: dict[str, Any] | None
    fabric_asn_pool: dict[str, Any] | None
    # A TopologyPod child is {id, name, index, design: {id}}; any other child is just {id}.
    children: list[dict[str, Any]]
    vni_pool: dict[str, Any] | None
    evpn_rt_as: dict[str, Any] | None
    fabric_controllers: list[dict[str, Any]]
    security_manager_controllers: list[dict[str, Any]]
    lb_manager_controllers: list[dict[str, Any]]


class DCTopologyGenerator(ServiceDevicesMixin, PoolMixin, DeviceMixin, CablingMixin, RoutingMixin, CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

    data: TopologyDcData

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology."""

        try:
            deployment_list = clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error("No TopologyDeployment data found in GraphQL response")
                return

            self.data = cast(TopologyDcData, deployment_list[0])
            # No Pydantic validation: force-read every required field so a partial response raises KeyError.
            model = DcFabricModel.compile(self.data)
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        dc_id = model.dc_id
        dc_name = model.dc_name
        dc_index = model.dc_index
        dc_design = self.data["design"]
        underlay_protocol = model.underlay_protocol
        routing_strategy = model.routing_strategy
        design_mode = model.design_mode
        max_hyper_spines_cap = dc_design["max_hyper_spines_per_fabric"]
        pools_to_allocate = dict(model.pools_to_allocate)

        self.set_controllers_from(self.data)
        self.logger.info(f"Processing Data Center: {dc_name}")
        self._existing_pods = self.pods_of(self.data)
        super_spine_entries = templates_by_role(self.data.get("fabric_templates", []), "super-spine")

        self.deployment_id = dc_id
        self.fabric_name = model.fabric_name
        self.logger.info(f"Generating topology for data center {self.fabric_name.upper()}")
        self.logger.info(f"Allocating DC pools: {list(pools_to_allocate.keys())}")

        dc_pools = await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=pools_to_allocate,
            underlay_protocol=underlay_protocol,
        )

        asn_start, asn_end = model.asn_range
        fabric_asn_pool_id: str | None = None
        if model.requires_asn_pool:
            asn_pool_obj = await self.upsert_asn_pool(
                pool_name=f"{self.fabric_name}-asn-pool",
                description=f"ASN pool for {self.fabric_name.upper()} fabric",
                start_range=asn_start,
                end_range=asn_end,
            )
            if asn_pool_obj:
                fabric_asn_pool_id = asn_pool_obj.id

        # The local VLAN id is chosen per device (segment_vlan.py), so there is no per-DC pool.

        # L2 VNI pool, capped at 39999: the RD/RT leave 16 bits for the VNI, and it must stay clear of the fixed L3 VNIs
        # (60000+, exchange transit 61000+) and of GLOBAL-L2VNI (40000-49999).
        vni_pool = await self.upsert_number_pool(
            pool_name=f"{self.fabric_name}-vni-pool",
            description=f"L2 VNI pool for {self.fabric_name.upper()}",
            start_range=SITE_VNI_START,
            end_range=SITE_VNI_END,
            node="ManagedSegmentDeployment",
            node_attribute="vni",
        )

        super_spine_names: list[str] = []
        if design_mode == "back-to-back":
            self.logger.info(
                f"DC {self.fabric_name}: design_mode=back-to-back — "
                "skipping super-spine tier, spines will connect directly across pods"
            )
        elif super_spine_entries:
            super_spine_names = await self._create_dc_tier_devices(
                "super-spine",
                super_spine_entries,
                indexes=[dc_index],
                dc_pools=dc_pools,
                underlay_protocol=underlay_protocol,
            )

        # Hyper-spine (XL fabrics only) is cabled here because both tiers are DC-scoped.
        hyper_spine_entries = templates_by_role(self.data.get("fabric_templates", []), "hyper-spine")
        amount_of_hyper_spines = sum(entry["quantity"] for entry in hyper_spine_entries)
        if amount_of_hyper_spines > max_hyper_spines_cap:
            raise RuntimeError(
                f"DC {self.fabric_name.upper()} requests {amount_of_hyper_spines} hyper-spines but the assigned "
                f"design allows at most {max_hyper_spines_cap}"
            )

        hyper_spine_names = await self._create_dc_tier_devices(
            "hyper-spine",
            hyper_spine_entries,
            indexes=[dc_index],
            dc_pools=dc_pools,
            underlay_protocol=underlay_protocol,
        )

        # Shared routing objects at DC level so pods and racks never duplicate them; overlay_asn is asn_end + 1, outside
        # the device pool but inside the DC's grid slot.
        shared_routing = await self._create_shared_routing_objects(
            overlay_asn=asn_end + 1,
            asn_pool_id=fabric_asn_pool_id,
        )
        super_spine_as_id = shared_routing.super_spine_as_id

        # Attach every pool reference and the fabric AS in one plain untracked save (PoolMixin.attach_refs).
        dc_refs = {
            "loopback_pool": dc_pools["loopback"].id,
            "management_pool": dc_pools["management"].id,
            "technical_pool": dc_pools["technical"].id,
            "fabric_asn_pool": fabric_asn_pool_id,
            "vni_pool": vni_pool.id,
            "evpn_rt_as": shared_routing.overlay_as_id,
        }
        dc_data = cast("dict[str, Any]", self.data)
        await self.attach_refs(
            kind=TopologyDataCenter,
            node_id=dc_id,
            refs=dc_refs,
            current={name: (dc_data.get(name) or {}).get("id") for name in dc_refs},
        )

        # Pre-seed each DC-scoped tier's BGP before any pod generator runs; super-spines get overlay under ospf-ibgp
        # too.
        if super_spine_names and routing_strategy in _SEEDED_STRATEGIES:
            await self._preseed_tier_routing(
                "super-spine",
                super_spine_names,
                routing_strategy=routing_strategy,
                asn_pool_id=fabric_asn_pool_id,
                shared_underlay_as_id=super_spine_as_id,
            )

        if hyper_spine_names and routing_strategy in _SEEDED_STRATEGIES:
            await self._preseed_tier_routing(
                "hyper-spine", hyper_spine_names, routing_strategy=routing_strategy, asn_pool_id=fabric_asn_pool_id
            )

            # Super-spine <-> hyper-spine full mesh, both ends DC-scoped.
            super_spine_uplink_interfaces = [
                iface_name
                for entry in super_spine_entries
                for iface_name in template_interface_names_by_role(
                    interfaces=entry["template"].get("interfaces", []),
                    role="uplink",
                )
            ]
            hyper_spine_downlink_interfaces = [
                iface_name
                for entry in hyper_spine_entries
                for iface_name in template_interface_names_by_role(
                    interfaces=entry["template"].get("interfaces", []),
                    role="downlink",
                )
            ]
            if super_spine_names and super_spine_uplink_interfaces and hyper_spine_downlink_interfaces:
                # Serialize on dc_id: overlapping runs would both create the same P2P addresses.
                async with self.resource_lock(f"hyperspine-cabling-{dc_id}"):
                    p2p_pairs = await self.create_cabling(
                        bottom_devices=super_spine_names,
                        bottom_interfaces=super_spine_uplink_interfaces,
                        top_devices=hyper_spine_names,
                        top_interfaces=hyper_spine_downlink_interfaces,
                        strategy="pod",
                        options=CablingOptions(
                            pool=dc_pools.get("technical"),
                            p2p_prefix_length=p2p_prefix_length(underlay_protocol),
                        ),
                    )
                await self.create_routing(
                    bottom_devices=super_spine_names,
                    top_devices=hyper_spine_names,
                    options=RoutingOptions(design=self.data, asn_pool=fabric_asn_pool_id),
                    p2p_interfaces=p2p_pairs,
                    bottom_role="super-spine",
                    top_role="hyper-spine",
                )
            elif super_spine_names:
                self.logger.error(
                    f"DC {self.fabric_name}: cannot cable super-spine<->hyper-spine — "
                    f"super_spine_uplinks={len(super_spine_uplink_interfaces)}, "
                    f"hyper_spine_downlinks={len(hyper_spine_downlink_interfaces)}."
                )

        # Pod fan-out is dc_pod_cascade's job; back-to-back spine meshes are cabled by pod.py.

        self._underlay_protocol = underlay_protocol
        dc_fabric_loopback_pool = dc_pools.get("dc-fabric-loopback")
        self._dc_fabric_loopback_pool_id = dc_fabric_loopback_pool.id if dc_fabric_loopback_pool else None
        await self._generate_dc_scoped_fabric_devices()

    async def _create_dc_tier_devices(
        self,
        role: str,
        entries: list[dict[str, Any]],
        *,
        indexes: list[int],
        dc_pools: dict[str, Any],
        underlay_protocol: str,
    ) -> list[str]:
        """Create one DC-scoped fabric tier (super-spine/hyper-spine) from the DC-fabric loopback pool."""
        names: list[str] = []
        for entry in entries:
            names.extend(
                await self.create_devices(
                    deployment_id=self.data["id"],
                    device_role=role,
                    quantity=entry["quantity"],
                    template=entry["template"],
                    naming_convention=naming_convention_of(self.data),
                    options=DeviceOptions(
                        indexes=indexes,
                        allocate_loopback=True,
                        loopback_pool=dc_pools.get("dc-fabric-loopback"),
                        loopback_prefix_length=loopback_prefix_length(underlay_protocol),
                        management_pool=dc_pools.get("management"),
                    ),
                )
            )
        return names

    async def _preseed_tier_routing(
        self,
        role: str,
        names: list[str],
        *,
        routing_strategy: str,
        asn_pool_id: str | None,
        shared_underlay_as_id: str | None = None,
    ) -> None:
        """Create a DC-scoped tier's BGP processes before any cabling (overlay only under ospf-ibgp)."""
        options = RoutingOptions(design=self.data, asn_pool=asn_pool_id)
        if routing_strategy == RoutingStrategy.OSPF_IBGP.value:
            options["skip_underlay"] = True
        elif shared_underlay_as_id:
            options["shared_underlay_as_id"] = shared_underlay_as_id
        await self.create_routing(
            bottom_devices=names, top_devices=[], options=options, p2p_interfaces=[], bottom_role=role
        )

    def _validate_fabric_template_roles(self) -> None:
        """Log and skip a fabric_templates entry with a role this generator cannot place."""
        for entry in self.data.get("fabric_templates", []):
            if entry["role"] not in _DC_VALID_FABRIC_ROLES:
                self.logger.warning(
                    f"DC {self.fabric_name}: fabric_templates entry with role={entry['role']!r} is not valid "
                    f"at DC level (expected one of {sorted(_DC_VALID_FABRIC_ROLES)}) — skipping this entry."
                )

    @staticmethod
    def pods_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The DC's pods from its cleaned payload: the children that carry a design."""
        return [child for child in data.get("children") or [] if child.get("design")]

    def _pod_border_leaf_capacities(self, pods: list[dict[str, Any]]) -> dict[str, int]:
        """Pod id -> how many border-leaf devices the pod can receive (its design's max_border_leafs_per_pod).
        A pod capped at 0 is skipped; one whose design is not in the DC design's pod_templates gets none."""
        cap_by_design = {
            template["id"]: template["max_border_leafs_per_pod"] for template in self.data["design"]["pod_templates"]
        }
        capacities: dict[str, int] = {}
        for pod in pods:
            design_id = pod["design"]["id"]
            if design_id not in cap_by_design:
                self.logger.warning(
                    f"DC {self.fabric_name}: pod {pod['name']}'s design is not one of the DC design's "
                    "pod_templates — no border-leafs are placed on it."
                )
            capacities[pod["id"]] = cap_by_design.get(design_id, 0)
        return capacities

    async def _create_border_leaf_devices(self) -> list[str]:
        """Create border-leaf devices for every fabric_templates(role="border-leaf") entry, spread over the DC's pods in
        index order. Capped per pod by max_border_leafs_per_pod; cabling to the spines is pod.py's job. Returns every
        name created.
        """
        entries = templates_by_role(self.data.get("fabric_templates", []), "border-leaf")
        if not entries:
            return []

        existing_pods = getattr(self, "_existing_pods", [])
        if not existing_pods:
            self.logger.info(f"DC {self.fabric_name}: no pods yet — deferring border-leaf placement")
            return []
        sorted_pods = sorted(existing_pods, key=lambda p: p["index"])

        max_border_leafs_per_fabric = self.data["design"]["max_border_leafs_per_fabric"]
        capacities = self._pod_border_leaf_capacities(sorted_pods)
        all_names: list[str] = []
        for entry in entries:
            if entry["quantity"] > max_border_leafs_per_fabric:
                self.logger.error(
                    f"DC {self.fabric_name}: border-leaf entry requests {entry['quantity']} devices but "
                    f"design.max_border_leafs_per_fabric allows at most {max_border_leafs_per_fabric} — skipping."
                )
                continue

            remaining = entry["quantity"]
            for pod in sorted_pods:
                if remaining <= 0:
                    break
                pod_capacity = capacities[pod["id"]]
                if pod_capacity <= 0:
                    continue
                share = min(remaining, pod_capacity)
                remaining -= share

                device_options = DeviceOptions(
                    indexes=[self.data["index"], pod["index"]],
                    allocate_loopback=True,
                    loopback_pool=self._dc_fabric_loopback_pool_id,
                    loopback_prefix_length=loopback_prefix_length(self._underlay_protocol),
                )
                names = await self.create_devices(
                    deployment_id=self.data["id"],
                    device_role="border-leaf",
                    quantity=share,
                    template=entry["template"],
                    naming_convention=naming_convention_of(self.data),
                    options=device_options,
                )
                all_names.extend(names)

            if remaining > 0:
                # A warning, not a failure: pods may arrive later (dc_pod_cascade places the rest), but it is a data
                # error once all pods exist.
                capacity = sum(capacities.values())
                self.logger.warning(
                    f"DC {self.fabric_name}: border-leaf entry requested {entry['quantity']} device(s) but "
                    f"{remaining} are left unplaced — the DC's {len(sorted_pods)} pod(s) offer "
                    f"max_border_leafs_per_pod capacity for {capacity} in total. Add pods, or lower the "
                    "entry's quantity to match."
                )

        return all_names

    async def _generate_dc_scoped_fabric_devices(self) -> None:
        """Create border-leaf devices and provision the DC's shared service chain; a pod added later needs a
        dc_pod_cascade run.
        """
        border_leaf_names = await self._create_border_leaf_devices()
        await self._generate_dc_shared_service_devices(border_leaf_names=border_leaf_names)

    async def _generate_dc_shared_service_devices(self, *, border_leaf_names: list[str]) -> None:
        """Create shared DC firewall/load-balancer devices and cable them to border-leaf."""

        data = self.data
        firewall_names, load_balancer_names = await self.create_firewall_and_load_balancer_devices(
            fabric_templates=data.get("fabric_templates", []),
            deployment_id=data["id"],
            naming_convention=naming_convention_of(data),
            indexes=[data["index"]],
        )

        external_names = await self.create_external_firewall_devices(
            fabric_templates=data.get("fabric_templates", []),
            deployment_id=data["id"],
            naming_convention=naming_convention_of(data),
            indexes=[data["index"]],
        )

        # This DC's own context VLAN pool per service role (generators/firewall_context.py allocates from it); the
        # external pair takes its VLANs from the firewall contexts' pool (EDGE_CONTEXT).
        for spec, names in (
            (FIREWALL_CONTEXT, firewall_names + external_names),
            (LOADBALANCER_CONTEXT, load_balancer_names),
        ):
            if names:
                await self.ensure_context_vlan_pool(name=self.fabric_name, spec=spec)

        await self._cable_border_services(
            border_role_for=BORDER_ROLE_FOR_SERVICES,
            connectivity_mode=cast(Literal["pbr", "inline"], data.get("connectivity_mode", "pbr")),
            border_names=border_leaf_names,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )
        if external_names:
            # A pbr-style leg off the same border leaves on the next free firewall-role ports, never inline.
            await self._cable_border_services(
                border_role_for=BORDER_ROLE_FOR_SERVICES,
                connectivity_mode="pbr",
                border_names=border_leaf_names,
                firewall_names=external_names,
                load_balancer_names=[],
            )
