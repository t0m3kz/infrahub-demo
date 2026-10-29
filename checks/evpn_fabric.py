"""Validate the fabric-wide EVPN invariants that nothing else can catch.

The failures modelled here are invisible in rendered config: every device's
configuration is syntactically valid and accepted, sessions come up, routes are
advertised — and traffic still does not forward. They cannot be caught by a
per-device check either, because each device on its own looks correct. They are
properties of the fabric as a whole, which is why they live in one check that
reads every fabric at once.

1. Route-target agreement. A route-target is <admin>:<vni>, and a VTEP imports a
   route only when the RT matches exactly. transforms/helpers/vxlan.py derives the
   admin field from TopologySegmentHosting.evpn_rt_as, falling back to the
   device's own local ASN. Under ebgp-ibgp and ospf-ibgp that fallback is safe —
   the overlay ASN is fabric-wide. Under ebgp-ebgp every VTEP runs its own ASN, so
   the fallback gives every VTEP a DIFFERENT route-target for the same VNI and no
   VTEP ever imports another's routes.

2. VNI uniqueness. A VNI on the wire is one 24-bit field with no L2/L3
   discriminator, so the L2 and L3 VNI spaces are the same space. Two segments
   sharing a VNI silently bridge two tenants together; an L2 VNI colliding with an
   L3 VNI mixes bridged and routed traffic in one VNI.

Also checks VNI encodability, which is the same class of problem: every ASN in
this project is 4-byte, and a 4-byte-admin route-target leaves only 16 bits for
the assigned field, so a VNI above 65535 cannot be expressed in the RT at all.

3. Site identity. ``evpn_rt_as`` is the Multi-Site site id and keeps the MANAGEMENT route-target DC-local, so
   two data centers or colocation metros sharing one ASN merge their sites. A stretched segment in the MANAGEMENT
   namespace would break the same DC-local rule.

4. Router-id uniqueness. The RD is ``<router_id>:<l3_vni>``: every VTEP with an instance realized as an L3 VNI
   needs an overlay router_id and no two of them may share it, or their routes overwrite each other.
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
from utils.vrf_demand import SVI_ROLE, internet_exits, leg_namespaces, realized_l3

# A type-2 route-target (4-byte admin ASN + 2-byte assigned number) and a type-1
# route-distinguisher (4-byte IP + 2-byte assigned number) both leave 16 bits for
# the VNI. Every ASN this project allocates is from the 4-byte private range, so
# there is no 2-byte-admin variant available to widen it.

# Strategies whose OVERLAY ASN is per-device rather than fabric-wide. Only for
# these does a missing evpn_rt_as actually break forwarding.
_PER_DEVICE_OVERLAY_ASN_STRATEGIES = frozenset({"ebgp-ebgp"})

_NO_OVERLAY_PROCESS_ROLES = frozenset({"access-leaf"})
"""VTEP roles that may legitimately have no BGP process. The overlay templates render an RD only when the transform
found an overlay process (transforms/helpers/vxlan.py ``build_evpn_inputs``: ``evpn_enabled = bool(process)``, read by
templates/configs/common/sonic_vxlan.j2). generators/helpers/routing.py plans the processes of a DC fabric only, so an
access-leaf outside one (the hand-placed dell_sonic cage switches CS-NY1-SW1, CS-VA1-SW1, MCR-AMS1-SW1) is a routed-SVI
switch with no BGP. Every other VTEP role gets its process from a generator: a missing one is a lost process."""

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
        namespace = ((segment.get("gateway") or {}).get("ip_prefix") or {}).get("ip_namespace") or {}
        if scope != _LOCAL_SCOPE and namespace.get("namespace_type") == MANAGEMENT_TYPE:
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
    """Whether one of the device's VRF instances is realized as an L3 VNI (utils/vrf_demand.py ``realized_l3``).

    The same inputs as the overlay transform: it routed in an instance's namespace when it has an anycast SVI there
    (a segment SVI or a transit leg), and it is an EVPN Multi-Site border gateway (``multisite-vip`` interface and a
    site ``evpn_rt_as``, transforms/helpers/vxlan.py ``_is_bgw``) for the internet-type exits.
    """
    interfaces = device.get("interfaces") or []
    namespaces = device_vrfs(device)
    routed = {
        name
        for iface in interfaces
        if iface.get("role") == SVI_ROLE and (name := ((iface.get("vrf") or {}).get("namespace") or {}).get("name"))
    } | leg_namespaces(interfaces)
    border_gateway = bool(_site_rt_asn(device)) and any(iface.get("role") == _MULTISITE_ROLE for iface in interfaces)
    exits = internet_exits(namespaces, border_gateway=border_gateway)
    return any(realized_l3(namespace, routed, exits) for namespace in namespaces)


def vtep_router_id_errors(devices: list[dict[str, Any]]) -> list[str]:
    """Every VTEP that renders an RD for an L3 VNI instance has an overlay router_id, unique among such VTEPs.

    A VRF-lite instance (a session or address only, no anycast SVI and no default exit) has no RD, so a VRF-lite edge
    needs none; neither does a device with no overlay BGP process (a routed-SVI access leaf without BGP renders no
    RD), so an ``access-leaf`` is skipped. Any other VTEP role without a process is an error.
    """
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
            namespaces=cleaned.get("IpamNamespace") or [],
        )

    # ------------------------------------------------------------------
    # 1. Route-target agreement
    # ------------------------------------------------------------------

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
                # iBGP overlay: the shared overlay ASN is itself the fabric-wide
                # constant, so the fallback lands on the same value everywhere.
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
                # No routing_strategy attribute exists on TopologyColocationMetro
                # yet, so the strategy cannot be inspected the way it can for a DC.
                # Warn rather than error: a metro with no VXLAN segments at all is
                # a perfectly valid configuration today.
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

    # ------------------------------------------------------------------
    # 2. VNI uniqueness and encodability
    # ------------------------------------------------------------------

    def _validate_vni_uniqueness(
        self,
        segments: list[dict[str, Any]],
        namespaces: list[dict[str, Any]],
    ) -> None:
        # deployment name -> vni -> segment names holding it
        l2_by_deployment: dict[str, dict[int, list[str]]] = {}
        for segment in segments:
            seg_name = segment.get("name", "<unnamed-vxlan-segment>")
            for activation in segment.get("segment_deployments") or []:
                vni = activation.get("vni")
                if not isinstance(vni, int):
                    # Unallocated VNI: the generator allocates from the pool on the
                    # next run. Not this check's concern.
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

        # L3 VNIs are fixed per namespace (data/bootstrap/22_namespaces.yml), so they
        # are global, not scoped to a deployment.
        l3_by_vni: dict[int, list[str]] = {}
        for namespace in namespaces:
            ns_name = namespace.get("name", "<unnamed-namespace>")
            l3_vni = namespace.get("l3_vni")
            if not isinstance(l3_vni, int):
                # The default/underlay namespace has no l3_vni, by design.
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

        # Exchange transit VNIs (utils/exchange_transit.py) are derived, not
        # pooled, so a pool-allocated VNI landing in their band would collide.
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

        # The crossover case: same 24-bit space, so an L2/L3 collision is just as
        # real as an L2/L2 one. L3 VNIs are fixed per namespace while L2 VNIs come
        # from the per-site pools, so a pool range can grow over a namespace value.
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
