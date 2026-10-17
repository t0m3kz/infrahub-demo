"""Validate the fabrics that run the LISP overlay instead of BGP EVPN.

The LISP overlay (overlay_control_plane = lisp) is a deliberately narrow
profile: the cisco_xe templates render map-server/map-resolver on the border
leaves and ETRs on the edge (leaf / access-leaf / tor), over an OSPFv2 IPv4
underlay (routing_strategy ospf-ibgp). Anything outside that profile
renders config that is syntactically valid and silently does not forward, so
the profile is enforced here, on the fabric as a whole, not per device.

An office with a fabric (fabric_templates) is a LISP site by role, not by flag: its access-switches and
distribution-switches always run LISP, so its profile is checked here too (cisco_xe, a distribution-switch
map-server, a site key, every segment in a VRF with an L3 instance-id, an SD-WAN edge with the BGP process of its WAN
exit, one LISP footprint per building).

A fabric with the default bgp_evpn control plane produces no output.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES

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
OFFICE_FABRIC_ROLES = CAMPUS_FABRIC_ROLES | LISP_BORDER_ROLES
"""The roles of an office LISP site; any other device of the office (its SD-WAN edge) is not part of it."""
MIN_DISTRIBUTION_SWITCHES = 1
"""Distribution switches are the office's map-servers; a single one is enough for the demo site."""
EDGE_ROLE = "edge"
"""The office's SD-WAN edge (add_sdwan_edge): the far end of the WAN exit of the distribution switches."""


def _gateway_problems(activations: list[dict[str, Any]]) -> list[str]:
    """One problem per active segment without a gateway: no L2 flooding is rendered, so it cannot work."""
    return [
        f"segment '{(activation.get('segment') or {}).get('name')}' (VNI {activation.get('vni')}) has no gateway, "
        "LISP does not render L2 flooding so a segment needs a gateway"
        for activation in activations
        if not (activation.get("segment") or {}).get("gateway")
    ]


def _vrf_problems(activations: list[dict[str, Any]]) -> list[str]:
    """One problem per active segment whose gateway is not in a VRF with an L3 instance-id.

    The transform refuses to render such a segment (a host SVI would land in the global table next to the EID
    tables), so the check says so before an artifact fails.
    """
    problems: list[str] = []
    for activation in activations:
        segment = activation.get("segment") or {}
        gateway = segment.get("gateway")
        if not gateway:
            continue  # no gateway: _gateway_problems reports it
        namespace = ((gateway.get("ip_prefix") or {}).get("ip_namespace") or {}) if isinstance(gateway, dict) else {}
        vrf = namespace.get("name")
        if not vrf or vrf == "default":
            problems.append(
                f"segment '{segment.get('name')}' (VNI {activation.get('vni')}) has its gateway in the default "
                "namespace, LISP needs a VRF namespace with an l3_vni"
            )
        elif not namespace.get("l3_vni"):
            problems.append(
                f"segment '{segment.get('name')}' (VNI {activation.get('vni')}) is in VRF '{vrf}' which has no "
                "l3_vni, LISP needs an L3 instance-id"
            )
    return problems


class CheckLispFabric(InfrahubCheck):
    """LISP overlay profile: a DC's ospf-ibgp/IPv4/two-border profile, an office's single-site profile."""

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

        self._validate_offices(
            [office for office in cleaned.get("TopologyCustomerOffice") or [] if office.get("fabric_templates")]
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

        problems.extend(_gateway_problems(dc.get("segment_deployments") or []))

        for problem in problems:
            self.log_error(
                message=f"Data center '{name}' runs the LISP overlay but {problem}.",
                object_id=dc.get("id"),
                object_type="TopologyDataCenter",
            )

    def _validate_offices(self, offices: list[dict[str, Any]]) -> None:
        """Validate every office that has a fabric: they are the LISP footprints, one allowed per building."""
        offices_by_building: dict[str, list[str]] = {}
        for office in offices:
            for building in office.get("locations") or []:
                offices_by_building.setdefault(str(building.get("id")), []).append(office.get("name", "<unnamed>"))
        for office in offices:
            self._validate_office(office, offices_by_building)

    def _validate_office(self, office: dict[str, Any], offices_by_building: dict[str, list[str]]) -> None:
        name = office.get("name", "<unnamed-office>")
        problems: list[str] = []

        fabric = [device for device in office.get("devices") or [] if device.get("role") in OFFICE_FABRIC_ROLES]
        distribution = [device for device in fabric if device.get("role") in LISP_BORDER_ROLES]
        if len(distribution) < MIN_DISTRIBUTION_SWITCHES:
            problems.append(
                f"it has {len(distribution)} distribution-switch device(s), LISP needs {MIN_DISTRIBUTION_SWITCHES} "
                "(the map-server and border)"
            )

        for device in fabric:
            platform = (device.get("platform") or {}).get("name")
            if platform != LISP_PLATFORM:
                problems.append(
                    f"{device.get('role')} '{device.get('name', '<unnamed-device>')}' is on platform {platform}, "
                    f"LISP renders {LISP_PLATFORM} only"
                )

        if fabric and not office.get("lisp_site_key"):
            problems.append("it has no lisp_site_key, the ETRs cannot authenticate to the map-servers")

        # The exit is built by add_office_fabric for the edge the office has: an edge with no BGP process of its own
        # means the exit is missing (the generator ran before the edge existed and has not run since).
        for device in office.get("devices") or []:
            if device.get("role") == EDGE_ROLE and not any(
                capability.get("typename") == "ManagedBGP" for capability in device.get("capabilities") or []
            ):
                problems.append(
                    f"its SD-WAN edge '{device.get('name', '<unnamed-device>')}' has no BGP process, "
                    "the WAN exit of the distribution-switches is not built"
                )

        activations = office.get("segment_deployments") or []
        problems.extend(_gateway_problems(activations))
        problems.extend(_vrf_problems(activations))

        for building in office.get("locations") or []:
            others = sorted(n for n in offices_by_building.get(str(building.get("id")), []) if n != name)
            if others:
                problems.append(
                    f"building '{building.get('name')}' also hosts the LISP fabric of {', '.join(others)}, "
                    "the demo allows one LISP footprint per building"
                )

        for problem in problems:
            self.log_error(
                message=f"Office '{name}' runs a LISP fabric but {problem}.",
                object_id=office.get("id"),
                object_type="TopologyCustomerOffice",
            )
