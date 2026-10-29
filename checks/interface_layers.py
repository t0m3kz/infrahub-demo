"""Validate that a port is on one layer: a switchport (L2) or a routed port (L3), not both.

There is no ``l2_mode`` attribute: the layer of a port is its content.

* An L2 tag on a switch port (physical or LAG) is its ``segments`` (switchport membership, ManagedVxlanSegment
  ``switched_interfaces``) or a firewall / load-balancer context in its ``capabilities``.
* An L3 signal is an ``ip_address``, any ``sub_interfaces``, or a routing capability (a BGP or OSPF session, an OSPF
  interface, a routed exchange, an interconnect, tunnel or handoff circuit). A physical circuit is L1 and never a signal.

Rules:

* R1 a physical or LAG port (not a LAG member) has both an L2 tag and an L3 signal;
* R2 a LAG member (``lag`` set) has an L2 tag or an L3 signal: its LAG carries them;
* R5 a ManagedVxlanSegment in the ``capabilities`` of a physical or LAG port is an ERROR: switchport membership is
  ``segments``. (The inline termination on a virtual sub-interface is rule 6 of checks/interface_parents.py.)
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

SEGMENT_KIND = "ManagedVxlanSegment"
L2_TAG_KINDS = frozenset({"ManagedFirewallContext", "ManagedLoadbalancerContext"})
"""Capabilities that make a switch port an L2 port besides ``segments``."""
L3_SIGNAL_KINDS = frozenset(
    {
        "ManagedBGPPeering",
        "RoutingOSPFInterface",
        "ManagedOSPFPeering",
        "TopologyRoutedExchange",
        "TopologyInterconnectCircuit",
        "TopologyTunnelCircuit",
        "TopologyHandoffCircuit",
    }
)
"""Capabilities that make a port a routed port besides an address and sub-interfaces."""


def capability_kinds(port: dict[str, Any]) -> set[str]:
    """The ``__typename`` of each capability of a cleaned interface node."""
    return {cap.get("typename") for cap in port.get("capabilities") or []}


def l2_tags(port: dict[str, Any]) -> list[str]:
    """What makes a cleaned physical or LAG port a switchport."""
    tags = ["segments"] if port.get("segments") else []
    return tags + sorted(capability_kinds(port) & L2_TAG_KINDS)


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
    """R1, R2 and R5 of one cleaned DcimPhysicalInterface or DcimLAGInterface node."""
    errors = []
    tags, signals = l2_tags(port), l3_signals(port)
    where = _where(port)
    if SEGMENT_KIND in capability_kinds(port):
        errors.append(
            f"{where} has a {SEGMENT_KIND} in its capabilities: a segment on a switch port is its `segments` "
            "(only a sub-interface carries the inline termination)."
        )
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


class CheckInterfaceLayers(InfrahubCheck):
    """A port is a switchport or a routed port, a LAG member carries neither, and `segments` is its only segment edge."""

    query = "interface_layers"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for port in (*(cleaned.get("DcimPhysicalInterface") or []), *(cleaned.get("DcimLAGInterface") or [])):
            for message in switch_port_errors(port):
                self.log_error(message=message, object_id=port.get("id"))
