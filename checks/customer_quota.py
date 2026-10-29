"""Meter each DC customer footprint's segments and host-facing ports against its design.

A footprint's `design` (TopologyCustomerTemplateDC, data/bootstrap/21_customer_templates.yml) is its
service tier. Besides the dedicated firewall/load balancer flags that generators/firewall_context.py
reads, it carries what the customer is entitled to, and this check holds the footprint to it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from generators.vlan_domain import SERVICE_PORT_ROLES
from utils.data_cleaning import clean_data
from utils.exchange_transit import is_exchange_zone
from utils.interface_speed import InterfaceSpeedMatcher

from .common import deployment_scope_ids

FOOTPRINT_KIND = "TopologyCustomerDC"


class CheckCustomerQuota(InfrahubCheck):
    """A DC footprint with a design stays within the design's segment and port numbers (all ERRORs).

    What is counted, for every TopologyCustomerDC that has a `design`:

    - segments: the ManagedVxlanSegments (customer_deployments) that name the footprint, against ``max_segments``. A segment stretched over two DCs counts for each
      footprint it names.
    - host-facing ports: the switch ports tagged with those segments (their `switched_interfaces`: add_app_component_segment
      tags the far end of the uplink NICs of every AppComponent instance's physical host, a role=lag NIC's far end
      through its port-channel), grouped by speed, against ``ports_<speed>g``. A port counts once per footprint
      however many of its segments tag it, and once for each footprint that has a segment on it (a host serving
      two customers). A port-channel counts its member ports (one per NIC, the speed of the member's
      interface_type). Not counted: the border-leaf firewall/load-balancer ports (the inline termination's
      service ports, not host-facing), interfaces that are neither physical ports nor port-channels, and a
      port in another DC than the footprint's (a stretched segment is tagged in every DC it spans).
    - exchange-zone segments (INTERNET/MANAGEMENT, the DC-local infra services) are the operator's, not the
      customer's: they count for neither.

    Zero means none allowed. A speed without a `ports_<speed>g` attribute (only 25G and 100G host ports exist in
    the model: the server device types carry no other NIC) is not metered. A footprint without a design, and
    every footprint that is not a data center one (colocation, cloud, office), is not metered.
    """

    query = "customer_quota"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        footprints = {fp["id"]: fp for fp in cleaned.get(FOOTPRINT_KIND) or [] if fp.get("design")}
        segments: dict[str, set[str]] = defaultdict(set)  # footprint id -> segment ids
        ports: dict[str, dict[str, int]] = defaultdict(dict)  # footprint id -> port id -> speed (Gbps)

        for segment in cleaned.get("ManagedVxlanSegment") or []:
            self._collect(segment, footprints, segments, ports)

        for footprint_id, footprint in footprints.items():
            self._validate_footprint(footprint, len(segments[footprint_id]), ports[footprint_id])

    @staticmethod
    def _collect(
        segment: dict[str, Any],
        footprints: dict[str, dict[str, Any]],
        segments: dict[str, set[str]],
        ports: dict[str, dict[str, int]],
    ) -> None:
        """Add the segment and its host-facing ports to every metered footprint it names."""
        if is_exchange_zone((segment.get("security_zone") or {}).get("name")):
            return
        host_ports = [
            port for interface in segment.get("switched_interfaces") or [] for port in _host_facing_ports(interface)
        ]
        for deployment in segment.get("customer_deployments") or []:
            footprint = footprints.get(deployment.get("id", ""))
            if footprint is None:
                continue
            segments[footprint["id"]].add(segment["id"])
            dc_id = (footprint.get("parent") or {}).get("id")
            for port_id, speed, scope in host_ports:
                if not scope or dc_id in scope:
                    ports[footprint["id"]][port_id] = speed

    def _validate_footprint(self, footprint: dict[str, Any], segment_count: int, ports: dict[str, int]) -> None:
        name = footprint.get("name", "<unnamed-footprint>")
        design = footprint["design"]
        design_name = design.get("name", "<unnamed-design>")

        if segment_count > design["max_segments"]:
            self.log_error(
                message=(
                    f"Customer footprint '{name}' has {segment_count} segments, over the {design['max_segments']} "
                    f"its design '{design_name}' allows (max_segments)"
                ),
                object_id=footprint.get("id"),
                object_type=FOOTPRINT_KIND,
            )

        used_by_speed: dict[int, int] = defaultdict(int)
        for speed in ports.values():
            used_by_speed[speed] += 1
        for speed, used in sorted(used_by_speed.items()):
            allowed = design.get(f"ports_{speed}g")
            if allowed is not None and used > allowed:
                self.log_error(
                    message=(
                        f"Customer footprint '{name}' has {used} host-facing {speed}G ports tagged with its "
                        f"segments, over the {allowed} its design '{design_name}' allows (ports_{speed}g)"
                    ),
                    object_id=footprint.get("id"),
                    object_type=FOOTPRINT_KIND,
                )


def _host_facing_ports(interface: dict[str, Any]) -> list[tuple[str, int, set[str]]]:
    """(port id, speed in Gbps, ids of the port's device deployment and its parent) of one switched interface.

    A physical port is itself; a port-channel is its member ports. Anything that is not a host-facing port,
    or whose speed its interface_type does not state or is below 1G (no ``ports_<speed>g`` meters it), yields nothing.
    """
    if interface.get("role") in SERVICE_PORT_ROLES:
        return []
    scope = deployment_scope_ids((interface.get("device") or {}).get("deployment"))
    physical = [interface] if interface["typename"] == "DcimPhysicalInterface" else interface.get("member_interfaces")
    return [
        (port["id"], speed_mbps // 1000, scope)
        for port in physical or []
        if (speed_mbps := InterfaceSpeedMatcher.extract_speed(port.get("interface_type"))) and speed_mbps >= 1000
    ]
