"""Validate that a port is on one layer: a switchport (L2) or a routed port (L3), not both, and its segment membership.

There is no ``l2_mode`` attribute: the layer of a port is its content.

* An L2 tag on a switch port (physical or LAG) is a ``ManagedSegmentDeployment`` among its ``capabilities`` (the
  segment's realization in the port's topology, utils/segment_vlan.py ``port_deployments``). A border service port
  that faces a firewall or load-balancer pair is a trunk too, but a context has no interface edge (its border ports
  are derived from the cables, utils/service_contexts.py), so this check does not see it; a terminated segment it
  carries is a deployment tag.
* An L3 signal is an ``ip_address``, any ``sub_interfaces``, or a routing capability (a BGP or OSPF session, an OSPF
  interface, a routed exchange, an interconnect, tunnel or handoff circuit). A carrier circuit is L1 and never a signal.

Rules:

* R1 a physical or LAG port (not a LAG member) has both an L2 tag and an L3 signal;
* R2 a LAG member (``lag`` set) has an L2 tag or an L3 signal: its LAG carries them;
* R3 a segment deployment lists only physical ports and LAGs (its interface edge peers any ``DcimInterface``);
* R4 a port's segment deployment is in the hosting deployment of the port's device (the DC, metro or office it sits
  in, utils/segment_addressing.py ``hosting_deployment``); a device with no hosting deployment is not judged;
* R5 a port is a member of at most one deployment of a segment.

A segment itself cannot sit in the ``capabilities`` of a port: ManagedVxlanSegment does not inherit the interface-edge
generic (tests/unit/test_capability_edge_names.py), so switchport membership is only a deployment.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.segment_addressing import hosting_deployment
from utils.segment_vlan import port_deployments

SWITCHED_PORT_KINDS = frozenset({"DcimPhysicalInterface", "DcimLAGInterface"})
"""The interface kinds that may be a member of a segment deployment (DcimSwitchedInterface)."""

L3_SIGNAL_KINDS = frozenset(
    {
        "ManagedBGPPeering",
        "RoutingOSPFInterface",
        "ManagedOSPFPeering",
        "TopologyRoutedExchange",
        "TopologySessionCircuit",
    }
)
"""Capabilities that make a port a routed port besides an address and sub-interfaces."""


def capability_kinds(port: dict[str, Any]) -> set[str]:
    """The ``__typename`` of each capability of a cleaned interface node."""
    return {cap.get("typename") for cap in port.get("capabilities") or []}


def l2_tags(port: dict[str, Any]) -> list[str]:
    """What makes a cleaned physical or LAG port a switchport."""
    return ["segment deployments"] if port_deployments(port) else []


def l3_signals(port: dict[str, Any]) -> list[str]:
    """What makes a cleaned physical or LAG port a routed port."""
    signals = [
        label
        for label, found in (("ip_address", port.get("ip_address")), ("sub_interfaces", port.get("sub_interfaces")))
        if found
    ]
    return signals + sorted(capability_kinds(port) & L3_SIGNAL_KINDS)


def _where(port: dict[str, Any]) -> str:
    return f"port '{(port.get('device') or {}).get('name')}' {port.get('name')}"


def switch_port_errors(port: dict[str, Any]) -> list[str]:
    """R1 and R2 of one cleaned DcimPhysicalInterface or DcimLAGInterface node."""
    errors = []
    tags, signals = l2_tags(port), l3_signals(port)
    where = _where(port)
    if port.get("lag"):
        if tags or signals:
            errors.append(
                f"{where} is a LAG member but carries {', '.join(tags + signals)}: put it on the LAG, not on the member."
            )
    elif tags and signals:
        errors.append(
            f"{where} is both a switchport ({', '.join(tags)}) and a routed port ({', '.join(signals)}): "
            "a port is on one layer."
        )
    return errors


def _segment_name(deployment: dict[str, Any]) -> str:
    return str((deployment.get("segment") or {}).get("name"))


def membership_errors(port: dict[str, Any]) -> list[str]:
    """R4 and R5 of one cleaned DcimPhysicalInterface or DcimLAGInterface node."""
    errors = []
    where = _where(port)
    deployments = port_deployments(port)
    device = port.get("device") or {}
    hosting = hosting_deployment(device.get("deployment"))
    if hosting and hosting.get("id"):
        for dep in deployments:
            dep_hosting = dep.get("deployment") or {}
            if dep_hosting.get("id") and dep_hosting["id"] != hosting["id"]:
                errors.append(
                    f"{where} is a member of segment '{_segment_name(dep)}' in '{dep_hosting.get('name')}' but its "
                    f"device sits in '{hosting.get('name')}' (rule R4: a port carries the segment's deployment of its "
                    "own topology)."
                )
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for dep in deployments:
        segment_id = (dep.get("segment") or {}).get("id")
        if segment_id:
            by_segment.setdefault(str(segment_id), []).append(dep)
    for found in by_segment.values():
        if len(found) > 1:
            places = ", ".join(sorted(str((dep.get("deployment") or {}).get("name")) for dep in found))
            errors.append(
                f"{where} is a member of {len(found)} deployments of segment '{_segment_name(found[0])}' ({places}) "
                "(rule R5: one deployment of a segment per port)."
            )
    return errors


def deployment_kind_errors(deployment: dict[str, Any]) -> list[str]:
    """R3 of one cleaned ManagedSegmentDeployment node: every interface it lists is a physical port or a LAG."""
    return [
        f"segment '{_segment_name(deployment)}' in '{(deployment.get('deployment') or {}).get('name')}' lists "
        f"{iface.get('typename')} '{(iface.get('device') or {}).get('name')}' {iface.get('name')} "
        "(rule R3: only a physical port or a LAG carries a segment)."
        for iface in deployment.get("capabilities") or []
        if isinstance(iface, dict) and iface.get("typename") not in SWITCHED_PORT_KINDS
    ]


class CheckInterfaceLayers(InfrahubCheck):
    """A port is a switchport or a routed port, a LAG member carries neither (R1, R2); a segment deployment lists only
    switch ports (R3), of its own topology (R4), one deployment of a segment per port (R5)."""

    query = "interface_layers"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for port in (*(cleaned.get("DcimPhysicalInterface") or []), *(cleaned.get("DcimLAGInterface") or [])):
            for message in switch_port_errors(port) + membership_errors(port):
                self.log_error(message=message, object_id=port.get("id"))
        for deployment in cleaned.get("ManagedSegmentDeployment") or []:
            for message in deployment_kind_errors(deployment):
                self.log_error(message=message, object_id=deployment.get("id"))
