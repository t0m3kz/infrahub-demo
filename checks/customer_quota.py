"""Meter each customer footprint (data center, colocation, cloud) against its design.

A footprint's `design` (TopologyCustomerTemplateDC, ...Colocation or ...Cloud, data/bootstrap/21_customer_templates.yml)
is its service tier and every footprint has one (the relationship is required). Besides the dedicated firewall/load
balancer flags that generators/firewall_context.py reads and the address block that
generators/topology/segment_addressing.py carves for the customer's prefix pool, it carries what the customer is
entitled to, and this check holds the footprint to it.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from typing import Any

from checks.base import WarningCheck
from utils.data_cleaning import clean_data
from utils.exchange_transit import SERVICE_CONTEXT_ROLES, is_exchange_zone
from utils.interface_speed import InterfaceSpeedMatcher
from utils.limits import CLOUD_RANGE_PREFIX_LENGTH
from utils.segment_addressing import IPV4, IPV6, segment_prefix_length
from utils.segment_vlan import port_segments

from .common import deployment_scope_ids

FOOTPRINT_KIND = "TopologyCustomerDC"
COLOCATION_KIND = "TopologyCustomerColocation"
CLOUD_KIND = "TopologyCustomerCloud"
FOOTPRINT_KINDS = (FOOTPRINT_KIND, COLOCATION_KIND, CLOUD_KIND)
CLOUD_TEMPLATE_KIND = "TopologyCustomerTemplateCloud"
BLOCK_TEMPLATE_KINDS = ("TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo")
SWITCHED_KINDS = ("DcimPhysicalInterface", "DcimLAGInterface")


class CheckCustomerQuota(WarningCheck):
    """A footprint stays within the numbers of its design, and the design holds its tier (ERRORs, one WARNING).

    What is counted, for every footprint (data center, colocation, cloud):

    - segments: for a data center or colocation footprint the ManagedVxlanSegments (customer_deployments) that name it,
      for a cloud footprint its CloudNetworkSegments, against ``max_segments``. A segment stretched over two DCs counts
      for each footprint it names. Exchange-zone segments (INTERNET/MANAGEMENT, the DC-local infra services) are the
      operator's, not the customer's: they count for neither.
    - host-facing ports (data center only): the switch ports tagged with those segments (a deployment of the segment
      among the port's `capabilities`, read from the port: add_app_component_segment
      tags the far end of the uplink NICs of every AppComponent instance's physical host, a role=lag NIC's far end
      through its port-channel), grouped by speed, against ``ports_<speed>g``. A port counts once per footprint
      however many of its segments tag it, and once for each footprint that has a segment on it (a host serving
      two customers). A port-channel counts its member ports (one per NIC, the speed of the member's
      interface_type). Not counted: the border-leaf firewall/load-balancer ports (the service ports a terminated
      segment is tagged on, not host-facing), interfaces that are neither physical ports nor port-channels, and a
      port in another DC than the footprint's (a stretched segment is tagged in every DC it spans).
    - virtual networks (cloud only): the CloudVirtualNetworks of the footprint's account in the footprint's region,
      against ``max_virtual_networks``.

    Zero means none allowed. A speed without a `ports_<speed>g` attribute (only 25G and 100G host ports exist in
    the model: the server device types carry no other NIC) is not metered.

    Besides the quota, the tier itself. A cloud template: its ``address_prefix_length`` block holds
    ``max_virtual_networks`` routed ranges of ``CLOUD_RANGE_PREFIX_LENGTH`` (/29), one per virtual network (ERROR naming
    the tier), and a WARNING for an attachment to a virtual network of a footprint with no ``allowed_prefixes`` (neither
    authored nor the routed range add_cloud_attachment_range writes: the BGP prefix list falls back to the network's CIDR
    blocks). A data center or colocation template (a cloud template has none of these other numbers): its IPv4 block holds ``max_segments`` segments of ``segment_prefix_length`` and its IPv6 block
    ``max_segments`` segments of ``ipv6_segment_prefix_length``, and a segment is never larger than its block (ERRORs
    naming the tier). A cloud segment with a ``host_count`` and a ``cidr_block`` has a block at least as large as the
    host count needs (the same sizing as the VXLAN allocator, ``utils/segment_addressing.py``), and is IPv4 (ERROR: it
    carries a provider ``cidr_block``). A VXLAN segment's gateway prefix is of its ``ip_version`` (ERROR), and a
    ``host_count`` on an IPv6 segment is a WARNING (it sizes nothing there). And a WARNING for a VXLAN segment whose
    footprints are of different kinds (the customer pool that addresses it is the one of the first kind, data center
    before colocation).
    """

    query = "customer_quota"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        footprints: dict[str, dict[str, Any]] = {
            fp["id"]: {**fp, "kind": kind} for kind in FOOTPRINT_KINDS for fp in cleaned.get(kind) or []
        }
        segments: dict[str, set[str]] = defaultdict(set)  # footprint id -> segment ids
        ports: dict[str, dict[str, int]] = defaultdict(dict)  # footprint id -> port id -> speed (Gbps)

        ports_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for kind in SWITCHED_KINDS:
            for port in cleaned.get(kind) or []:
                for tagged in port_segments(port):
                    ports_by_segment[tagged["id"]].append(port)

        for segment in cleaned.get("ManagedVxlanSegment") or []:
            self._collect(segment, ports_by_segment[segment["id"]], footprints, segments, ports)
            self._warn_mixed_kinds(segment)
            self._validate_ip_version(segment)

        for fp in footprints.values():
            if fp["kind"] == CLOUD_KIND:
                segments[fp["id"]] = {s["id"] for s in fp.get("network_segments") or []}
        networks = self._virtual_networks_by_footprint(cleaned.get("CloudVirtualNetwork") or [], footprints)
        for footprint_id, footprint in footprints.items():
            self._validate_footprint(
                footprint, len(segments[footprint_id]), ports[footprint_id], networks[footprint_id]
            )
        for kind in BLOCK_TEMPLATE_KINDS:
            for template in cleaned.get(kind) or []:
                self._validate_block(kind, template)
        for template in cleaned.get(CLOUD_TEMPLATE_KIND) or []:
            self._validate_cloud_tier(template)
        self._warn_attachments_without_range(cleaned.get("CloudGatewayAttachment") or [], footprints, networks)
        for segment in cleaned.get("CloudNetworkSegment") or []:
            self._validate_cloud_block(segment)

    @staticmethod
    def _collect(
        segment: dict[str, Any],
        tagged_ports: list[dict[str, Any]],
        footprints: dict[str, dict[str, Any]],
        segments: dict[str, set[str]],
        ports: dict[str, dict[str, int]],
    ) -> None:
        """Add the segment and its host-facing ports to every metered footprint it names."""
        if is_exchange_zone((segment.get("security_zone") or {}).get("name")):
            return
        host_ports = [port for interface in tagged_ports for port in _host_facing_ports(interface)]
        for deployment in segment.get("customer_deployments") or []:
            footprint = footprints.get(deployment.get("id", ""))
            if footprint is None:
                continue
            segments[footprint["id"]].add(segment["id"])
            if footprint["kind"] != FOOTPRINT_KIND:
                continue
            dc_id = (footprint.get("parent") or {}).get("id")
            for port_id, speed, scope in host_ports:
                if not scope or dc_id in scope:
                    ports[footprint["id"]][port_id] = speed

    def _warn_mixed_kinds(self, segment: dict[str, Any]) -> None:
        """WARNING when the footprints of one segment are of different kinds (one customer pool addresses it)."""
        kinds = sorted({d["typename"] for d in segment.get("customer_deployments") or [] if d.get("typename")})
        if len(kinds) > 1:
            self.log_warning(
                message=(
                    f"Segment '{segment.get('name')}' names footprints of different kinds ({', '.join(kinds)}): "
                    "its prefix is drawn from the pool of the first kind, data center before colocation"
                )
            )

    @staticmethod
    def _virtual_networks_by_footprint(
        networks: list[dict[str, Any]], footprints: dict[str, dict[str, Any]]
    ) -> dict[str, set[str]]:
        """Footprint id -> ids of the virtual networks of its account in its region (cloud footprints only)."""
        by_footprint: dict[str, set[str]] = defaultdict(set)
        for footprint in footprints.values():
            if footprint["kind"] != CLOUD_KIND:
                continue
            account_id = (footprint.get("account") or {}).get("id")
            region_id = (footprint.get("parent") or {}).get("id")
            for network in networks:
                same_account = (network.get("account") or {}).get("id") == account_id
                if same_account and (network.get("region") or {}).get("id") == region_id:
                    by_footprint[footprint["id"]].add(network["id"])
        return by_footprint

    def _validate_footprint(
        self, footprint: dict[str, Any], segment_count: int, ports: dict[str, int], networks: set[str]
    ) -> None:
        name = footprint.get("name", "<unnamed-footprint>")
        kind = footprint["kind"]
        design = footprint["design"]
        design_name = design.get("name", "<unnamed-design>")

        if segment_count > design["max_segments"]:
            self.log_error(
                message=(
                    f"Customer footprint '{name}' has {segment_count} segments, over the {design['max_segments']} "
                    f"its design '{design_name}' allows (max_segments)"
                ),
                object_id=footprint.get("id"),
                object_type=kind,
            )
        if kind == CLOUD_KIND and len(networks) > design["max_virtual_networks"]:
            self.log_error(
                message=(
                    f"Customer footprint '{name}' has {len(networks)} virtual networks in its account, over the "
                    f"{design['max_virtual_networks']} its design '{design_name}' allows (max_virtual_networks)"
                ),
                object_id=footprint.get("id"),
                object_type=kind,
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
                    object_type=kind,
                )

    def _validate_block(self, kind: str, template: dict[str, Any]) -> None:
        """ERRORs when a tier's blocks have no room for ``max_segments`` segments of the tier's size, per family."""
        name = template.get("name")
        families = (
            (IPV4, template["address_prefix_length"], template["segment_prefix_length"], "segment_prefix_length"),
            (
                IPV6,
                template["ipv6_prefix_length"],
                template["ipv6_segment_prefix_length"],
                "ipv6_segment_prefix_length",
            ),
        )
        for family, block, segment, field in families:
            label = "IPv6 " if family == IPV6 else ""
            if segment < block:
                self.log_error(
                    message=(
                        f"Design '{name}' ({kind}): its {field} /{segment} is larger than its /{block} {label}address "
                        "block"
                    ),
                    object_id=template.get("id"),
                    object_type=kind,
                )
                continue
            room = 2 ** (segment - block)
            if room < template["max_segments"]:
                self.log_error(
                    message=(
                        f"Design '{name}' ({kind}): its /{block} {label}address block holds {room} /{segment} "
                        f"segments, fewer than the {template['max_segments']} its max_segments allows"
                    ),
                    object_id=template.get("id"),
                    object_type=kind,
                )

    def _validate_cloud_tier(self, template: dict[str, Any]) -> None:
        """ERROR when a cloud tier's block has no room for a routed range per virtual network it allows."""
        block, wanted = template["address_prefix_length"], template["max_virtual_networks"]
        room = 2 ** (CLOUD_RANGE_PREFIX_LENGTH - block) if block <= CLOUD_RANGE_PREFIX_LENGTH else 0
        if room < wanted:
            self.log_error(
                message=(
                    f"Design '{template.get('name')}' ({CLOUD_TEMPLATE_KIND}): its /{block} block holds {room} "
                    f"/{CLOUD_RANGE_PREFIX_LENGTH} routed ranges, fewer than the {wanted} virtual networks its "
                    "max_virtual_networks allows"
                ),
                object_id=template.get("id"),
                object_type=CLOUD_TEMPLATE_KIND,
            )

    def _warn_attachments_without_range(
        self,
        attachments: list[dict[str, Any]],
        footprints: dict[str, dict[str, Any]],
        networks: dict[str, set[str]],
    ) -> None:
        """WARNING for an attachment to a footprint's virtual network that has no ``allowed_prefixes`` at all."""
        owner_of = {network_id: fp_id for fp_id, ids in networks.items() for network_id in ids}
        for attachment in attachments:
            footprint_id = owner_of.get((attachment.get("virtual_network") or {}).get("id", ""))
            if footprint_id is None or attachment.get("allowed_prefixes"):
                continue
            self.log_warning(
                message=(
                    f"Attachment '{attachment.get('name')}' of footprint "
                    f"'{footprints[footprint_id].get('name')}' has no allowed_prefixes: no routed range was "
                    "authored or allocated (add_cloud_attachment_range), so its network's CIDR blocks are announced"
                )
            )

    def _validate_ip_version(self, segment: dict[str, Any]) -> None:
        """A gateway prefix of the segment's family (ERROR), and no host_count on an IPv6 segment (WARNING)."""
        version = segment.get("ip_version") or IPV4
        prefix = ((segment.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
        if prefix and (":" in prefix) != (version == IPV6):
            self.log_error(
                message=(
                    f"Segment '{segment.get('name')}': its gateway prefix {prefix} is not {version.replace('ip', 'IP')}, "
                    "the ip_version the segment states"
                ),
                object_id=segment.get("id"),
                object_type="ManagedVxlanSegment",
            )
        if version == IPV6 and segment.get("host_count") is not None:
            self.log_warning(
                message=(
                    f"Segment '{segment.get('name')}': host_count {segment['host_count']} sizes nothing on an IPv6 "
                    "segment (its prefix is the ipv6_segment_prefix_length of its design)"
                )
            )

    def _validate_cloud_block(self, segment: dict[str, Any]) -> None:
        """ERRORs when a cloud segment is not IPv4, or its cidr_block is smaller than its host_count needs."""
        if (segment.get("ip_version") or IPV4) != IPV4:
            self.log_error(
                message=f"Cloud segment '{segment.get('name')}': it must be IPv4 (it carries a provider cidr_block)",
                object_id=segment.get("id"),
                object_type="CloudNetworkSegment",
            )
        host_count, cidr_block = segment.get("host_count"), segment.get("cidr_block")
        if host_count is None or not cidr_block:
            return
        wanted = segment_prefix_length(host_count)
        actual = ipaddress.ip_network(cidr_block, strict=False).prefixlen
        if actual > wanted:
            self.log_error(
                message=(
                    f"Cloud segment '{segment.get('name')}': its cidr_block {cidr_block} is smaller than the "
                    f"/{wanted} its host_count {host_count} needs"
                ),
                object_id=segment.get("id"),
                object_type="CloudNetworkSegment",
            )


def _host_facing_ports(interface: dict[str, Any]) -> list[tuple[str, int, set[str]]]:
    """(port id, speed in Gbps, ids of the port's device deployment and its parent) of one switched interface.

    A physical port is itself; a port-channel is its member ports. Anything that is not a host-facing port,
    or whose speed its interface_type does not state or is below 1G (no ``ports_<speed>g`` meters it), yields nothing.
    """
    if interface.get("role") in SERVICE_CONTEXT_ROLES.values():
        return []
    scope = deployment_scope_ids((interface.get("device") or {}).get("deployment"))
    physical = [interface] if interface["typename"] == "DcimPhysicalInterface" else interface.get("member_interfaces")
    return [
        (port["id"], speed_mbps // 1000, scope)
        for port in physical or []
        if (speed_mbps := InterfaceSpeedMatcher.extract_speed(port.get("interface_type"))) and speed_mbps >= 1000
    ]
