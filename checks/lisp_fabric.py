"""Validate the fabrics that run the LISP overlay instead of BGP EVPN.

The LISP overlay (overlay_control_plane = lisp) is a deliberately narrow
profile: the cisco_xe templates render map-server/map-resolver on the border
leaves and ETRs on the edge (leaf / access-leaf / tor), over an OSPFv2 IPv4
underlay (routing_strategy ospf-ibgp). Anything outside that profile
renders config that is syntactically valid and silently does not forward, so
the profile is enforced here, on the fabric as a whole, not per device.

A fabric with the default bgp_evpn control plane produces no output.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

LISP = "lisp"
LISP_PLATFORM = "cisco_xe"
LISP_STRATEGY = "ospf-ibgp"
LISP_UNDERLAY = "ipv4"
EDGE_ROLES = frozenset({"leaf", "access-leaf", "tor", "border-leaf"})
"""Roles that carry a LISP role (ETR, or co-located map-server/map-resolver on the border leaf)."""
SPINE_OVERLAY_ROLES = frozenset({"super-spine", "hyper-spine", "border-spine"})
"""Spine tiers with an overlay role under BGP EVPN (route reflector); LISP has none, spine is transit only."""
MIN_BORDER_LEAFS = 2
"""Border leaves are the map-servers; the pair keeps registrations alive through the loss of one."""


class CheckLispFabric(InfrahubCheck):
    """LISP overlay profile: ospf-ibgp, IPv4 underlay, two cisco_xe borders, gatewayed segments."""

    query = "lisp_fabric"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        devices_by_dc: dict[str, list[dict[str, Any]]] = {}
        for pod in cleaned.get("TopologyPod") or []:
            parent = pod.get("parent") or {}
            devices_by_dc.setdefault(str(parent.get("id")), []).extend(pod.get("devices") or [])

        for dc in cleaned.get("TopologyDataCenter") or []:
            if dc.get("overlay_control_plane") != LISP:
                continue
            devices = (dc.get("devices") or []) + devices_by_dc.get(str(dc.get("id")), [])
            self._validate_data_center(dc, devices)

        for metro in cleaned.get("TopologyColocationMetro") or []:
            if metro.get("overlay_control_plane") == LISP:
                self.log_error(
                    message=(
                        f"Colocation metro '{metro.get('name')}' sets overlay_control_plane=lisp, which is "
                        "only supported on a data center. Set it back to bgp_evpn."
                    ),
                    object_id=metro.get("id"),
                    object_type="TopologyColocationMetro",
                )

    def _validate_data_center(self, dc: dict[str, Any], devices: list[dict[str, Any]]) -> None:
        name = dc.get("name", "<unnamed-data-center>")
        problems: list[str] = []

        strategy = dc.get("routing_strategy") or "ebgp-ebgp"
        if strategy != LISP_STRATEGY:
            problems.append(f"routing_strategy is {strategy}, LISP requires {LISP_STRATEGY}")
        underlay = dc.get("underlay_protocol") or "ipv6"
        if underlay != LISP_UNDERLAY:
            problems.append(f"underlay_protocol is {underlay}, LISP renders an OSPFv2 IPv4 underlay only")

        borders = [device for device in devices if device.get("role") == "border-leaf"]
        if len(borders) < MIN_BORDER_LEAFS:
            problems.append(
                f"it has {len(borders)} border-leaf device(s), LISP needs {MIN_BORDER_LEAFS} (the map-servers)"
            )

        for device in devices:
            role = device.get("role")
            device_name = device.get("name", "<unnamed-device>")
            if role in SPINE_OVERLAY_ROLES:
                problems.append(f"{role} '{device_name}' has no LISP overlay role (spine is transit only)")
            elif role in EDGE_ROLES:
                platform = (device.get("platform") or {}).get("name")
                if platform != LISP_PLATFORM:
                    problems.append(
                        f"{role} '{device_name}' is on platform {platform}, LISP renders {LISP_PLATFORM} only"
                    )

        if devices and not dc.get("lisp_site_key"):
            problems.append("it has no lisp_site_key, the ETRs cannot authenticate to the map-servers")

        for activation in dc.get("segment_deployments") or []:
            segment = activation.get("segment") or {}
            if not segment.get("gateway"):
                problems.append(
                    f"segment '{segment.get('name')}' (VNI {activation.get('vni')}) has no gateway, "
                    "LISP does not render L2 flooding so a segment needs a gateway"
                )

        for problem in problems:
            self.log_error(
                message=f"Data center '{name}' runs the LISP overlay but {problem}.",
                object_id=dc.get("id"),
                object_type="TopologyDataCenter",
            )
