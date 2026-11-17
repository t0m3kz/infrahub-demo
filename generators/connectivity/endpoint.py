"""Infrastructure generator for endpoint device connectivity: connects servers to the fabric by deployment type.
Interface type matching, dual-homing and connection fingerprinting for idempotency."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from infrahub_sdk.protocols import CoreGroup
from typing_extensions import TypedDict

from generators.base import BaseGenerator
from generators.connectivity.endpoint_lag import plan_lag_wiring, read_lag_wiring
from generators.connectivity.endpoint_targets import FREE_OR_CABLED_INTERFACE_STATUSES, EndpointTargetResolver
from generators.connectivity.endpoint_uplink import plan_cables, plan_uplink_connections, with_port_types
from generators.dcim.rack_layout import read_device_positions
from generators.helpers.cabling import cable_endpoint_device_names
from generators.model.pod_profile import pod_deployment_type
from generators.plan import NodeSpec, TopologyPlan
from generators.protocols import (
    AppComponent,
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    ManagedControllerVirtual,
)
from utils.data_cleaning import clean_data
from utils.limits import GROUP_APP_COMPONENTS

# Tags the switch ports a component's instances are cabled to (app_instance_segment.py). Its own triggers do not fire
# when cabling lands later, so a run that cables this endpoint dispatches it; only GROUP_APP_COMPONENTS members run.
_COMPONENT_SEGMENT_GENERATOR = "add_app_component_segment"
# The NIC roles this generator cables: plain uplinks and bond members.
ENDPOINT_NIC_ROLES = ("uplink", "lag")
# Instance kinds placed on a physical host (pinned to utils/app_instances.HOSTED_INSTANCE_TYPENAMES by a unit test).
HOSTED_INSTANCE_KINDS = (DcimVirtualDevice, ManagedControllerVirtual)


def _is_generated_uplink_cable(cable: Any, own_ends: set[str]) -> bool:
    """Whether this generator made this cable for one of this endpoint's uplinks (named by ``plan_link``)."""
    return bool(own_ends & set(str(cable.name.value).split("__")))


class EndpointInterfaceData(TypedDict, total=False):
    """One interface projection (``EndpointInterfaceFields`` in endpoint.gql)."""

    id: str
    name: str
    interface_type: str | None
    role: str | None
    status: str | None
    cable: dict[str, Any] | None


class EndpointPodRackData(TypedDict):
    """One rack of the pod (``racks`` of ``EndpointPodFields`` in endpoint.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str


class EndpointPodData(TypedDict, total=False):
    """Pod projection (``EndpointPodFields`` in endpoint.gql)."""

    id: str
    name: str
    design: dict[str, Any]
    index: int
    parent: dict[str, Any]
    racks: list[EndpointPodRackData]


class EndpointRackData(TypedDict, total=False):
    """Rack projection (``RackFields`` in endpoint.gql)."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str
    deployment: EndpointPodData


class EndpointBondData(TypedDict):
    """One bond of the server (``BondFields`` in endpoint.gql)."""

    id: str
    name: str
    role: str | None
    lag_id: int | None
    member_interfaces: list[dict[str, str]]


class EndpointDeviceData(TypedDict, total=False):
    """Top-level endpoint device (``DcimDevice`` in endpoint.gql)."""

    id: str
    name: str
    role: str | None
    deployment: dict[str, Any] | None
    rack: EndpointRackData | None
    interfaces: list[EndpointInterfaceData]


class EndpointConnectivityGenerator(BaseGenerator):
    """Generate connectivity for endpoint devices by deployment type: middle_rack, tor or mixed (strategies in
    endpoint_targets.py). Speed-aware, dual-homed across consecutive device pairs. The cables are a plan
    (generators/dcim/cabling_plan.py) built from a snapshot of the free ports and applied under the (pod, row) lock.
    """

    data: EndpointDeviceData

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate endpoint device connectivity based on deployment type."""
        try:
            deployment_list = clean_data(data).get("DcimDevice", [])
            if not deployment_list:
                self.logger.error("No Endpoint Device data found in GraphQL response")
                return

            # Drop empty interface nodes; split the server's own bonds (with a lag_id) from its physical NICs.
            deployment_data = deployment_list[0]
            bonds: list[EndpointBondData] = []
            if "interfaces" in deployment_data:
                present = [intf for intf in deployment_data["interfaces"] if intf]
                bonds = [intf for intf in present if "lag_id" in intf and intf.get("role") == "lag"]
                deployment_data["interfaces"] = [intf for intf in present if "lag_id" not in intf]

            self.data = cast(EndpointDeviceData, deployment_data)
            # No Pydantic validation: force-read every required field here so a partial response raises KeyError.
            endpoint_name = self.data["name"]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        if not self.data.get("rack"):
            self.logger.error(f"Endpoint {endpoint_name} has no rack assigned - cannot determine connectivity")
            return

        rack = self.data["rack"]
        assert rack is not None
        pod = rack.get("deployment") or {}
        if "design" not in pod:
            # Same guard as RackGenerator: a colocation cage rack arrives as an empty pod dict; its kit is cabled in the
            # object data, but every role=endpoint device reaches this generator.
            self.logger.info(
                "Endpoint %s: rack %s does not hang off a DC-fabric pod, nothing to cable",
                endpoint_name,
                rack.get("name"),
            )
            return
        deployment_type = pod_deployment_type(pod["design"])
        pod_id = pod["id"]
        dc_id = pod["parent"]["id"]

        self.logger.info(f"Generating connectivity for endpoint {endpoint_name} in {deployment_type} deployment")

        # Never tracked: the server is data-loaded and is this run's target, not its output.
        if (self.data.get("deployment") or {}).get("id") != pod_id:
            await self.apply_plan(
                TopologyPlan(
                    root_id=None,
                    specs=[
                        NodeSpec(
                            "endpoint-deployment",
                            DcimPhysicalDevice,
                            {"id": self.data["id"], "deployment": {"id": pod_id}},
                        )
                    ],
                )
            )
            self.logger.info(f"Updated {endpoint_name} deployment to pod {pod['name'].lower()}")

        # Concurrent endpoint runs would pick the same free port; lock on (pod, row) since tor/mixed reach the row.
        # The lock is held around the reads the ports are chosen from, the plan and its apply.
        cabling_lock_key = f"endpoint-cabling-pod-{pod_id}-row-{rack['row_index']}"
        # LAG endpoints get port-channels in the pair's ManagedMLAG domain instead of 1:1 uplink cabling.
        if bonds:
            cabled_before = await self._wire_bonds(bonds, deployment_type, dc_id, cabling_lock_key)
            if cabled_before is not None:
                await self._fan_out_component_segments(cabled_before)
            return

        cabled_before = await self._cable_uplinks(deployment_type, dc_id, cabling_lock_key)
        if cabled_before is not None:
            await self._fan_out_component_segments(cabled_before)

    async def _wire_bonds(
        self, bonds: list[EndpointBondData], deployment_type: str, dc_id: str, lock_key: str
    ) -> set[str] | None:
        """Wire the server's bonds into the switch pair's MLAG domain; the ids of its NICs that were cabled before, or
        None when this run cabled nothing new.
        """
        endpoint_name = self.data["name"]
        # Members come from their own query so their cables and interface types are loaded.
        lag_nics: list[DcimPhysicalInterface] = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[self.data["id"]],
            role__value="lag",
            include=["cable"],
        )
        nic_by_id = {nic.id: nic for nic in lag_nics}
        members_by_bond = {
            bond["name"]: [nic_by_id[peer["id"]] for peer in bond["member_interfaces"] if peer["id"] in nic_by_id]
            for bond in bonds
        }
        cabled_before = {nic.id for nic in lag_nics if nic.cable and nic.cable.id}
        # Keep additional bonds on the pair earlier bonds are cabled to.
        existing_switch_names = self._extract_cabled_switch_names(
            [nic for nic in lag_nics if nic.cable and nic.cable.id]
        )
        async with self.plan_locks(lock_key):
            targets = await EndpointTargetResolver(
                self.client,
                self.logger,
                self.data,
                # The members stand in for the free uplinks: their types narrow the switch-port search.
                lag_nics,
            ).resolve(deployment_type)
            reads = await read_lag_wiring(
                self.client,
                self.logger,
                endpoint_name=endpoint_name,
                members_by_bond=members_by_bond,
                target_interfaces=targets,
                existing_switch_names=existing_switch_names,
            )
            if reads is not None:
                wiring = plan_lag_wiring(
                    self.logger,
                    reads,
                    endpoint_name=endpoint_name,
                    members_by_bond=members_by_bond,
                    target_interfaces=targets,
                    deployment_id=dc_id,
                    positions=await read_device_positions(self.client, [endpoint_name, reads.switch_a, reads.switch_b]),
                )
                # Both members are cabled before either port-channel exists
                await self.apply_plan(wiring.cabling)
                await self.apply_plan(wiring.port_channels)
        return cabled_before if len(cabled_before) < len(lag_nics) else None

    async def _cable_uplinks(self, deployment_type: str, dc_id: str, lock_key: str) -> set[str] | None:
        """Dual-home the server's free uplinks and keep the cables of its cabled ones; the ids of its NICs cabled before
        when it has targets, else None.
        """
        endpoint_name = self.data["name"]
        # Endpoints use the "uplink" role, ToR/Leaf devices the "customer" role.
        all_endpoint_interfaces: list[DcimPhysicalInterface] = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[self.data["id"]],
            role__value="uplink",
            status__values=FREE_OR_CABLED_INTERFACE_STATUSES,
            include=["device", "interface_type", "cable"],
        )

        # Only interfaces without cables are connected.
        endpoint_interfaces = [intf for intf in all_endpoint_interfaces if not (intf.cable and intf.cable.id)]
        already_cabled_interfaces = [intf for intf in all_endpoint_interfaces if intf.cable and intf.cable.id]
        existing_connections = len(all_endpoint_interfaces) - len(endpoint_interfaces)

        if not endpoint_interfaces:
            if existing_connections > 0:
                self.logger.info(
                    f"Endpoint {endpoint_name} already has {existing_connections} connection(s) - "
                    "all interfaces connected, skipping"
                )
            else:
                self.logger.info(f"Endpoint {endpoint_name} has no uplink interfaces, skipping")
        elif existing_connections > 0:
            self.logger.info(
                f"Endpoint {endpoint_name} already has {existing_connections} connection(s) - "
                f"will create connections for {len(endpoint_interfaces)} free interface(s)"
            )

        async with self.plan_locks(lock_key):
            plan = TopologyPlan(root_id=None)
            # Keep this generator's own cables (named by plan_link) in the run's group, so tracking keeps them
            await self._keep_own_cables(plan, already_cabled_interfaces)
            targets: list[DcimPhysicalInterface] = []
            if endpoint_interfaces:
                targets = await EndpointTargetResolver(
                    self.client,
                    self.logger,
                    self.data,
                    endpoint_interfaces,
                    already_connected=existing_connections > 0,
                ).resolve(deployment_type)
            if targets:
                fingerprints = plan_uplink_connections(
                    endpoint_name=endpoint_name,
                    free_interfaces=endpoint_interfaces,
                    target_interfaces=targets,
                    # Keep additional free ports (extra NICs, re-runs) on the SAME switch pair.
                    existing_switch_names=self._extract_cabled_switch_names(already_cabled_interfaces),
                    logger=self.logger,
                )
                if fingerprints:
                    plan_cables(
                        plan,
                        with_port_types(fingerprints, endpoint_interfaces, targets),
                        deployment_id=dc_id,
                        positions=await read_device_positions(
                            self.client, [endpoint_name, *{fp.switch_name for fp in fingerprints}]
                        ),
                    )
            if plan.specs:
                await self.apply_plan(plan)
        return {intf.id for intf in already_cabled_interfaces} if targets else None

    async def _keep_own_cables(self, plan: TopologyPlan, cabled: list[DcimPhysicalInterface]) -> None:
        """Add to ``plan`` the re-save (tracked, by id alone: the cable keeps what it has) of this endpoint's own cables
        on its ``cabled`` uplinks; one batch fetch.
        """
        cable_ids = sorted({str(intf.cable.id) for intf in cabled})
        if not cable_ids:
            return
        cables = await self.client.filters(kind=DcimCable, ids=cable_ids)
        own_ends = {f"{self.data['name']}-{intf.name.value}" for intf in cabled}
        for cable in cables:
            if _is_generated_uplink_cable(cable, own_ends):
                plan.add(NodeSpec(f"cable-{cable.id}", DcimCable, {"id": cable.id}, track=True))

    def _extract_cabled_switch_names(self, interfaces: list[DcimPhysicalInterface]) -> set[str]:
        """Far-end switch names of this endpoint's already-cabled interfaces."""
        return cable_endpoint_device_names(interfaces) - {self.data["name"]}

    async def _fan_out_component_segments(self, cabled_before: set[str]) -> None:
        """Dispatch add_app_component_segment, without waiting, for the components on this endpoint once this run cabled
        a NIC. `cabled_before` holds the NICs already cabled, so a rerun dispatches nothing.
        """
        endpoint_id = self.data["id"]
        nics = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[endpoint_id],
            role__values=list(ENDPOINT_NIC_ROLES),
            include=["cable"],
        )
        if not {nic.id for nic in nics if nic.cable and nic.cable.id} - cabled_before:
            return

        hosted = await asyncio.gather(
            *(self.client.filters(kind=kind, hosting_device__ids=[endpoint_id]) for kind in HOSTED_INSTANCE_KINDS)
        )
        instance_ids = [endpoint_id, *(instance.id for instances in hosted for instance in instances)]
        components = await self.client.filters(
            kind=AppComponent, instances__ids=instance_ids, include=["member_of_groups"]
        )
        if not components:
            return

        target_group = await self.client.get(kind=CoreGroup, name__value=GROUP_APP_COMPONENTS)
        component_ids = sorted(
            {
                component.id
                for component in components
                if target_group.id in {group.id for group in getattr(component, "member_of_groups").peers}
            }
        )
        if len(component_ids) < len(components):
            self.logger.warning(
                f"Endpoint {self.data['name']}: {len(components) - len(component_ids)} component(s) on it are not "
                f"in {GROUP_APP_COMPONENTS}, so {_COMPONENT_SEGMENT_GENERATOR} cannot run for them"
            )
        await self.run_generator(_COMPONENT_SEGMENT_GENERATOR, component_ids, wait=False)
