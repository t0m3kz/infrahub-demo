"""Validate the routing invariants that hold between topologies.

PROD and NON-PROD never meet, and the INTERNET VRF carries both environments'
SNAT /32s, so it must stay transit-only. None of these is visible in a
rendered config; each is a property of data that spans topologies.

1. Route-target joins. A RouteLeakExchange (the only route-target import
   between namespaces) may join namespaces of one namespace_type only, and an
   explicit route_target must not name the L3 VNI of a namespace of another
   type. Two namespaces of different types sharing an l3_vni would also share
   a route-target, an implicit import.
2. The INTERNET VRF has no host-facing interface: no segment gateway and no
   address on a host, a customer port or any device other than a transit
   edge, a provider or a firewall.
3. A cloud Transit Gateway route table is environment-pure: every attachment
   associated with or propagating into it resolves to one namespace type. A
   VPC attachment resolves through its account's environment; a hybrid
   attachment through the namespaces its VIF circuits terminate in (falling
   back to its owner account's environment while no circuit is addressed).
4. Every circuit on a cloud virtual interface resolves to one namespace type,
   and to its private VIF's virtual network when it has one.

5. Cloud VPC aggregates. Each `CloudVirtualNetwork.cidr_blocks` prefix is
   registered in the namespace whose type matches its account's environment
   (the four shared environment VRFs, never a per-customer one); every cloud
   subnet `cidr_block` (Text) lies inside one of its VPC's blocks; and inside
   one namespace no VPC block overlaps another VPC's block or an on-prem
   prefix (a non-container prefix behind a segment gateway). A prefix object
   referenced by both a VPC and an on-prem segment is flagged on its own: an
   object-file upsert of an identical CIDR silently adopts the on-prem object.
6. Office LAN prefixes. Each `TopologyCustomerOffice.lan_prefixes` prefix is
   registered in the namespace whose type matches the footprint's environment,
   and is held to the same overlap rule as a VPC block: inside one namespace it
   overlaps no VPC block, no other office's LAN and no on-prem prefix. These
   are the routes an office may announce into its VRF over SD-WAN.

A missing evpn_rt_as on an eBGP-overlay fabric is checked by CheckEvpnFabric.
"""

from __future__ import annotations

import ipaddress
from typing import Any, NamedTuple

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import INTERNET_TYPE, namespace_type_for_environment

# Devices that may legitimately hold an address in the INTERNET VRF: the transit
# edge, the ISP side, and firewall legs.
_INTERNET_DEVICE_ROLES = frozenset(
    {"edge", "core", "border-leaf", "provider", "firewall", "firewall-active", "firewall-passive"}
)

_INTERNET_INTERFACE_KEYS = ("InternetPhysicalInterfaces", "InternetVirtualInterfaces", "InternetLagInterfaces")


def _type_for_environment(environment: str | None) -> str | None:
    return namespace_type_for_environment(environment) if environment else None


class _Aggregate(NamedTuple):
    """A routed prefix a VPC or an office owns, with the namespace type its owner's environment implies."""

    owner: str  # "VPC 'name'" / "office 'name'"
    noun: str  # what the prefix is to its owner: "block" / "LAN prefix"
    origin: str  # where the expected type comes from, for messages
    expected: str | None
    prefix: dict[str, Any]


class CheckRoutingInvariants(InfrahubCheck):
    """Namespace-type invariants across route-target joins, the INTERNET VRF and cloud routing domains."""

    query = "routing_invariants"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        namespaces = cleaned.get("IpamNamespace") or []
        self._validate_route_target_joins(namespaces, cleaned.get("TopologyRouteLeakExchange") or [])
        self._validate_internet_vrf(
            segments=cleaned.get("ManagedNetworkSegment") or [],
            interfaces=[iface for key in _INTERNET_INTERFACE_KEYS for iface in cleaned.get(key) or []],
        )
        vif_types = self._validate_vif_circuits(cleaned.get("CloudVirtualInterface") or [])
        self._validate_tgw_route_tables(cleaned.get("CloudTransitGatewayAttachment") or [], vif_types)
        networks = cleaned.get("CloudVirtualNetwork") or []
        aggregates = _vpc_aggregates(networks) + _office_aggregates(cleaned.get("TopologyCustomerOffice") or [])
        self._validate_aggregate_namespaces(aggregates)
        self._validate_subnets_in_vpc(networks, cleaned.get("CloudNetworkSegment") or [])
        self._validate_aggregate_overlaps(aggregates, cleaned.get("ManagedNetworkSegment") or [])

    # ------------------------------------------------------------------
    # 1. Route-target joins
    # ------------------------------------------------------------------

    def _validate_route_target_joins(self, namespaces: list[dict[str, Any]], leaks: list[dict[str, Any]]) -> None:
        by_vni: dict[int, list[dict[str, Any]]] = {}
        for namespace in namespaces:
            if isinstance(namespace.get("l3_vni"), int):
                by_vni.setdefault(namespace["l3_vni"], []).append(namespace)
        for vni, sharing in sorted(by_vni.items()):
            types = {ns.get("namespace_type") for ns in sharing}
            if len(types) > 1:
                self.log_error(
                    message=(
                        f"L3 VNI {vni} is used by namespaces of different types "
                        f"({sorted(ns.get('name', '') + '=' + str(ns.get('namespace_type')) for ns in sharing)}): "
                        "they share a route-target and so import each other's routes"
                    )
                )

        for leak in leaks:
            name = leak.get("name", "<unnamed-route-leak>")
            ns_a, ns_z = leak.get("namespace_a") or {}, leak.get("namespace_z") or {}
            type_a, type_z = ns_a.get("namespace_type"), ns_z.get("namespace_type")
            if not type_a or not type_z or type_a != type_z:
                self.log_error(
                    message=(
                        f"Route leak '{name}' joins '{ns_a.get('name')}' ({type_a}) and '{ns_z.get('name')}' "
                        f"({type_z}): route-target imports may only join namespaces of the same type"
                    )
                )
                continue
            vni = _route_target_vni(leak.get("route_target"))
            for target in by_vni.get(vni, []) if vni is not None else []:
                if target.get("namespace_type") != type_a:
                    self.log_error(
                        message=(
                            f"Route leak '{name}' route_target {leak['route_target']} names the L3 VNI of "
                            f"'{target.get('name')}' ({target.get('namespace_type')}), not a {type_a} namespace"
                        )
                    )

    # ------------------------------------------------------------------
    # 2. INTERNET VRF is transit-only
    # ------------------------------------------------------------------

    def _validate_internet_vrf(self, segments: list[dict[str, Any]], interfaces: list[dict[str, Any]]) -> None:
        for segment in segments:
            namespace = ((segment.get("gateway") or {}).get("ip_prefix") or {}).get("ip_namespace") or {}
            if namespace.get("namespace_type") == INTERNET_TYPE:
                self.log_error(
                    message=(
                        f"{segment.get('typename')} '{segment.get('name')}' has its gateway in the INTERNET "
                        f"namespace '{namespace.get('name')}': segments are host-facing, the INTERNET VRF is transit-only"
                    )
                )
        for iface in interfaces:
            namespace = (iface.get("ip_address") or {}).get("ip_namespace") or {}
            if namespace.get("namespace_type") != INTERNET_TYPE:
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

    # ------------------------------------------------------------------
    # 3. Cloud virtual interfaces
    # ------------------------------------------------------------------

    def _validate_vif_circuits(self, vifs: list[dict[str, Any]]) -> dict[str, str]:
        """Check each VIF's circuits; return VIF id -> its namespace type (circuit-resolved)."""
        resolved: dict[str, str] = {}
        for vif in vifs:
            name = vif.get("name", "<unnamed-vif>")
            expected = _type_for_environment(_account_environment(vif.get("virtual_network")))
            per_circuit: dict[str, set[str]] = {}
            for circuit in vif.get("virtual_circuits") or []:
                types = {
                    ((iface.get("ip_address") or {}).get("ip_namespace") or {}).get("namespace_type")
                    for iface in circuit.get("interface_capabilities") or []
                }
                per_circuit[circuit.get("name", "<unnamed-circuit>")] = {t for t in types if t}
            all_types = set().union(*per_circuit.values()) if per_circuit else set()
            if len(all_types) > 1:
                self.log_error(
                    message=(
                        f"VIF '{name}' carries circuits of different namespace types "
                        f"{ {c: sorted(t) for c, t in sorted(per_circuit.items())} }: never mix environments on one VIF"
                    )
                )
            elif expected and all_types and all_types != {expected}:
                self.log_error(
                    message=(
                        f"VIF '{name}' belongs to a {expected} virtual network but its circuits terminate in "
                        f"{sorted(all_types)} namespaces"
                    )
                )
            if len(all_types) == 1 and vif.get("id"):
                resolved[vif["id"]] = next(iter(all_types))
        return resolved

    # ------------------------------------------------------------------
    # 4. TGW route-table environment purity
    # ------------------------------------------------------------------

    def _validate_tgw_route_tables(self, attachments: list[dict[str, Any]], vif_types: dict[str, str]) -> None:
        members: dict[str, dict[str, Any]] = {}
        for att in attachments:
            att_name = att.get("name", "<unnamed-attachment>")
            att_type = self._attachment_type(att, vif_types)
            tables = [att.get("associated_route_table"), *(att.get("propagates_to_route_tables") or [])]
            for table in tables:
                if not isinstance(table, dict) or not table.get("id"):
                    continue
                entry = members.setdefault(table["id"], {"name": table.get("name"), "types": {}})
                if att_type is None:
                    self.log_info(
                        message=(
                            f"WARNING: attachment '{att_name}' in route table '{table.get('name')}' resolves to no "
                            "namespace type (account has no environment): environment purity cannot be verified"
                        )
                    )
                    continue
                entry["types"].setdefault(att_type, set()).add(att_name)
        for entry in members.values():
            if len(entry["types"]) > 1:
                self.log_error(
                    message=(
                        f"TGW route table '{entry['name']}' mixes environments: "
                        f"{ {t: sorted(n) for t, n in sorted(entry['types'].items())} }"
                    )
                )

    @staticmethod
    def _attachment_type(att: dict[str, Any], vif_types: dict[str, str]) -> str | None:
        """vpc -> its account's environment; hybrid -> its VIF's circuits, else the owner account's environment."""
        if att.get("attachment_type") == "vpc":
            environment = _account_environment(att.get("virtual_network")) or (att.get("owner_account") or {}).get(
                "environment"
            )
            return _type_for_environment(environment)
        endpoint = att.get("hybrid_endpoint") or {}
        if endpoint.get("id") in vif_types:
            return vif_types[endpoint["id"]]
        return _type_for_environment((att.get("owner_account") or {}).get("environment"))

    # ------------------------------------------------------------------
    # 5-6. VPC and office aggregates
    # ------------------------------------------------------------------

    def _validate_aggregate_namespaces(self, aggregates: list[_Aggregate]) -> None:
        for agg in aggregates:
            namespace = agg.prefix.get("ip_namespace") or {}
            actual = namespace.get("namespace_type")
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

    def _validate_aggregate_overlaps(self, aggregates: list[_Aggregate], segments: list[dict[str, Any]]) -> None:
        """Within one namespace an aggregate overlaps neither another aggregate nor an on-prem prefix."""
        routed = [(agg, net) for agg in aggregates if (net := _network(agg.prefix.get("prefix")))]

        onprem: list[tuple[str, dict[str, Any], ipaddress.IPv4Network | ipaddress.IPv6Network]] = []
        for segment in segments:
            if segment.get("typename") == "CloudNetworkSegment":
                continue
            prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
            if prefix.get("member_type") == "prefix":
                continue  # a container, not a routed subnet
            if net := _network(prefix.get("prefix")):
                onprem.append((str(segment.get("name")), prefix, net))

        for index, (agg, net) in enumerate(routed):
            namespace = _namespace_key(agg.prefix)
            namespace_name = (agg.prefix.get("ip_namespace") or {}).get("name")
            for other, other_net in routed[index + 1 :]:
                if _namespace_key(other.prefix) == namespace and _overlap(net, other_net):
                    self.log_error(
                        message=(
                            f"{agg.owner} {agg.noun} {net} overlaps {other.owner} {other.noun} {other_net} in "
                            f"namespace '{namespace_name}'"
                        )
                    )
            for segment_name, prefix, prefix_net in onprem:
                if _namespace_key(prefix) != namespace:
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


def _vpc_aggregates(networks: list[dict[str, Any]]) -> list[_Aggregate]:
    """Each VPC cidr_blocks entry, expected in its account's environment namespace."""
    return [
        _Aggregate(
            owner=f"VPC '{network.get('name')}'",
            noun="block",
            origin="its account's environment",
            expected=_type_for_environment((network.get("account") or {}).get("environment")),
            prefix=block,
        )
        for network in networks
        for block in network.get("cidr_blocks") or []
    ]


def _office_aggregates(offices: list[dict[str, Any]]) -> list[_Aggregate]:
    """Each office footprint's lan_prefixes entry, expected in its own environment's namespace."""
    return [
        _Aggregate(
            owner=f"office '{office.get('name')}'",
            noun="LAN prefix",
            origin="its environment",
            expected=_type_for_environment(office.get("environment")),
            prefix=prefix,
        )
        for office in offices
        for prefix in office.get("lan_prefixes") or []
    ]


def _network(prefix: str | None) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(prefix, strict=False) if prefix else None
    except ValueError:
        return None


def _namespace_key(prefix: dict[str, Any]) -> Any:
    namespace = prefix.get("ip_namespace") or {}
    return namespace.get("id") or namespace.get("name")


def _overlap(
    left: ipaddress.IPv4Network | ipaddress.IPv6Network, right: ipaddress.IPv4Network | ipaddress.IPv6Network
) -> bool:
    return left.version == right.version and left.overlaps(right)


def _account_environment(virtual_network: dict[str, Any] | None) -> str | None:
    return (((virtual_network or {}).get("account")) or {}).get("environment")


def _route_target_vni(route_target: str | None) -> int | None:
    """The assigned-number half of an `asn:vni` route-target, or None when it does not parse."""
    _, _, assigned = (route_target or "").rpartition(":")
    return int(assigned) if assigned.isdigit() else None
