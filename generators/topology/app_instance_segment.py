"""Generator assigning a VXLAN segment to the exact switch interface an
AppComponent's instance is cabled to.

A DC-wide sweep used to tag every leaf/tor customer port in a segment's
deployment, whether or not that customer had anything cabled there (see
generators/topology/segment.py, which no longer does this). That
over-provisions every access switch in the DC for a tenant that may only
have a handful of servers. This generator instead reacts to AppComponent —
the thing that actually names where a tenant's compute runs (``instances``)
and which segment it belongs to (``network_segment``) — and tags only the
interface(s) that instance is cabled to.

Resolution per instance:
  - A bare physical device (DcimPhysicalDevice, ManagedControllerPhysical)
    is used directly.
  - A virtual instance (DcimVirtualDevice, ManagedControllerVirtual) walks
    its hosting_device to the physical host actually cabled to the fabric.
  - Anything else (e.g. CloudInstance) has no on-prem port to tag and is
    skipped — cloud segmentation is handled in the Cloud namespace, not here.

For a bonded instance (role=lag NICs bundled into a DcimLAGInterface, see
generators/topology/endpoint.py), the switch-side far end of a member's
cable is itself a role=lag physical port that is a member of the switch's
own port-channel — the port-channel is what carries the segment's VLAN
(ConfigDB/NX-OS trunk config lives on the aggregate, not the raw member),
so that is what gets tagged, not the raw member port.

A segment with no AppComponent at all referencing it gets no customer-port
assignment — there is intentionally no DC-wide fallback (see
generators/topology/segment.py's module docstring).

An instance's physical device may not be cabled yet (add_endpoint has not
run, or hasn't reached it) — nothing is tagged until it is; re-triggering
once cabling lands later is a known gap, not solved here.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..pools import PoolMixin
from ..protocols import (
    DcimCable,
    DcimLAGInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    ManagedVxlanSegment,
)
from ..vlan_domain import VlanDomainMixin

# AppInstance kinds that ARE a physical device/controller — used directly.
_PHYSICAL_INSTANCE_TYPES = frozenset({"DcimPhysicalDevice", "ManagedControllerPhysical"})
# AppInstance kinds hosted BY a physical device — walk hosting_device to it.
_VIRTUAL_INSTANCE_TYPES = frozenset({"DcimVirtualDevice", "ManagedControllerVirtual"})
# Interface roles a tenant's traffic can actually ride on at the device side.
_CUSTOMER_FACING_NIC_ROLES = ("uplink", "lag")


class AppInstanceSegmentGenerator(PoolMixin, VlanDomainMixin, CommonGenerator):
    """Tags the switch interface(s) an AppComponent's instance is cabled to
    with that component's network_segment, and realizes the segment's LOCAL
    VLAN ID on each touched device's VLAN domain (MLAG pair or standalone).
    """

    graphql_root_key = "AppComponent"

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        component_list = cleaned.get(self.graphql_root_key, [])
        if not component_list:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        component = component_list[0]
        fqdn: str = component.get("fqdn", "")
        segment = component.get("network_segment") or {}
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        instances: list[dict[str, Any]] = component.get("instances") or []

        if not segment_id:
            self.logger.info(f"Component {fqdn}: no network_segment set — nothing to assign")
            return
        if not instances:
            self.logger.info(f"Component {fqdn}: no instances set — nothing to assign")
            return

        segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id)
        if not segment_obj:
            self.logger.error(f"Component {fqdn}: could not fetch segment SDK object {segment_id}")
            return

        total_assigned = 0
        touched_device_ids: set[str] = set()
        for instance in instances:
            device_id = self._resolve_physical_device_id(instance)
            if not device_id:
                continue
            assigned, device_ids = await self._tag_instance_interfaces(
                device_id=device_id,
                segment_id=segment_id,
                segment_obj=segment_obj,
                segment_name=segment_name,
            )
            total_assigned += assigned
            touched_device_ids.update(device_ids)

        self.logger.info(
            f"Component {fqdn}: tagged {total_assigned} interface(s) with segment '{segment_name}' "
            f"across {len(touched_device_ids)} switch device(s)"
        )

    @staticmethod
    def _resolve_physical_device_id(instance: dict[str, Any]) -> str | None:
        """The physical device id an AppInstance is actually cabled through.

        A bare physical device/controller is used directly. A virtual
        instance walks its hosting_device. Anything else (e.g. a cloud
        instance) has no on-prem device to resolve."""
        typename = instance.get("typename")
        if typename in _PHYSICAL_INSTANCE_TYPES:
            return instance.get("id")
        if typename in _VIRTUAL_INSTANCE_TYPES:
            hosting = instance.get("hosting_device") or {}
            return hosting.get("id")
        return None

    async def _tag_instance_interfaces(
        self,
        device_id: str,
        segment_id: str,
        segment_obj: Any,
        segment_name: str,
    ) -> tuple[int, set[str]]:
        """Tag every switch interface `device_id` is cabled to (role=uplink
        directly, role=lag via its switch-side port-channel) with the
        segment, and realize that switch's VLAN domain segment. Returns
        (interfaces newly tagged, switch device ids touched)."""
        nics = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[device_id],
            role__values=list(_CUSTOMER_FACING_NIC_ROLES),
            include=["cable"],
        )
        cabled = [nic for nic in nics if nic.cable and nic.cable.id]
        if not cabled:
            self.logger.info(f"  Device {device_id} has no cabled uplink/lag interface yet — nothing to tag")
            return 0, set()

        # Dedup by resolved target id: several bond members can resolve to
        # the SAME switch-side port-channel (two members, one port-channel).
        targets: dict[str, Any] = {}
        for nic in cabled:
            target = await self._resolve_customer_facing_target(nic)
            if target is not None:
                targets[target.id] = target

        if not targets:
            self.logger.info(f"  Device {device_id}: cabled but no customer-facing switch port resolved")
            return 0, set()

        assigned = 0
        touched_device_ids: set[str] = set()
        for target in targets.values():
            iface_services = target.interface_capabilities
            existing_ids = {peer.id for peer in iface_services.peers}
            # Status is left alone: it belongs to the cabling (connections.py
            # marks both cable ends active), not to the segment assignment.
            if segment_id not in existing_ids:
                iface_services.add(segment_obj)
                assigned += 1
                # update_group_context=False: a switch interface belongs to
                # the device's object_template, not to this generator run —
                # never a delete_unused_nodes candidate.
                await target.save(allow_upsert=True, update_group_context=False)
            touched_device_ids.add(target.device.peer.id)

        devices = []
        for switch_device_id in touched_device_ids:
            devices.append(
                await self.client.get(kind=DcimPhysicalDevice, id=switch_device_id, include=["capabilities"])
            )
        domain_pools = await self._ensure_vlan_domains_for_devices(devices)
        for domain_id, pool_id in domain_pools.items():
            await self._ensure_vlan_domain_segment(segment_id, segment_name, domain_id, pool_id)

        return assigned, touched_device_ids

    async def _resolve_customer_facing_target(self, nic: Any) -> Any | None:
        """The far end of `nic`'s cable, as the object to tag: the far-end
        port itself for a plain uplink, or its own switch-side port-channel
        when the far end is a role=lag member — the aggregate is what
        carries the VLAN, not the raw member port. None if the cable has no
        resolvable far end, or a lag far end has no port-channel yet."""
        cable_obj = await self.client.get(kind=DcimCable, id=nic.cable.id, include=["endpoints"])
        far_ends = [p for p in cable_obj.endpoints.peers if p.id != nic.id]
        if not far_ends:
            return None

        far_end = await self.client.get(
            kind=DcimPhysicalInterface,
            id=far_ends[0].id,
            include=["lag", "interface_capabilities", "device"],
        )
        if far_end.role.value != "lag":
            return far_end

        port_channel_peer = getattr(far_end.lag, "peer", None)
        if not port_channel_peer:
            self.logger.warning(f"  Far-end port {far_end.id} is role=lag but has no port-channel yet — skipping")
            return None
        return await self.client.get(
            kind=DcimLAGInterface, id=port_channel_peer.id, include=["interface_capabilities", "device"]
        )
