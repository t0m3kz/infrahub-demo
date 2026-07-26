"""Rack mixin for rack generator lifecycle gates and context bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import logging

from .helpers import DeviceNamingConfig
from .helpers.rack import rack_sort_key
from .protocols import LocationRack
from .types import RoutingOptions

# Which fabric_templates roles a pod's deployment_type knows how to cable.
#
# - "tor" cables to the pod spines: directly (tor deployment, cumulative
#   offset across the pod) or to the middle rack's leafs in the same row
#   (mixed deployment, calculate_cabling_offsets' mixed+tor branch). Neither
#   path exists for middle_rack — there every leaf lives in the same rack as
#   its ToRs, and _generate_tors() has no local-leaf cabling branch for it.
# - "l2_leaf"/"access_leaf" always cable to a local leaf pair — in the same
#   rack for middle_rack/network racks, or the middle rack in the same row
#   for mixed compute racks (_resolve_local_leaf_cabling_target). tor
#   deployment has no leafs anywhere in the pod, so neither role has a
#   target to cable to.
ROLES_BY_DEPLOYMENT_TYPE: dict[str, frozenset[str]] = {
    "tor": frozenset({"tor", "border_leaf"}),
    "mixed": frozenset({"leaf", "tor", "l2_leaf", "access_leaf", "border_leaf"}),
    "middle_rack": frozenset({"leaf", "l2_leaf", "access_leaf", "border_leaf"}),
}

# "tor" cables to a spine; "l2_leaf"/"access_leaf" cable to a local leaf pair.
# No cabling strategy satisfies both on the same rack, so they're mutually
# exclusive there regardless of deployment_type.
MUTUALLY_EXCLUSIVE_ROLE_GROUPS: tuple[frozenset[str], ...] = (frozenset({"tor", "l2_leaf", "access_leaf"}),)

# rack_type values for racks with no leaf template of their own — they wait
# for, and cable to, the leaf pair in the network/middle rack that shares
# their row. "tor" holds real ToRs (role="tor", cables to spines) and/or
# l2-leaf/access-leaf switches (cable to the row's leaf pair); "compute" is
# the same "no local leaf" shape used for a rack whose fabric_templates are
# l2-leaf/access-leaf only (no role="tor" — see ROLES_BY_DEPLOYMENT_TYPE).
# Checksum inheritance and row-ordering gates treat both identically; only
# the per-role cabling target (spine vs. row leaf) differs.
ROW_DEPENDENT_RACK_TYPES: frozenset[str] = frozenset({"tor", "compute"})


class RackMixin:
    """Mixin extracting rack-specific lifecycle checks and context setup."""

    # Attributes provided by CommonGenerator / RackGenerator
    client: Any
    logger: logging.Logger
    data: Any
    deployment_id: str
    pod_name: str
    fabric_name: str

    async def fetch_rack_devices_with_interfaces(
        self,
        rack: LocationRack | None = None,
        role_filter: str | None = None,
        interface_role: str = "downlink",
    ) -> list[dict]:
        """Implemented by RackGenerator; declared here for static type checking."""
        raise NotImplementedError

    def _derive_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive spine device names and interface names from query data.

        The rack.gql query already fetches pod.amount_of_spines,
        pod.spine_template.interfaces(role="downlink"), and all naming
        indexes (dc.index, pod.index). The pod generator always creates
        spines with strategy="standard" and indexes=[dc.index, pod.index],
        so spine names are deterministic - no API call needed.

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        pod = self.data.pod
        dc = pod.parent
        spine_count = pod.amount_of_spines
        spine_template = pod.spine_template

        if not spine_count or not spine_template:
            raise RuntimeError(
                f"Rack {self.data.name}: Cannot derive spine info - "
                f"amount_of_spines={spine_count}, spine_template={'set' if spine_template else 'None'}"
            )

        naming = DeviceNamingConfig(strategy=dc.naming_convention)
        spine_indexes = [dc.index, pod.index]
        device_names = sorted(
            [
                naming.format_device_name(
                    self.fabric_name,
                    "spine",
                    index=idx,
                    fabric_name=self.fabric_name,
                    indexes=spine_indexes,
                )
                for idx in range(1, spine_count + 1)
            ]
        )

        interface_names = sorted(iface.name for iface in spine_template.interfaces)

        if not interface_names:
            raise RuntimeError(f"Rack {self.data.name}: Spine template has no downlink interfaces")

        self.logger.info(
            f"Derived {len(device_names)} spine device names with "
            f"{len(interface_names)} downlink interface(s) from query data"
        )
        return device_names, interface_names

    def _derive_super_spine_info(self) -> tuple[list[str], list[str]]:
        """Derive super-spine device names and uplink interface names from query data.

        The DC generator always creates super-spines with indexes=[dc.index], so names
        are deterministic - no API call needed. Super-spine template interfaces are
        pre-filtered to role="uplink" in rack.gql.

        Returns:
            Tuple of (device_names, interface_names) for create_cabling.

        Raises:
            RuntimeError: when super-spine count or template is not set on the DC.
        """
        dc = self.data.pod.parent
        count = dc.amount_of_super_spines
        template = dc.super_spine_template

        if not count or not template:
            raise RuntimeError(
                f"Rack {self.data.name}: Cannot derive super-spine info - "
                f"amount_of_super_spines={count}, super_spine_template={'set' if template else 'None'}"
            )

        naming = DeviceNamingConfig(strategy=self.data.pod.parent.naming_convention)
        device_names = sorted(
            naming.format_device_name(
                self.fabric_name,
                "super-spine",
                index=idx,
                fabric_name=self.fabric_name,
                indexes=[dc.index],
            )
            for idx in range(1, count + 1)
        )

        interface_names = sorted(iface.name for iface in template.interfaces if iface.role == "uplink")
        if not interface_names:
            raise RuntimeError(f"Rack {self.data.name}: Super-spine template has no uplink interfaces")

        self.logger.info(
            f"Derived {len(device_names)} super-spine device names with "
            f"{len(interface_names)} uplink interface(s) from query data"
        )
        return device_names, interface_names

    async def _checksum_ready(self) -> bool:
        """Return True if this rack has a usable checksum; False if generation should abort."""
        if self.data.checksum:
            return True

        deployment_type = self.data.pod.deployment_type

        if deployment_type == "mixed" and self.data.rack_type in ROW_DEPENDENT_RACK_TYPES:
            middle_racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[self.data.pod.id],
                row_index__value=self.data.row_index,
                rack_type__value="network",
            )
            sorted_middle_racks = sorted(middle_racks, key=rack_sort_key)
            source_middle_rack = next((r for r in sorted_middle_racks if r.checksum.value), None)
            if source_middle_rack:
                rack_obj = await self.client.get(kind=LocationRack, id=self.data.id)
                rack_obj.checksum.value = source_middle_rack.checksum.value
                await rack_obj.save(allow_upsert=True)
                self.logger.info(
                    f"Rack {self.data.name} inherited checksum {source_middle_rack.checksum.value} "
                    f"from middle rack {source_middle_rack.name.value}. "
                    "Checksum update will trigger generator again to create devices."
                )
            else:
                self.logger.warning(
                    f"Rack {self.data.name} has no checksum and no middle rack found "
                    f"in row {self.data.row_index} - skipping generation."
                )
            return False

        if deployment_type == "middle_rack" and self.data.rack_type == "network":
            sibling_racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[self.data.pod.id],
                row_index__value=self.data.row_index,
                rack_type__value="network",
            )
            sibling_with_checksum = sorted(
                (r for r in sibling_racks if r.id != self.data.id and r.checksum.value),
                key=rack_sort_key,
            )
            if sibling_with_checksum:
                rack_obj = await self.client.get(kind=LocationRack, id=self.data.id)
                rack_obj.checksum.value = sibling_with_checksum[0].checksum.value
                await rack_obj.save(allow_upsert=True)
                self.logger.info(
                    f"Network rack {self.data.name} inherited checksum {sibling_with_checksum[0].checksum.value} "
                    f"from sibling rack {sibling_with_checksum[0].name.value}. "
                    "Checksum update will trigger generator again to create devices."
                )
            else:
                self.logger.warning(
                    f"Network rack {self.data.name} has no checksum and no sibling network rack found "
                    f"in row {self.data.row_index} to inherit from - run pod generator first."
                )
            return False

        self.logger.warning(
            f"Rack {self.data.name} has no checksum set - skipping generation. "
            "Checksum will be set by pod or middle rack generator."
        )
        return False

    async def _earlier_rows_ready(self) -> bool:
        """Row-by-row gate for deterministic generation order."""
        current_row = self.data.row_index
        if current_row <= 1:
            return True

        deployment_type = self.data.pod.deployment_type
        rack_type = self.data.rack_type

        ordered_rack_types: list[str] | None = None
        if rack_type == "network" and deployment_type in ("mixed", "middle_rack"):
            ordered_rack_types = ["network"]
        elif rack_type in ROW_DEPENDENT_RACK_TYPES and deployment_type in ("tor", "mixed"):
            ordered_rack_types = sorted(ROW_DEPENDENT_RACK_TYPES)

        if not ordered_rack_types:
            return True

        sibling_racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.pod.id],
            rack_type__values=ordered_rack_types,
        )

        previous_rows = [r for r in sibling_racks if getattr(getattr(r, "row_index", None), "value", 0) < current_row]
        if not previous_rows:
            return True

        blocked = [
            r for r in previous_rows if not (getattr(r, "checksum", None) and getattr(r.checksum, "value", None))
        ]
        if not blocked:
            return True

        blocked_names = sorted(getattr(getattr(r, "name", None), "value", "unknown-rack") for r in blocked)
        self.logger.info(
            f"Rack {self.data.name} waiting for earlier row {'/'.join(ordered_rack_types)} rack(s): {blocked_names}"
        )
        return False

    async def _tor_leafs_ready(self) -> bool:
        """Mixed deployment: ToR racks wait for the network rack's leafs to exist first."""
        network_racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.pod.id],
            row_index__value=self.data.row_index,
            rack_type__value="network",
        )

        if not network_racks:
            self.logger.info(
                f"ToR rack {self.data.name} waiting for network rack in row {self.data.row_index} - skipping this run."
            )
            return False

        source_network_rack = sorted(network_racks, key=rack_sort_key)[0]
        leaf_data = await self.fetch_rack_devices_with_interfaces(
            rack=source_network_rack,
            role_filter="leaf",
        )

        if not leaf_data:
            self.logger.info(
                f"ToR rack {self.data.name} waiting for leafs to be generated in row {self.data.row_index} - skipping this run."
            )
            return False

        self.logger.info(
            f"ToR rack {self.data.name} found {len(leaf_data)} leaf devices in row {self.data.row_index} "
            "- proceeding with ToR generation"
        )
        return True

    def _prepare_generation_context(self) -> bool:
        """Compute and store shared context needed by every per-role generation method."""
        pod = self.data.pod
        dc = pod.parent
        self.deployment_id = dc.id
        self.pod_name = pod.name.lower()
        self.fabric_name = dc.name.lower()

        if not pod.loopback_pool or not pod.prefix_pool:
            self.logger.error(
                f"Rack {self.data.name}: Pod {pod.name} pools not found. "
                f"Run pod generator first: infrahubctl generator generate_pod name={pod.name}"
            )
            return False

        self._management_pool_id = dc.management_pool.id if dc.management_pool else None
        self._loopback_pool_id = pod.loopback_pool.id if pod.loopback_pool else None

        suite = self.data.parent
        self._device_indexes: list[int] = [
            dc.index,
            pod.index,
            suite.index,
            self.data.row_index,
            self.data.index,
        ]

        if pod.deployment_type == "tor":
            self.logger.info(
                f"ToR rack {self.data.name}: using suite={suite.index}, row={self.data.row_index}, "
                f"rack_index={self.data.index}"
            )

        dc_design = dc.design
        self._naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            dc.naming_convention,
        )
        self._is_ipv6 = dc_design.is_ipv6 if dc_design else False

        try:
            self._spine_device_names, self._spine_interfaces = self._derive_spine_info()
        except RuntimeError as exc:
            self.logger.error(str(exc))
            return False

        routing_options: RoutingOptions = RoutingOptions(design=dc_design)
        if pod.asn_pool and pod.asn_pool.id:
            routing_options["asn_pool"] = pod.asn_pool.id

        self._technical_pool_id = pod.prefix_pool.id if pod.prefix_pool else None
        self._p2p_prefix_length = 127 if dc_design and getattr(dc_design, "p2p_ipv6", False) else 31
        self._routing_options = routing_options
        return True
