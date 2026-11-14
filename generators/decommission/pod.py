"""Decommission a pod's devices without deleting them (status "decommissioned"); idempotent. Every write is untracked.
Frees the far end of each removed fabric cable, then deletes the pod's pools and DC-pool slices if empty.
"""

from typing import Any

from infrahub_sdk.batch import InfrahubBatch

from generators.helpers.vrf import VrfMixin
from generators.mixins.common import CommonGenerator
from generators.mixins.pod_config import pod_spine_slot_device_names, spine_slot_role
from generators.mixins.pools import PoolMixin
from generators.protocols import (
    CoreIPAddressPool,
    CoreIPPrefixPool,
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
    ManagedInterfaceEncapsulation,
)
from utils.data_cleaning import clean_data
from utils.interface_encapsulation import encapsulation_name
from utils.vrf_demand import SVI_ROLE

# Device roles add_pod/add_rack create under a pod's deployment
_POD_FABRIC_ROLES = frozenset({"spine", "leaf", "tor", "l2-leaf", "access-leaf", "internal-firewall", "load-balancer"})
# Roles at the far end of a pod device's fabric cable that are not the pod's own devices; never an endpoint
_FAR_END_FABRIC_ROLES = frozenset({"super-spine", "spine", "border-spine", "border-leaf"})
# The pod's pools (TopologyPod field) and their kinds
_POD_POOLS = (("loopback_pool", CoreIPAddressPool), ("prefix_pool", CoreIPPrefixPool))


class PodDecommissionGenerator(PoolMixin, VrfMixin, CommonGenerator):
    async def _filter_by_ids(self, kind: Any, ids: list[str], **kwargs: Any) -> list[Any]:
        """client.filters(ids=...), or [] for no ids (the SDK forwards an empty id list, which does not narrow the
        query).
        """
        unique_ids = sorted(set(ids))
        if not unique_ids:
            return []
        return await self.client.filters(kind=kind, ids=unique_ids, **kwargs)

    def _border_spines(self) -> list[dict[str, Any]]:
        """This pod's border-spines, picked by name out of the DC's (pod_spine_slot_device_names)."""
        if spine_slot_role(self.data.get("fabric_templates") or []) != "border-spine":
            return []
        names = set(pod_spine_slot_device_names(self.data))
        dc_border_spines = (self.data.get("parent") or {}).get("border_spines") or []
        return [device for device in dc_border_spines if device.get("name") in names]

    async def _release_pod_pools(self) -> None:
        """Delete the pod's pools and the slices they hold, each only once nothing is left in it. A slice that still
        holds an address or prefix is kept (warning). The pool goes first, then its slice; a re-run does nothing.
        """
        for field, pool_kind in _POD_POOLS:
            pool = self.data.get(field)
            if not pool:
                continue
            slices = await self._filter_by_ids(
                IpamPrefix,
                [resource["id"] for resource in pool.get("resources") or []],
                include=["ip_addresses", "children"],
            )
            in_use = [prefix.prefix.value for prefix in slices if prefix.ip_addresses.peers or prefix.children.peers]
            if in_use:
                self.logger.warning(
                    f"Pod {self.data.get('name')}: keeping {field} {pool['id']}, its prefix(es) {in_use} "
                    "still hold addresses or prefixes"
                )
                continue
            await self.client.delete(kind=pool_kind, id=pool["id"])
            self.logger.info(f"deleted {field} {pool['id']}")
            for prefix in slices:
                await prefix.delete()
                self.logger.info(f"released prefix {prefix.prefix.value}")

    async def generate(self, data: dict[str, Any]) -> None:
        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod data found in GraphQL response")
                return

            # Only id/name are required for decommission
            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error("Generation failed due to %s", exc)
            return

        pod_devices = [
            device for device in self.data.get("devices", []) or [] if device.get("role") in _POD_FABRIC_ROLES
        ] + self._border_spines()
        self.data["devices"] = pod_devices

        devices: list = [device.get("id") for device in pod_devices if device.get("status") == "active"]

        physical_interfaces: list[str] = [
            interface.get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("status") == "active" and interface.get("typename") == "DcimPhysicalInterface"
        ]

        far_end_interfaces: list[str] = [
            endpoint.get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimPhysicalInterface"
            for endpoint in (interface.get("cable") or {}).get("endpoints", []) or []
            if endpoint.get("id") and (endpoint.get("device") or {}).get("role") in _FAR_END_FABRIC_ROLES
        ]

        virtual_interfaces: list[str] = [
            interface.get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("status") == "active"
            and interface.get("typename") == "DcimVirtualInterface"
            # An SVI is deleted by the VRF reconcile below, under the device locks
            and interface.get("role") != SVI_ROLE
        ]

        p2p_addresses: list[str] = [
            id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimPhysicalInterface"
            for id in [(interface.get("ip_address") or {}).get("id")]
            if isinstance(id, str)
        ]

        far_end_p2p_addresses: list[str] = [
            endpoint_ip_id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("status") == "active" and interface.get("typename") == "DcimPhysicalInterface"
            for endpoint in (interface.get("cable") or {}).get("endpoints", []) or []
            if (endpoint.get("device") or {}).get("role") in _FAR_END_FABRIC_ROLES
            for endpoint_ip_id in [(endpoint.get("ip_address") or {}).get("id")]
            if isinstance(endpoint_ip_id, str)
        ]

        virtual_interface_addresses: list[str] = [
            id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimVirtualInterface"
            # A segment SVI carries the segment's anycast gateway, which outlives the pod
            and interface.get("role") != SVI_ROLE
            for id in [(interface.get("ip_address") or {}).get("id")]
            if isinstance(id, str)
        ]

        device_primary_addresses: list[str] = [
            id
            for device in self.data.get("devices", []) or []
            for id in [(device.get("primary_address") or {}).get("id")]
            if isinstance(id, str)
        ]

        cables: list[str] = [
            cable_id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            for cable_id in [(interface.get("cable") or {}).get("id")]
            if isinstance(cable_id, str)
        ]

        # By id, not prefix value: the same prefix can exist in several namespaces
        p2p_prefix_ids: list[str] = [
            prefix_id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimPhysicalInterface"
            for prefix_id in [((interface.get("ip_address") or {}).get("ip_prefix") or {}).get("id")]
            if isinstance(prefix_id, str)
        ]

        execute_batch: InfrahubBatch = await self.client.create_batch()

        device_objs: list[DcimPhysicalDevice] = await self._filter_by_ids(DcimPhysicalDevice, devices)

        for device_obj in device_objs:
            device_obj.status.value = "decommissioned"
            execute_batch.add(task=device_obj.save, allow_upsert=True, update_group_context=False, node=device_obj)

        async for _node, _ in execute_batch.execute():
            self.logger.info(f"Decommissioned {_node.name.value}")

        execute_batch: InfrahubBatch = await self.client.create_batch()

        interface_objs: list[DcimPhysicalInterface] = await self._filter_by_ids(
            DcimPhysicalInterface, physical_interfaces + far_end_interfaces
        )

        for interface_obj in interface_objs:
            interface_obj.status.value = "free"
            interface_obj.description.value = ""
            execute_batch.add(
                task=interface_obj.save, allow_upsert=True, update_group_context=False, node=interface_obj
            )

        async for _node, _ in execute_batch.execute():
            self.logger.info(
                f"Cleaned interface {_node.device.display_label} - {_node.name.value} status and description"
            )

        interface_objs: list[DcimVirtualInterface] = await self._filter_by_ids(DcimVirtualInterface, virtual_interfaces)
        # An interface does not take its encapsulation with it: delete those first
        encapsulations = (
            await self.client.filters(
                kind=ManagedInterfaceEncapsulation,
                name__values=[
                    encapsulation_name(*hfid) for interface_obj in interface_objs if (hfid := interface_obj.hfid)
                ],
            )
            if interface_objs
            else []
        )
        for encapsulation in encapsulations:
            await encapsulation.delete()
        for interface_obj in interface_objs:
            await interface_obj.delete()
            self.logger.info(f"deleted virtual interface {interface_obj.hfid}")

        address_objs: list[IpamIPAddress] = await self._filter_by_ids(
            IpamIPAddress,
            p2p_addresses + far_end_p2p_addresses + virtual_interface_addresses + device_primary_addresses,
        )
        for address_obj in address_objs:
            await address_obj.delete()
            self.logger.info(f"deleted address {address_obj.address.value}")

        cable_objs: list[DcimCable] = await self._filter_by_ids(DcimCable, cables)
        for cable_obj in cable_objs:
            await cable_obj.delete()
            self.logger.info(f"deleted cable {cable_obj.name.value}")

        prefix_objs: list[IpamPrefix] = await self._filter_by_ids(IpamPrefix, p2p_prefix_ids)
        for prefix_obj in prefix_objs:
            await prefix_obj.delete()
            self.logger.info(f"deleted prefix {prefix_obj.prefix.value}")

        # Decommissioned devices carry no VRF: the reconcile (locks held) deletes their SVIs, memberships and empty
        # instances, also on a re-run
        await self.reconcile_vrfs([device["id"] for device in pod_devices if device.get("id")])

        await self._release_pod_pools()

        # Last: sweep what is left empty in the DC
        if dc_id := (self.data.get("parent") or {}).get("id"):
            await self.prune_empty_vrfs([dc_id])
