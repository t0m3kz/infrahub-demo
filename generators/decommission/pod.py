"""Decommission a pod's devices without deleting them.

Sets the pod's fabric devices (the ones add_pod/add_rack create) to status
"decommissioned". Idempotent: re-running on the same pod simply reaffirms the
status.

Owns nothing, so every write is untracked: the devices and interfaces belong
to add_pod/add_rack. Tracked, the next run (which finds the devices already
decommissioned and saves nothing) would delete them in its cleanup.

A border-spine pod's border-spines are deployed to the DC, not the pod, so
they are picked out of the DC's border-spines by name. The far end of every
removed fabric cable (a super-spine, another pod's spine, a border-leaf) is
freed with it. Last, the pod's two pools and the prefixes they were sliced
from the DC pools are deleted (add_pod's, released for the DC's next pod),
but only a slice that nothing is left in.

Endpoints (servers) are left alone even though add_endpoint points their
deployment at the pod: they are data-loaded, not built by the pod. Their
cables into the pod's switches are still removed, from the switch side.
"""

from typing import Any

from infrahub_sdk.batch import InfrahubBatch

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..pod_config import pod_spine_slot_device_names, spine_slot_role
from ..protocols import (
    CoreIPAddressPool,
    CoreIPPrefixPool,
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
)

# Device roles add_pod/add_rack create under a pod's deployment (spine, the
# rack switch tiers, and a border-spine pod's own firewall/load-balancer).
_POD_FABRIC_ROLES = frozenset({"spine", "leaf", "tor", "l2-leaf", "access-leaf", "firewall", "load-balancer"})
# Roles at the far end of a pod device's fabric cable that are not the pod's own
# devices: its spines' super-spines, another pod's spines (back-to-back mesh) and
# the border-leafs cabled to its spines. Never an endpoint (left alone, see above).
_FAR_END_FABRIC_ROLES = frozenset({"super-spine", "spine", "border-spine", "border-leaf"})
# The pod's pools (TopologyPod field) and their kinds.
_POD_POOLS = (("loopback_pool", CoreIPAddressPool), ("prefix_pool", CoreIPPrefixPool))


class PodDecommissionGenerator(CommonGenerator):
    async def _filter_by_ids(self, kind: Any, ids: list[str], **kwargs: Any) -> list[Any]:
        """client.filters(ids=...), or [] for no ids: the SDK forwards an
        empty id list, which does not narrow the query at all."""
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
        """Delete the pod's pools and the slices they hold, each only once nothing is left in it.

        A slice is empty once this run removed the pod devices' addresses and
        P2P prefixes; one that still holds an address or a prefix is kept
        (warning), so the DC pool can never hand its range to another pod
        while something still sits in it. The pool goes first, then its slice
        (freed in the DC pool it was allocated from). A re-run finds no pool
        and does nothing.
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

            # Keep raw pod dict; only id/name are required for decommission workflow.
            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error("Generation failed due to %s", exc)
            return

        # Only the pod's own fabric devices — see the module docstring.
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
            if interface.get("status") == "active" and interface.get("typename") == "DcimVirtualInterface"
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

        # By id, not by prefix value: the same prefix can exist in several IP
        # namespaces, and a value filter would delete every one of them.
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

        # Set all physical interfaces to free and remove descriptions
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

        # Remove all virtual interfaces
        interface_objs: list[DcimVirtualInterface] = await self._filter_by_ids(DcimVirtualInterface, virtual_interfaces)
        for interface_obj in interface_objs:
            await interface_obj.delete()
            self.logger.info(f"deleted virtual interface {interface_obj.hfid}")

        # Remove all ip addresses
        address_objs: list[IpamIPAddress] = await self._filter_by_ids(
            IpamIPAddress,
            p2p_addresses + far_end_p2p_addresses + virtual_interface_addresses + device_primary_addresses,
        )
        for address_obj in address_objs:
            await address_obj.delete()
            self.logger.info(f"deleted address {address_obj.address.value}")

        # Remove all cables attached to pod interfaces (after cleaning interfaces/IPs)
        cable_objs: list[DcimCable] = await self._filter_by_ids(DcimCable, cables)
        for cable_obj in cable_objs:
            await cable_obj.delete()
            self.logger.info(f"deleted cable {cable_obj.name.value}")

        # Remove all p2p prefixes from pools
        prefix_objs: list[IpamPrefix] = await self._filter_by_ids(IpamPrefix, p2p_prefix_ids)
        for prefix_obj in prefix_objs:
            await prefix_obj.delete()
            self.logger.info(f"deleted prefix {prefix_obj.prefix.value}")

        await self._release_pod_pools()
