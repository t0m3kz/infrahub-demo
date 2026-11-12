"""Check the routing invariants that hold between topologies (none is visible in one rendered config).

Rules 1-10: namespaces and l3_vni, INTERNET transit-only, cloud and office prefix registration, ownership. See AGENTS.md
"Border policy".
"""

from __future__ import annotations

import ipaddress
from typing import Any, NamedTuple

from transforms.helpers.bgp_cloud import CloudLeg, cloud_legs
from transforms.helpers.bgp_external import (
    get_onprem_prefixes,
    office_registered,
    registered_by_attachment,
    registered_by_namespace,
    session_circuit_owner,
)
from utils.circuit_underlay import SESSION_CIRCUIT
from utils.data_cleaning import clean_data
from utils.exchange_transit import INTERNET_TYPE, PROD_TYPE, namespace_type_for_environment
from utils.fabric_roles import FIREWALL_ROLES
from utils.ownership import CircuitOwnerError, customer_owner, ownership_conflicts
from utils.routing_domain import interface_security_zone, resolve_security_zone
from utils.segment_addressing import segment_namespace

from .base import WarningCheck

# Roles that may hold an address in the INTERNET VRF: transit edge, ISP side, firewall legs.
_INTERNET_DEVICE_ROLES = frozenset({"edge", "core", "border-leaf", "provider"}) | FIREWALL_ROLES

_INTERNET_INTERFACE_KEYS = ("InternetPhysicalInterfaces", "InternetVirtualInterfaces", "InternetLagInterfaces")


def _type_for_environment(environment: str | None) -> str | None:
    return namespace_type_for_environment(environment) if environment else None


class _Aggregate(NamedTuple):
    """A routed prefix a VPC, office or tunnel owns, with its expected namespace type."""

    owner: str  # "VPC 'name'" / "office 'name'"
    noun: str  # what the prefix is to its owner: "block" / "LAN prefix"
    origin: str  # where the expected type comes from, for messages
    expected: str | None
    prefix: dict[str, Any]
    zone: dict[str, Any]


class CheckRoutingInvariants(WarningCheck):
    """Namespace-type invariants across shared L3 VNIs, the INTERNET VRF and cloud routing domains."""

    query = "routing_invariants"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        namespaces = cleaned.get("SecurityZone") or []
        self._validate_l3_vni_sharing(namespaces)
        self._validate_internet_vrf(
            segments=cleaned.get("ManagedNetworkSegment") or [],
            interfaces=[iface for key in _INTERNET_INTERFACE_KEYS for iface in cleaned.get(key) or []],
        )
        self._validate_hub_route_tables(cleaned.get("CloudGatewayAttachment") or [])
        networks = cleaned.get("CloudVirtualNetwork") or []
        offices = cleaned.get("TopologyCustomerOffice") or []
        tunnels = cleaned.get(SESSION_CIRCUIT) or []
        legs = cloud_legs(cleaned.get("CloudPeeringInterface"), cleaned.get("CloudGatewayAttachment"))
        aggregates = _vpc_aggregates(networks, legs) + _office_aggregates(offices) + _tunnel_aggregates(tunnels)
        self._validate_aggregate_namespaces(aggregates)
        self._validate_subnets_in_vpc(networks, cleaned.get("CloudNetworkSegment") or [])
        self._validate_segment_overlaps(cleaned.get("ManagedNetworkSegment") or [])
        self._validate_aggregate_overlaps(aggregates, cleaned.get("ManagedNetworkSegment") or [])
        self._validate_registered_prefix_owners(networks, offices, tunnels, legs)
        self._validate_ownership_conflicts(cleaned.get("ManagedNetworkSegment") or [], offices, tunnels, legs)
        self._validate_offices_register(offices)

    # 1. L3 VNI sharing

    def _validate_l3_vni_sharing(self, namespaces: list[dict[str, Any]]) -> None:
        by_vni: dict[int, list[dict[str, Any]]] = {}
        for namespace in namespaces:
            if isinstance(namespace.get("l3_vni"), int):
                by_vni.setdefault(namespace["l3_vni"], []).append(namespace)
        for vni, sharing in sorted(by_vni.items()):
            types = {ns.get("vrf_type") for ns in sharing}
            if len(types) > 1:
                self.log_error(
                    message=(
                        f"L3 VNI {vni} is used by namespaces of different types "
                        f"({sorted(ns.get('name', '') + '=' + str(ns.get('vrf_type')) for ns in sharing)}): "
                        "they share a route-target and so import each other's routes"
                    )
                )

    # 2. INTERNET VRF is transit-only

    def _validate_internet_vrf(self, segments: list[dict[str, Any]], interfaces: list[dict[str, Any]]) -> None:
        for segment in segments:
            namespace = segment_namespace(segment)
            if namespace.get("vrf_type") == INTERNET_TYPE:
                self.log_error(
                    message=(
                        f"{segment.get('typename')} '{segment.get('name')}' has its gateway in the INTERNET "
                        f"namespace '{namespace.get('name')}': segments are host-facing, the INTERNET VRF is transit-only"
                    )
                )
        for iface in interfaces:
            namespace = interface_security_zone(iface)
            if namespace.get("vrf_type") != INTERNET_TYPE:
                continue
            device = iface.get("device") or {}
            device_role = device.get("role")
            if device_role not in _INTERNET_DEVICE_ROLES:
                self.log_error(
                    message=(
                        f"Interface '{device.get('name')}:{iface.get('name')}' (device role {device_role}, "
                        f"interface role {iface.get('role')}) holds an address in the INTERNET namespace "
                        f"'{namespace.get('name')}': only transit, provider and firewall-leg interfaces may"
                    )
                )

    # 4. Hub route-table environment purity

    def _validate_hub_route_tables(self, attachments: list[dict[str, Any]]) -> None:
        members: dict[str, dict[str, Any]] = {}
        for att in attachments:
            att_name = att.get("name", "<unnamed-attachment>")
            att_type = self._attachment_type(att)
            tables = [att.get("associated_route_table"), *(att.get("propagates_to_route_tables") or [])]
            for table in tables:
                if not isinstance(table, dict) or not table.get("id"):
                    continue
                entry = members.setdefault(table["id"], {"name": table.get("name"), "types": {}})
                if att_type is None:
                    self.log_warning(
                        message=(
                            f"attachment '{att_name}' in route table '{table.get('name')}' resolves to no "
                            "namespace type (account has no environment): environment purity cannot be verified"
                        )
                    )
                    continue
                entry["types"].setdefault(att_type, set()).add(att_name)
        for entry in members.values():
            if len(entry["types"]) > 1:
                self.log_error(
                    message=(
                        f"Hub route table '{entry['name']}' mixes environments: "
                        f"{ {t: sorted(n) for t, n in sorted(entry['types'].items())} }"
                    )
                )

    @staticmethod
    def _attachment_type(att: dict[str, Any]) -> str | None:
        """Namespace type of a network attachment (its account's environment) or a gateway one (its owner account's)."""
        owner_environment = (att.get("owner_account") or {}).get("environment")
        if att.get("virtual_network"):
            return _type_for_environment(_account_environment(att.get("virtual_network")) or owner_environment)
        return _type_for_environment(owner_environment)

    # 5-6. VPC and office aggregates

    def _validate_aggregate_namespaces(self, aggregates: list[_Aggregate]) -> None:
        for agg in aggregates:
            namespace = agg.zone
            actual = namespace.get("vrf_type")
            if agg.expected and actual != agg.expected:
                self.log_error(
                    message=(
                        f"{agg.owner} {agg.noun} {agg.prefix.get('prefix')} is registered in namespace "
                        f"'{namespace.get('name')}' ({actual}), but {agg.origin} makes it a "
                        f"{agg.expected} network"
                    )
                )

    def _validate_subnets_in_vpc(self, networks: list[dict[str, Any]], subnets: list[dict[str, Any]]) -> None:
        blocks_by_vpc = {
            network["id"]: [net for block in network.get("cidr_blocks") or [] if (net := _network(block.get("prefix")))]
            for network in networks
            if network.get("id")
        }
        for subnet in subnets:
            cidr = subnet.get("cidr_block")
            vpc = subnet.get("virtual_network") or {}
            subnet_net = _network(cidr)
            if subnet_net is None:
                self.log_error(message=f"Cloud subnet '{subnet.get('name')}' has no valid cidr_block ({cidr!r})")
                continue
            blocks = blocks_by_vpc.get(vpc.get("id"), [])
            if not any(_overlap(subnet_net, block) and subnet_net.prefixlen >= block.prefixlen for block in blocks):
                self.log_error(
                    message=(
                        f"Cloud subnet '{subnet.get('name')}' {cidr} lies outside every cidr_blocks entry of its "
                        f"VPC '{vpc.get('name')}' ({[str(block) for block in blocks] or 'none registered'})"
                    )
                )

    def _validate_segment_overlaps(self, segments: list[dict[str, Any]]) -> None:
        """Routed prefixes of two on-prem segments never overlap in one namespace (customer networks are not NATed)."""
        routed = _onprem_prefixes(segments)
        for index, (name, owner, prefix, net, zone) in enumerate(routed):
            namespace = _namespace_key(zone)
            for other_name, other_owner, other_prefix, other_net, other_zone in routed[index + 1 :]:
                if _namespace_key(other_zone) == namespace and _overlap(net, other_net):
                    self.log_error(
                        message=(
                            f"On-prem segment '{name}' ({_describe_owner(owner)}) {net} overlaps segment "
                            f"'{other_name}' ({_describe_owner(other_owner)}) {other_net} in namespace "
                            f"'{zone.get('name')}'"
                        )
                    )

    def _validate_aggregate_overlaps(self, aggregates: list[_Aggregate], segments: list[dict[str, Any]]) -> None:
        """Within one namespace an aggregate overlaps neither another aggregate nor an on-prem prefix."""
        routed = [(agg, net) for agg in aggregates if (net := _network(agg.prefix.get("prefix")))]

        onprem = [(name, prefix, net, zone) for name, _owner, prefix, net, zone in _onprem_prefixes(segments)]

        for index, (agg, net) in enumerate(routed):
            namespace = _namespace_key(agg.zone)
            namespace_name = agg.zone.get("name")
            for other, other_net in routed[index + 1 :]:
                if _namespace_key(other.zone) == namespace and _overlap(net, other_net):
                    self.log_error(
                        message=(
                            f"{agg.owner} {agg.noun} {net} overlaps {other.owner} {other.noun} {other_net} in "
                            f"namespace '{namespace_name}'"
                        )
                    )
            for segment_name, prefix, prefix_net, zone in onprem:
                if _namespace_key(zone) != namespace:
                    continue
                if agg.prefix.get("id") and agg.prefix.get("id") == prefix.get("id"):
                    self.log_error(
                        message=(
                            f"Prefix {net} is referenced by {agg.owner} and by on-prem segment '{segment_name}': "
                            f"the {agg.noun} adopted an on-prem prefix object"
                        )
                    )
                elif _overlap(net, prefix_net):
                    self.log_error(
                        message=(
                            f"{agg.owner} {agg.noun} {net} overlaps on-prem prefix {prefix_net} (segment "
                            f"'{segment_name}') in namespace '{namespace_name}'"
                        )
                    )

    # 7. Registered-prefix owners are customers

    def _validate_registered_prefix_owners(
        self,
        networks: list[dict[str, Any]],
        offices: list[dict[str, Any]],
        tunnels: list[dict[str, Any]],
        legs: list[CloudLeg],
    ) -> None:
        for leg in legs:
            if leg.registered and leg.owner is None:
                self.log_error(
                    message=(
                        f"cloud peering '{leg.peering}' registers prefixes behind {leg.point}, but its transport "
                        f"'{leg.transport}' has no customer owner (its account's owner, or its own for a SaaS): "
                        "only a customer-owned prefix is accepted from or announced to an external BGP peer, so "
                        "its prefixes are never accepted or announced at the border"
                    )
                )
        registrants = [
            (
                f"VPC '{network.get('name')}' registers blocks, but its account "
                f"'{(network.get('account') or {}).get('name')}'",
                (network.get("account") or {}).get("owner"),
            )
            for network in networks
            if network.get("cidr_blocks")
        ] + [
            (f"office '{office.get('name')}' registers LANs or hosts segments, but it", office.get("owner"))
            for office in offices
            if office.get("lan_prefixes") or office.get("network_segments")
        ]
        for tunnel in tunnels:
            if not tunnel.get("registered_prefixes"):
                continue
            subject = f"tunnel '{tunnel.get('name')}' registers prefixes, but it"
            try:
                key = session_circuit_owner(tunnel)
            except CircuitOwnerError as exc:
                self.log_error(message=f"{subject} has no single owner: {exc}")
                continue
            nodes = [location["owner"] for location in tunnel.get("locations") or [] if location.get("owner")]
            registrants.append(
                (subject, next((n for n in nodes if customer_owner(n) == key), None) or (nodes or [None])[0])
            )
        for subject, owner in registrants:
            if customer_owner(owner) is None:
                self.log_error(
                    message=(
                        f"{subject} has {_describe_owner(owner)}: only a customer-owned prefix is accepted from or "
                        "announced to an external BGP peer, so its prefixes are never accepted or announced at the border"
                    )
                )

    # 8. One owner and one attachment point per registered prefix

    def _validate_ownership_conflicts(
        self,
        segments: list[dict[str, Any]],
        offices: list[dict[str, Any]],
        tunnels: list[dict[str, Any]],
        legs: list[CloudLeg],
    ) -> None:
        # namespace -> attachment point -> owner -> prefixes, read as the edge reads them.
        learned: dict[str, dict[str, dict[str, set[str]]]] = {}
        for point, registered in registered_by_attachment(offices, tunnels, legs).items():
            for namespace, by_owner in registered_by_namespace(registered).items():
                behind = learned.setdefault(namespace, {}).setdefault(point, {})
                for owner, prefixes in by_owner.items():
                    behind.setdefault(owner, set()).update(prefixes)

        onprem = get_onprem_prefixes(segments)
        for namespace in sorted(set(onprem) | set(learned)):
            for conflict in ownership_conflicts(onprem.get(namespace, {}), learned.get(namespace, {})):
                self.log_error(message=f"Namespace '{namespace}': {conflict.message}")

    # 9. An office behind an SD-WAN gateway registers something

    def _validate_offices_register(self, offices: list[dict[str, Any]]) -> None:
        for office in offices:
            gateway = (office.get("sdwan_gateway") or {}).get("name")
            if gateway and not office_registered(office):
                self.log_error(
                    message=(
                        f"office '{office.get('name')}' sits behind SD-WAN gateway '{gateway}' but registers no "
                        "prefix (no lan_prefixes, no segment it hosts): the gateway session accepts nothing of it"
                    )
                )


def _onprem_prefixes(
    segments: list[dict[str, Any]],
) -> list[
    tuple[str, dict[str, Any] | None, dict[str, Any], ipaddress.IPv4Network | ipaddress.IPv6Network, dict[str, Any]]
]:
    """(name, owner, prefix, network) of the routed prefix behind each on-prem segment's gateway."""
    found = []
    for segment in segments:
        if segment.get("typename") == "CloudNetworkSegment":
            continue
        prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
        if prefix.get("member_type") == "prefix":
            continue  # a container, not a routed subnet
        if net := _network(prefix.get("prefix")):
            found.append(
                (
                    str(segment.get("name")),
                    segment.get("owner"),
                    prefix,
                    net,
                    segment_namespace(segment),
                )
            )
    return found


def _vpc_aggregates(networks: list[dict[str, Any]], legs: list[CloudLeg]) -> list[_Aggregate]:
    """Each VPC cidr_blocks entry, expected in its account's environment namespace."""
    return [
        _Aggregate(
            owner=f"VPC '{network.get('name')}'",
            noun="block",
            origin="its account's environment",
            expected=_type_for_environment((network.get("account") or {}).get("environment")),
            prefix=block,
            zone=dict(registration.zone),
        )
        for network in networks
        for block in network.get("cidr_blocks") or []
        for leg in legs
        for registration in leg.registered
        if registration.prefix == block
    ]


def _office_aggregates(offices: list[dict[str, Any]]) -> list[_Aggregate]:
    """Each office lan_prefixes entry, expected in PROD."""
    return [
        _Aggregate(
            owner=f"office '{office.get('name')}'",
            noun="LAN prefix",
            origin="its being an office (always production)",
            expected=PROD_TYPE,
            prefix=dict(registration.prefix),
            zone=dict(registration.zone),
        )
        for office in offices
        if (office.get("sdwan_gateway") or {}).get("name")
        for registration in office_registered(office)
    ]


def _tunnel_aggregates(tunnels: list[dict[str, Any]]) -> list[_Aggregate]:
    """Each tunnel registered_prefixes entry, expected in PROD."""
    return [
        _Aggregate(
            owner=f"tunnel '{tunnel.get('name')}'",
            noun="registered prefix",
            origin="its being a tunnel (always production)",
            expected=PROD_TYPE,
            prefix=prefix,
            zone=resolve_security_zone([tunnel], required=True, label=str(tunnel.get("name") or "tunnel")),
        )
        for tunnel in tunnels
        for prefix in tunnel.get("registered_prefixes") or []
    ]


def _describe_owner(owner: dict[str, Any] | None) -> str:
    """'no owner' or the owner's org_id and kind."""
    if not owner:
        return "no owner"
    return f"owner {owner.get('org_id')} ({owner.get('display_label')}, {owner.get('typename')})"


def _network(prefix: str | None) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(prefix, strict=False) if prefix else None
    except ValueError:
        return None


def _namespace_key(namespace: dict[str, Any]) -> Any:
    return namespace.get("id") or namespace.get("name")


def _overlap(
    left: ipaddress.IPv4Network | ipaddress.IPv6Network, right: ipaddress.IPv4Network | ipaddress.IPv6Network
) -> bool:
    return left.version == right.version and left.overlaps(right)


def _account_environment(virtual_network: dict[str, Any] | None) -> str | None:
    return (((virtual_network or {}).get("account")) or {}).get("environment")
