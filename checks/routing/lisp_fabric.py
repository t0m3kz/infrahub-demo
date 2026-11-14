"""Check the LISP fabric of every office that has one (cisco_xe, map-server, site key, VRF instance-ids, SD-WAN edge,
one footprint per building).
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES
from utils.routing_domain import RoutingDomainError
from utils.segment_addressing import segment_namespace

LISP_PLATFORM = "cisco_xe"


def platform_supports_lisp(platform: dict[str, Any] | str | None) -> bool:
    """Honor DcimPlatformCapabilityProfile.supports_lisp when present; fall back to LISP_PLATFORM."""
    if isinstance(platform, dict):
        os_caps = platform.get("os_capabilities")
        if isinstance(os_caps, dict) and "supports_lisp" in os_caps:
            return bool(os_caps["supports_lisp"])
        return platform.get("name") == LISP_PLATFORM
    if isinstance(platform, str):
        return platform == LISP_PLATFORM
    return False


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
    """One problem per active segment whose gateway is not in a VRF with an L3 instance-id (the transform refuses it)."""
    problems: list[str] = []
    for activation in activations:
        segment = activation.get("segment") or {}
        gateway = segment.get("gateway")
        if not gateway:
            continue  # no gateway: _gateway_problems reports it
        try:
            namespace = segment_namespace(segment)
        except RoutingDomainError as exc:
            problems.append(f"segment '{segment.get('name')}' (VNI {activation.get('vni')}): {exc}")
            continue
        vrf = namespace.get("name")
        if not vrf:
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
    """LISP profile of an office fabric: a single site with a distribution-switch map-server."""

    query = "lisp_fabric"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        self._validate_offices(
            [office for office in cleaned.get("TopologyCustomerOffice") or [] if office.get("fabric_templates")]
        )

    def _validate_offices(self, offices: list[dict[str, Any]]) -> None:
        """Validate every office with a fabric (one LISP footprint per building)."""
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

        # An edge with no BGP process means add_office_fabric ran before the edge existed.
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
