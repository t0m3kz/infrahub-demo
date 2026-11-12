"""Validate fabric-wide EVPN invariants invisible in per-device configs.

1 route-target agreement; 2 VNI uniqueness and encodability; 3 site identity (``evpn_rt_as``); 4 router-id uniqueness.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from transforms.helpers.vrf import device_vrfs
from transforms.helpers.vxlan import _select_evpn_bgp_process
from utils.data_cleaning import clean_data
from utils.exchange_transit import MANAGEMENT_TYPE, TRANSIT_VNI_MAX, TRANSIT_VNI_MIN
from utils.fabric_roles import VTEP_ROLES
from utils.limits import MAX_ENCODABLE_VNI
from utils.routing_domain import RoutingDomainError, interface_vrf
from utils.segment_addressing import segment_namespace
from utils.service_contexts import border_contexts
from utils.vrf_demand import SVI_ROLE, internet_exits, leg_namespaces, realized_l3

# Route-targets and route-distinguishers leave 16 bits for the VNI (4-byte ASNs only), so a VNI above 65535 does not
# fit.

# Strategies with a per-device overlay ASN: only there a missing evpn_rt_as breaks forwarding.
_PER_DEVICE_OVERLAY_ASN_STRATEGIES = frozenset({"ebgp-ebgp"})

_NO_OVERLAY_PROCESS_ROLES = frozenset({"access-leaf"})
"""VTEP roles that may have no BGP process: an access-leaf outside a DC fabric is a routed-SVI switch; any other role
lost its process.
"""

_LOCAL_SCOPE = "local"
_MULTISITE_ROLE = "multisite-vip"


def duplicate_rt_as_errors(data_centers: list[dict[str, Any]], metros: list[dict[str, Any]]) -> list[str]:
    """Two sites (data centers or colocation metros) sharing one ``evpn_rt_as`` ASN."""
    sites: dict[int, list[str]] = defaultdict(list)
    for site in (*data_centers, *metros):
        asn = (site.get("evpn_rt_as") or {}).get("asn")
        if isinstance(asn, int):
            sites[asn].append(str(site.get("name")))
    return [
        f"evpn_rt_as ASN {asn} is shared by {len(names)} sites: {sorted(names)}. evpn_rt_as is the Multi-Site site id "
        "and keeps the MANAGEMENT route-target DC-local, so these sites would merge. Give each data center and "
        "colocation metro its own ASN."
        for asn, names in sorted(sites.items())
        if len(names) > 1
    ]


def stretched_management_errors(segments: list[dict[str, Any]]) -> list[str]:
    """A segment stretched beyond one deployment whose gateway is in the MANAGEMENT namespace."""
    errors = []
    for segment in segments:
        scope = segment.get("stretch_scope") or _LOCAL_SCOPE
        if not segment.get("security_zone") and not segment.get("gateway"):
            continue
        try:
            namespace = segment_namespace(segment)
        except RoutingDomainError as exc:
            errors.append(str(exc))
            continue
        if scope != _LOCAL_SCOPE and namespace.get("vrf_type") == MANAGEMENT_TYPE:
            errors.append(
                f"Segment '{segment.get('name')}' has stretch_scope '{scope}' but its gateway is in the MANAGEMENT "
                f"namespace '{namespace.get('name')}'. MANAGEMENT is DC-local and is never stretched: set "
                "stretch_scope to local."
            )
    return errors


def _site_rt_asn(device: dict[str, Any]) -> int | None:
    """The ``evpn_rt_as`` ASN of the device's site: its deployment's, else its parent's (pod, zone, virtual colo)."""
    deployment = device.get("deployment") or {}
    for holder in (deployment, deployment.get("parent") or {}):
        asn = (holder.get("evpn_rt_as") or {}).get("asn")
        if isinstance(asn, int):
            return asn
    return None


def _has_l3_instance(device: dict[str, Any]) -> bool:
    """Whether one of the device's VRF instances is realized as an L3 VNI (utils/vrf_demand.py ``realized_l3``)."""
    interfaces = device.get("interfaces") or []
    namespaces = device_vrfs(device)
    routed = {
        name
        for iface in interfaces
        if iface.get("role") == SVI_ROLE and (name := (interface_vrf(iface).get("security_zone") or {}).get("name"))
    } | leg_namespaces(border_contexts(device))
    border_gateway = bool(_site_rt_asn(device)) and any(iface.get("role") == _MULTISITE_ROLE for iface in interfaces)
    exits = internet_exits(namespaces, border_gateway=border_gateway)
    return any(realized_l3(namespace, routed, exits) for namespace in namespaces)


def vtep_router_id_errors(devices: list[dict[str, Any]]) -> list[str]:
    """Every VTEP that renders an RD for an L3 VNI instance has an overlay router_id, unique among such VTEPs."""
    errors = []
    holders: dict[str, list[str]] = defaultdict(list)
    for device in sorted(devices, key=lambda item: str(item.get("name"))):
        capabilities = device.get("capabilities") or []
        if device.get("role") not in VTEP_ROLES or not _has_l3_instance(device):
            continue
        process = _select_evpn_bgp_process(capabilities)
        if process is None:
            if device.get("role") not in _NO_OVERLAY_PROCESS_ROLES:
                errors.append(
                    f"VTEP '{device.get('name')}' (role '{device.get('role')}') has a VRF instance realized as an L3 "
                    "VNI but no BGP process at all, so it renders no RD. Re-run the routing generator of its "
                    "deployment to recreate its overlay ManagedBGP process."
                )
            continue
        address = (process.get("router_id") or {}).get("address")
        if not address:
            errors.append(
                f"VTEP '{device.get('name')}' has a VRF instance realized as an L3 VNI but no EVPN BGP process "
                "router_id. The RD is <router_id>:<l3_vni>; give the overlay ManagedBGP process a router_id."
            )
            continue
        holders[address.split("/")[0]].append(str(device.get("name")))
    errors.extend(
        f"EVPN router_id {router_id} is shared by VTEPs {names}. The RD is <router_id>:<l3_vni>, so their VRF routes "
        "would collide. Give each overlay BGP process its own router_id."
        for router_id, names in sorted(holders.items())
        if len(names) > 1
    )
    return errors


class CheckEvpnFabric(InfrahubCheck):
    """Fabric-wide EVPN invariants: route-target agreement, site identity, router ids and VNI uniqueness."""

    query = "evpn_fabric"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        self._validate_route_target_asn(
            data_centers=cleaned.get("TopologyDataCenter") or [],
            metros=cleaned.get("TopologyColocationMetro") or [],
        )
        segments = cleaned.get("ManagedVxlanSegment") or []
        for message in (
            duplicate_rt_as_errors(
                cleaned.get("TopologyDataCenter") or [], cleaned.get("TopologyColocationMetro") or []
            )
            + stretched_management_errors(segments)
            + vtep_router_id_errors(cleaned.get("DcimDevice") or [])
        ):
            self.log_error(message=message)
        self._validate_vni_uniqueness(
            segments=segments,
            namespaces=cleaned.get("SecurityZone") or [],
        )

    # 1. Route-target agreement

    def _validate_route_target_asn(
        self,
        data_centers: list[dict[str, Any]],
        metros: list[dict[str, Any]],
    ) -> None:
        for dc in data_centers:
            name = dc.get("name", "<unnamed-data-center>")
            strategy = dc.get("routing_strategy") or "ebgp-ebgp"
            if self._rt_asn(dc) is not None:
                continue
            if strategy not in _PER_DEVICE_OVERLAY_ASN_STRATEGIES:
                # iBGP overlay: the shared overlay ASN is the same everywhere.
                continue
            self.log_error(
                message=(
                    f"Data center '{name}' uses routing_strategy={strategy}, where every VTEP has "
                    "its own ASN, but has no evpn_rt_as set. Each VTEP will derive a different "
                    "EVPN route-target for the same VNI and none of them will import the others' "
                    "routes — the config renders and the sessions come up, but traffic will not "
                    "forward between leaves. Set evpn_rt_as on the data center."
                ),
                object_id=dc.get("id"),
                object_type="TopologyDataCenter",
            )

        for metro in metros:
            name = metro.get("name", "<unnamed-colocation-metro>")
            if self._rt_asn(metro) is None:
                # No routing_strategy on a metro: warn, not error (a metro with no VXLAN segments is valid).
                self.log_info(
                    message=(
                        f"Colocation metro '{name}' has no evpn_rt_as set. That is fine while it "
                        "runs no EVPN overlay, but any VTEP there will fall back to its own ASN "
                        "for the route-target admin field. Set evpn_rt_as before enabling EVPN."
                    ),
                    object_id=metro.get("id"),
                    object_type="TopologyColocationMetro",
                )

    @staticmethod
    def _rt_asn(fabric: dict[str, Any]) -> int | None:
        asn = (fabric.get("evpn_rt_as") or {}).get("asn")
        return asn if isinstance(asn, int) else None

    # 2. VNI uniqueness and encodability

    def _validate_vni_uniqueness(
        self,
        segments: list[dict[str, Any]],
        namespaces: list[dict[str, Any]],
    ) -> None:
        # deployment -> vni -> segment names
        l2_by_deployment: dict[str, dict[int, list[str]]] = {}
        for segment in segments:
            seg_name = segment.get("name", "<unnamed-vxlan-segment>")
            for activation in segment.get("segment_deployments") or []:
                vni = activation.get("vni")
                if not isinstance(vni, int):
                    # Unallocated VNI: the next generator run allocates it.
                    continue
                deployment = activation.get("deployment")
                dep_name = deployment.get("name", "<unknown>") if isinstance(deployment, dict) else "<unknown>"
                l2_by_deployment.setdefault(dep_name, {}).setdefault(vni, []).append(seg_name)
                self._check_encodable(vni, f"L2 VNI for segment '{seg_name}' in deployment '{dep_name}'")

        for dep_name, by_vni in sorted(l2_by_deployment.items()):
            for vni, seg_names in sorted(by_vni.items()):
                if len(seg_names) > 1:
                    self.log_error(
                        message=(
                            f"Deployment '{dep_name}' allocates L2 VNI {vni} to "
                            f"{len(seg_names)} segments: {sorted(seg_names)}. A VNI is the only "
                            "thing identifying a bridge domain on the wire, so these segments "
                            "would be bridged into one another — tenant isolation is lost with no "
                            "visible error. Reallocate so each segment has its own VNI."
                        )
                    )

        # L3 VNIs are fixed per namespace, so global.
        l3_by_vni: dict[int, list[str]] = {}
        for namespace in namespaces:
            ns_name = namespace.get("name", "<unnamed-namespace>")
            l3_vni = namespace.get("l3_vni")
            if not isinstance(l3_vni, int):
                # The default/underlay namespace has no l3_vni.
                continue
            l3_by_vni.setdefault(l3_vni, []).append(ns_name)
            self._check_encodable(l3_vni, f"L3 VNI for namespace '{ns_name}'")

        for vni, ns_names in sorted(l3_by_vni.items()):
            if len(ns_names) > 1:
                self.log_error(
                    message=(
                        f"L3 VNI {vni} is shared by {len(ns_names)} namespaces: {sorted(ns_names)}. "
                        "The L3 VNI is the tenant's transit VNI, so these VRFs would route into "
                        "each other. l3_vni is fixed per namespace in data/bootstrap/22_namespaces.yml; "
                        "correct the conflicting namespace value."
                    )
                )

        # Derived exchange transit VNIs are not pooled, so a pool VNI in their band collides.
        for dep_name, by_vni in sorted(l2_by_deployment.items()):
            for vni in sorted(v for v in by_vni if TRANSIT_VNI_MIN <= v <= TRANSIT_VNI_MAX):
                self.log_error(
                    message=(
                        f"L2 VNI {vni} (segments {sorted(by_vni[vni])}, deployment '{dep_name}') sits "
                        f"in the exchange transit VNI band {TRANSIT_VNI_MIN}-{TRANSIT_VNI_MAX}, which "
                        "border leaves derive for firewall transit VLANs. Keep L2 VNI pool ranges outside it."
                    )
                )
        for vni, ns_names in sorted(l3_by_vni.items()):
            if TRANSIT_VNI_MIN <= vni <= TRANSIT_VNI_MAX:
                self.log_error(
                    message=(
                        f"L3 VNI {vni} for namespace(s) {sorted(ns_names)} sits in the exchange transit "
                        f"VNI band {TRANSIT_VNI_MIN}-{TRANSIT_VNI_MAX}. l3_vni is fixed per namespace in "
                        "data/bootstrap/22_namespaces.yml; move it out of the band."
                    )
                )

        # Same 24-bit space: an L3 VNI fixed per namespace can fall in a per-site L2 pool range.
        for dep_name, by_vni in sorted(l2_by_deployment.items()):
            for vni in sorted(set(by_vni) & set(l3_by_vni)):
                self.log_error(
                    message=(
                        f"VNI {vni} is used as an L2 VNI in deployment '{dep_name}' (segments "
                        f"{sorted(by_vni[vni])}) and as an L3 VNI for namespace(s) "
                        f"{sorted(l3_by_vni[vni])}. A VNI on the wire carries no L2/L3 "
                        "discriminator, so bridged and routed traffic would share one VNI. Make "
                        "the L2 VNI pools clear of the fixed namespace l3_vni (correct the namespace value in "
                        "data/bootstrap/22_namespaces.yml or move the conflicting L2 pool)."
                    )
                )

    def _check_encodable(self, vni: int, what: str) -> None:
        if vni > MAX_ENCODABLE_VNI:
            self.log_error(
                message=(
                    f"{what} is {vni}, above {MAX_ENCODABLE_VNI}. Every ASN in this project is "
                    "4-byte, and both the type-1 route-distinguisher and the type-2 route-target "
                    "leave only 16 bits for the assigned field, so this VNI cannot be encoded in "
                    f"them. Keep VNI pool ranges at or below {MAX_ENCODABLE_VNI}."
                )
            )
