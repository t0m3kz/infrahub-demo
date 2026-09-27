"""Validate the two fabric-wide EVPN invariants that nothing else can catch.

Both failures modelled here are invisible in rendered config: every device's
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
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

# A type-2 route-target (4-byte admin ASN + 2-byte assigned number) and a type-1
# route-distinguisher (4-byte IP + 2-byte assigned number) both leave 16 bits for
# the VNI. Every ASN this project allocates is from the 4-byte private range, so
# there is no 2-byte-admin variant available to widen it. Mirrors
# transforms/helpers/vxlan.py's _MAX_ENCODABLE_VNI — kept as its own constant
# rather than imported so a check never depends on a transform.
_MAX_ENCODABLE_VNI = 65535

# Strategies whose OVERLAY ASN is per-device rather than fabric-wide. Only for
# these does a missing evpn_rt_as actually break forwarding.
_PER_DEVICE_OVERLAY_ASN_STRATEGIES = frozenset({"ebgp-ebgp"})


class CheckEvpnFabric(InfrahubCheck):
    """Fabric-wide EVPN invariants: route-target agreement and VNI uniqueness."""

    query = "evpn_fabric"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        self._validate_route_target_asn(
            data_centers=cleaned.get("TopologyDataCenter") or [],
            metros=cleaned.get("TopologyColocationMetro") or [],
        )
        self._validate_vni_uniqueness(
            segments=cleaned.get("ManagedVxlanSegment") or [],
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

        # L3 VNIs come from the GLOBAL-L3VNI pool, so they are global, not scoped
        # to a deployment.
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
                        "each other. Reallocate from the GLOBAL-L3VNI pool."
                    )
                )

        # The crossover case: same 24-bit space, so an L2/L3 collision is just as
        # real as an L2/L2 one and is the easier of the two to introduce, because
        # the L2 and L3 pools are configured independently per fabric.
        for dep_name, by_vni in sorted(l2_by_deployment.items()):
            for vni in sorted(set(by_vni) & set(l3_by_vni)):
                self.log_error(
                    message=(
                        f"VNI {vni} is used as an L2 VNI in deployment '{dep_name}' (segments "
                        f"{sorted(by_vni[vni])}) and as an L3 VNI for namespace(s) "
                        f"{sorted(l3_by_vni[vni])}. A VNI on the wire carries no L2/L3 "
                        "discriminator, so bridged and routed traffic would share one VNI. Make "
                        "the L2 and L3 VNI pool ranges disjoint."
                    )
                )

    def _check_encodable(self, vni: int, what: str) -> None:
        if vni > _MAX_ENCODABLE_VNI:
            self.log_error(
                message=(
                    f"{what} is {vni}, above {_MAX_ENCODABLE_VNI}. Every ASN in this project is "
                    "4-byte, and both the type-1 route-distinguisher and the type-2 route-target "
                    "leave only 16 bits for the assigned field, so this VNI cannot be encoded in "
                    f"them. Keep VNI pool ranges at or below {_MAX_ENCODABLE_VNI}."
                )
            )
