"""Validate edge."""

from typing import Any

from transforms.common import _fabric_rt_asn
from transforms.helpers.vxlan import _internet_session_namespaces, _is_bgw
from utils.data_cleaning import get_data

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


def warn_transit_without_internet_bgw(device: dict[str, Any]) -> list[str]:
    """Warnings for an edge that has an upstream transit session but cannot export it.

    A transit session is a VRF session whose local address sits in a
    namespace_type internet namespace (the ``transit`` flag of
    transforms/helpers/bgp.py). Its default route only reaches the DCs as an
    EVPN type-5 route when the edge is its metro's border gateway (a
    ``multisite-vip`` interface and a fabric ``evpn_rt_as``) and the INTERNET
    namespace has an L3 VNI. These are warnings, not errors: a guard for an edge
    whose session renders but whose default cannot be reached from any DC (it
    has no site VIP, or the fabric has no evpn_rt_as).
    """
    namespaces = _internet_session_namespaces(device)
    if not namespaces:
        return []

    name = device.get("name", "unknown")
    warnings: list[str] = []
    if not _is_bgw(device, _fabric_rt_asn(device.get("deployment"))):
        warnings.append(
            f"Edge '{name}' has an upstream transit session in VRF "
            f"{', '.join(sorted(ns['name'] for ns in namespaces))} but is not an EVPN Multi-Site border "
            "gateway (needs a multisite-vip interface and a fabric evpn_rt_as): its default route "
            "cannot reach any DC."
        )
    warnings.extend(
        f"Edge '{name}' has an upstream transit session in VRF {ns['name']} but the namespace has no L3 VNI."
        for ns in namespaces
        if not ns.get("l3_vni")
    )
    return warnings


class CheckEdge(BaseDeviceCheck):
    """Check Edge."""

    query = "edge_config"
    validators = [validate_interfaces, validate_routing_password]

    def validate(self, data: Any) -> None:
        super().validate(data)
        # InfrahubCheck has no log_warning; log_info does not block the merge.
        for warning in warn_transit_without_internet_bgw(get_data(data)):
            self.log_info(message=f"WARNING: {warning}")
