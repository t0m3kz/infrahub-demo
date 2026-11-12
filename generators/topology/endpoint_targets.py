"""Target resolution of the endpoint generator: which switch interfaces a server dual-homes onto (plain uplink and LAG
flows).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..helpers.rack import pod_row_racks
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface

# DcimInterface statuses of a port that is free to cable or already cabled by this endpoint (a re-run).
FREE_OR_CABLED_INTERFACE_STATUSES = ["free", "active"]


def interface_type_of(intf: Any) -> str | None:
    """An interface's interface_type value, or None when unset."""
    return intf.interface_type.value or None


class EndpointTargetsMixin:
    """Mixin resolving the switch interfaces an endpoint is cabled to, per deployment type; the host provides the
    attributes below.
    """

    client: Any
    logger: logging.Logger
    data: Any
    _free_interfaces: list[DcimPhysicalInterface]
    _already_connected: bool

    async def _resolve_target_interfaces(self, deployment_type: str) -> list[DcimPhysicalInterface]:
        """Resolve the target switch interfaces for this endpoint's deployment type."""
        if deployment_type == "middle_rack":
            return await self._connect_middle_rack_deployment()
        if deployment_type == "tor":
            return await self._connect_tor_deployment()
        return await self._connect_mixed_deployment()

    async def _connect_middle_rack_deployment(self) -> list[DcimPhysicalInterface]:
        """Resolve target interfaces for middle_rack: the row's network rack, ToR first, then l2-leaf/access-leaf, then
        Leaf.
        """
        # Validated in generate() before this call.
        assert self.data.get("rack") is not None, "Rack must be assigned"
        rack = self.data["rack"]
        assert rack is not None

        self.logger.info(
            f"Endpoint {self.data['name']} is in {rack['rack_type']} rack "
            f"(row {rack['row_index']}), searching for ToR/L2-leaf/access-leaf/Leaf switches "
            "in middle rack (network rack) in same row"
        )

        racks = pod_row_racks(rack["deployment"], rack["row_index"], {"network"})

        if not racks:
            self.logger.error(
                f"Endpoint {self.data['name']}: No network rack found in row {rack['row_index']} for middle_rack deployment."
            )
            return []

        rack_ids = [r["id"] for r in racks]
        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self._free_interfaces,
        )

        # Fall back to the L2 aggregation layer, ahead of the routed Leaf layer.
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces found in network rack for {self.data['name']}, trying l2-leaf/access-leaf switches"
            )
            all_target_interfaces = await self._query_l2_aggregation_layer(rack_ids)

        # Fall back to Leaf devices.
        if not all_target_interfaces:
            self.logger.info(f"No l2-leaf/access-leaf interfaces found for {self.data['name']}, trying Leaf switches")
            all_target_interfaces = await self._query_interfaces_by_location(
                rack_ids=rack_ids,
                device_role="leaf",
                endpoint_interfaces=self._free_interfaces,
            )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No free interfaces found on ToR, l2-leaf/access-leaf, or Leaf "
                "devices in middle rack. Cannot create endpoint connectivity."
            )

        return all_target_interfaces

    async def _query_l2_aggregation_layer(self, rack_ids: list[str]) -> list[DcimPhysicalInterface]:
        """Try l2-leaf, then access-leaf, in the given racks."""
        for device_role in ("l2-leaf", "access-leaf"):
            interfaces = await self._query_interfaces_by_location(
                rack_ids=rack_ids,
                device_role=device_role,
                endpoint_interfaces=self._free_interfaces,
            )
            if interfaces:
                return interfaces
        return []

    async def _connect_tor_deployment(self) -> list[DcimPhysicalInterface]:
        """Resolve target interfaces for tor: ToR switches in the same rack, falling back to the same row."""
        # Validated in generate() before this call.
        assert self.data.get("rack") is not None, "Rack must be assigned"
        rack = self.data["rack"]
        assert rack is not None

        rack_ids = [rack["id"]]

        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self._free_interfaces,
        )

        # Fall back to the same row.
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces in same rack for {self.data['name']}, searching same row {rack['row_index']}"
            )

            racks = pod_row_racks(rack["deployment"], rack["row_index"])

            if racks:
                all_target_interfaces = await self._query_interfaces_by_location(
                    rack_ids=[r["id"] for r in racks],
                    device_role="tor",
                    endpoint_interfaces=self._free_interfaces,
                )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No ToR interfaces found in tor deployment. "
                "Cannot create endpoint connectivity."
            )

        return all_target_interfaces

    async def _connect_mixed_deployment(self) -> list[DcimPhysicalInterface]:
        """Resolve target interfaces for mixed: ToRs in the rack, then its l2-leaf/access-leaf, then the middle rack's
        l2-leaf/access-leaf/leaf in the row.
        """
        # Validated in generate() before this call.
        assert self.data.get("rack") is not None, "Rack must be assigned"
        rack = self.data["rack"]
        assert rack is not None

        rack_ids = [rack["id"]]

        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self._free_interfaces,
        )

        # Then the L2 aggregation layer in the same rack.
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces in same rack for {self.data['name']}, trying l2-leaf/access-leaf in same rack"
            )
            all_target_interfaces = await self._query_l2_aggregation_layer(rack_ids)

        # Then the network rack of the same row: l2-leaf/access-leaf, then Leaf.
        if not all_target_interfaces:
            self.logger.info(
                f"No local ToR/l2-leaf/access-leaf for {self.data['name']}, "
                f"trying middle rack switches in same row {rack['row_index']}"
            )

            racks = pod_row_racks(rack["deployment"], rack["row_index"], {"network"})

            if racks:
                network_rack_ids = [r["id"] for r in racks]
                all_target_interfaces = await self._query_l2_aggregation_layer(network_rack_ids)
                if not all_target_interfaces:
                    all_target_interfaces = await self._query_interfaces_by_location(
                        rack_ids=network_rack_ids,
                        device_role="leaf",
                        endpoint_interfaces=self._free_interfaces,
                    )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No ToR, l2-leaf/access-leaf, or Leaf interfaces found "
                "in mixed deployment. Cannot create endpoint connectivity."
            )

        return all_target_interfaces

    async def _query_interfaces_by_location(
        self,
        rack_ids: list[str],
        device_role: Literal["tor", "leaf", "l2-leaf", "access-leaf"],
        endpoint_interfaces: list[Any],
    ) -> list[DcimPhysicalInterface]:
        """Query free interfaces on devices in specific racks."""
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            rack__ids=rack_ids,
            role__value=device_role,
            status__values=["active", "free", "provisioning"],
        )

        if not devices:
            self.logger.info(f"No {device_role} devices found in {len(rack_ids)} rack(s)")
            return []

        endpoint_types = [t for t in (interface_type_of(intf) for intf in endpoint_interfaces) if t]

        # Servers uplink to the "customer"/"downlink" ports of ToR/Leaf devices.
        acceptable_roles = ["downlink", "customer"]
        device_ids = [dev.id for dev in devices]
        # First attempt: only unoccupied ports; on a re-run also accept ports this endpoint already cabled.
        status_filter: dict[str, Any] = (
            {"status__values": FREE_OR_CABLED_INTERFACE_STATUSES}
            if self._already_connected
            else {"status__value": "free"}
        )
        intf_filters: dict[str, Any] = {
            "kind": DcimPhysicalInterface,
            "device__ids": device_ids,
            **status_filter,
            "role__values": acceptable_roles,
            "include": ["device", "interface_type", "cable"],
        }
        if endpoint_types:
            intf_filters["interface_type__values"] = endpoint_types
        all_interfaces = await self.client.filters(**intf_filters)

        self.logger.debug(f"Query returned {len(all_interfaces)} interfaces before cable filter")
        self.logger.debug(f"Device IDs: {device_ids}")
        self.logger.debug(f"Endpoint types: {endpoint_types}")
        self.logger.debug(f"Acceptable roles: {acceptable_roles}")

        free_interfaces = [intf for intf in all_interfaces if not (intf.cable and intf.cable.id)]

        filtered_count = len(all_interfaces) - len(free_interfaces)
        if filtered_count > 0:
            self.logger.debug(f"Filtered out {filtered_count} interfaces with cables")
            for intf in all_interfaces[:3]:
                self.logger.debug(
                    f"  Interface {intf.name.value}: cable={intf.cable.id if intf.cable else None}, "
                    f"status={intf.status.value}"
                )

        self.logger.info(
            f"Found {len(free_interfaces)} free interfaces on {len(devices)} {device_role} device(s) in {len(rack_ids)} rack(s) "
            f"(interface_types={endpoint_types or 'any'}, roles={acceptable_roles})"
        )

        return free_interfaces
