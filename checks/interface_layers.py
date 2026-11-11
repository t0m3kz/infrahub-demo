"""Validate that a port is on one layer: a switchport (L2) or a routed port (L3), not both, and its segment membership.

There is no ``l2_mode`` attribute: the layer of a port is its content.

* An L2 tag on a switch port (physical or LAG) is a VLAN row of its switch, a ``ManagedSegmentVlan`` among its
  ``capabilities`` (the segment's realization on the port's device, utils/segment_vlan.py ``port_rows``), or the
  segment's deployment the port is assigned to (``ManagedSegmentDeployment``, the request). A border service port that
  faces a firewall or load-balancer pair is a trunk too, but a context has no interface edge (its border ports are
  derived from the cables, utils/service_contexts.py), so this check does not see it; a terminated segment it carries
  is a row tag.
* An L3 signal is an ``ip_address``, a sub-interface (an interface whose encapsulation has the port as its parent) other than a VM NIC pinned to the port (a base NIC of a virtual
  device rides on its host's port, bond member included: the attachment of a machine is no routing, and
  generators/virtual_nics.py pins to the cabled physical port, never to the bond), or a routing capability (a BGP session, an OSPF
  interface, an interconnect, tunnel or handoff circuit). A carrier circuit is L1 and never a signal.

Rules:

* R1 a physical or LAG port (not a LAG member) has both an L2 tag and an L3 signal;
* R2 a LAG member (``lag`` set) has an L2 tag or an L3 signal: its LAG carries them;
* R3 a segment deployment lists only physical ports and LAGs, and a VLAN row only physical ports, LAGs and its SVI (the
  interface edge peers any ``DcimInterface``; the SVI is the role-``svi`` virtual interface a routed row is realized
  as, utils/segment_vlan.py ``svi_rows``);
* R4 a port is a member of at most one row of a segment;
* R5 (WARNING) a port is a member of a row nobody asked for: the port is not assigned to the row's deployment, it faces
  no host of an instance of a component on the segment (its cable, or a LAG member's, ends on an uplink or LAG NIC of
  the host, as generators/topology/segment_ports.py derives it) and it is not a firewall service port of a terminated
  segment. Only the segment reconcile writes a membership, and its next run removes this one: a hand edit is visible
  here until then.

* R6 a row lists only ports and its SVI of its own switch (its ``device``): the interface edge holds no same-device
  rule in the schema (no ``common_parent``), so this check does;
* R7 the interfaces of an OSPF interface setting (``RoutingOSPFInterface``) are all on ONE device: the setting has no
  device of its own, so that is all the check can judge of the setting by itself;
* R8 (WARNING) an OSPF interface setting lists no interface: it no longer goes with a device, so a setting left behind
  by a deleted device or interface shows here (the generator's next run does not reclaim it);
* R9 an OSPF interface setting that is part of a peering is on a device that one of the peering's processes runs on
  (``ManagedOSPF.capabilities``). A setting with no peering, or a peering with no process, is not judged.
* R10 a row has at most one SVI among its members. An SVI of another switch is R6's (the SVI is a member of its row
  through the same edge as a port, with no same-device rule in the schema); that the SVI is the row's routed one and
  carries the segment's gateway is checks/vrf_instances.py (S1, S2, S5).

A segment itself cannot sit in the ``capabilities`` of a port: it does not inherit the interface-edge generic
(tests/unit/test_capability_edge_names.py).
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from generators.topology.endpoint import ENDPOINT_NIC_ROLES
from generators.virtual_nics import is_host_nic
from utils.app_instances import physical_host
from utils.data_cleaning import clean_data
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, SERVICE_CONTEXT_ROLES
from utils.interface_encapsulation import sub_interfaces_of
from utils.segment_vlan import DEPLOYMENT_KIND, port_rows
from utils.vrf_demand import SVI_ROLE

L3_SIGNAL_KINDS = frozenset(
    {
        "ManagedBGPPeering",
        "RoutingOSPFInterface",
        "TopologySessionCircuit",
    }
)
"""Capabilities that make a port a routed port besides an address and sub-interfaces."""


def capability_kinds(port: dict[str, Any]) -> set[str]:
    """The ``__typename`` of each capability of a cleaned interface node."""
    return {cap.get("typename") for cap in port.get("capabilities") or []}


SWITCHED_PORT_KINDS = frozenset({"DcimPhysicalInterface", "DcimLAGInterface"})
"""The interface kinds that may be assigned to a segment deployment or be a member of a row (DcimSwitchedInterface)."""

SVI_KIND = "DcimVirtualInterface"
"""A row's one non-port member is the segment SVI: a virtual interface of role ``svi``."""


def is_svi(member: dict[str, Any]) -> bool:
    """Whether a cleaned member of a row is its SVI (not a port)."""
    return member.get("typename") == SVI_KIND and member.get("role") == SVI_ROLE


def l2_tags(port: dict[str, Any]) -> list[str]:
    """What makes a cleaned physical or LAG port a switchport: its rows (realized) and its deployments (asked)."""
    tags = ["segment VLAN rows"] if port_rows(port) else []
    return tags + (["segment deployments"] if DEPLOYMENT_KIND in capability_kinds(port) else [])


def l3_signals(port: dict[str, Any]) -> list[str]:
    """What makes a cleaned physical or LAG port a routed port."""
    routed_subs = [sub for sub in sub_interfaces_of(port) if not is_host_nic(sub)]
    signals = [
        label for label, found in (("ip_address", port.get("ip_address")), ("sub_interfaces", routed_subs)) if found
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


def _row_segment(row: dict[str, Any]) -> dict[str, Any]:
    """The segment of a cleaned row (through its ``segment_deployment``)."""
    return (row.get("segment_deployment") or {}).get("segment") or {}


def _row_where(row: dict[str, Any]) -> str:
    return (
        f"VLAN row {row.get('vlan_id')} of segment '{_row_segment(row).get('name')}' on "
        f"'{(row.get('device') or {}).get('name')}'"
    )


def membership_errors(port: dict[str, Any]) -> list[str]:
    """R4 of one cleaned DcimPhysicalInterface or DcimLAGInterface node."""
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for row in port_rows(port):
        segment_id = _row_segment(row).get("id")
        if segment_id:
            by_segment.setdefault(str(segment_id), []).append(row)
    return [
        f"{_where(port)} is a member of {len(found)} VLAN rows of segment '{_row_segment(found[0]).get('name')}' "
        f"(VLANs {', '.join(sorted(str(row.get('vlan_id')) for row in found))}) (rule R4: one row of a segment per port)."
        for found in by_segment.values()
        if len(found) > 1
    ]


LAG_MEMBER_ROLE = "lag"
"""The role of a switch port bundled into a LAG: the reconcile tags its LAG, not the port."""


def component_hosts(components: list[dict[str, Any]]) -> dict[str, set[str]]:
    """{segment id: ids of the physical hosts of the instances of its components} (cleaned AppComponent nodes)."""
    hosts: dict[str, set[str]] = {}
    for component in components:
        segment_id = (component.get("network_segment") or {}).get("id")
        for instance in component.get("instances") or []:
            host = physical_host(instance) if isinstance(instance, dict) else None
            if segment_id and host and host.get("id"):
                hosts.setdefault(str(segment_id), set()).add(str(host["id"]))
    return hosts


def _faces_a_host(port: dict[str, Any], hosts: set[str]) -> bool:
    """Whether the cable of a cleaned physical port ends on an uplink or LAG NIC of one of ``hosts``."""
    return any(
        far.get("id") != port.get("id")
        and (far.get("device") or {}).get("id") in hosts
        and far.get("role") in ENDPOINT_NIC_ROLES
        for far in (port.get("cable") or {}).get("endpoints") or []
        if isinstance(far, dict)
    )


def is_derived(port: dict[str, Any], hosts: set[str], terminated: bool) -> bool:
    """Whether the reconcile derives a cleaned row member: a plain port facing a host, a LAG one of whose members faces
    one, or (``terminated``) a firewall service port."""
    if terminated and port.get("role") == SERVICE_CONTEXT_ROLES[FIREWALL_CONTEXT_KIND]:
        return True
    if port.get("typename") == "DcimLAGInterface":
        return any(
            member.get("role") == LAG_MEMBER_ROLE and _faces_a_host(member, hosts)
            for member in port.get("member_interfaces") or []
            if isinstance(member, dict)
        )
    return port.get("role") != LAG_MEMBER_ROLE and _faces_a_host(port, hosts)


def kind_errors(owner: str, ports: list[Any], *, allow_svi: bool = False) -> list[str]:
    """R3: the ports ``owner`` (a cleaned deployment's or row's label) lists that are no physical port or LAG (a row's
    SVI is no port and is allowed there: ``allow_svi``)."""
    return [
        f"{owner} lists {port.get('typename')} '{(port.get('device') or {}).get('name')}' {port.get('name')} "
        "(rule R3: only a physical port or a LAG carries a segment)."
        for port in ports
        if isinstance(port, dict)
        and port.get("typename") not in SWITCHED_PORT_KINDS
        and not (allow_svi and is_svi(port))
    ]


def unrequested_members(row: dict[str, Any], hosts: dict[str, set[str]]) -> list[str]:
    """R5 of one cleaned ManagedSegmentVlan node: its members that are neither assigned to its deployment nor
    derived."""
    deployment = row.get("segment_deployment") or {}
    segment = deployment.get("segment") or {}
    requested = {str(port.get("id")) for port in deployment.get("capabilities") or [] if isinstance(port, dict)}
    segment_hosts = hosts.get(str(segment.get("id")), set())
    terminated = bool(segment.get("terminated_on"))
    return [
        f"{_row_where(row)} lists '{(port.get('device') or {}).get('name')}' {port.get('name')}, which nobody asked "
        "for (rule R5: not assigned to the deployment and derived from no component; the next segment reconcile "
        "removes it)."
        for port in row.get("capabilities") or []
        if isinstance(port, dict)
        and not is_svi(port)
        and str(port.get("id")) not in requested
        and not is_derived(port, segment_hosts, terminated)
    ]


def row_device_errors(row: dict[str, Any]) -> list[str]:
    """R6 of one cleaned ManagedSegmentVlan node: its ports that are on another device than the row's."""
    owner = (row.get("device") or {}).get("name")
    return [
        f"{_row_where(row)} lists '{(port.get('device') or {}).get('name')}' {port.get('name')}, a port of another "
        "device (rule R6: a row lists only ports and the SVI of its own switch)."
        for port in row.get("capabilities") or []
        if isinstance(port, dict) and (port.get("device") or {}).get("name") != owner
    ]


def row_svi_errors(row: dict[str, Any]) -> list[str]:
    """R10 of one cleaned ManagedSegmentVlan node: more than one SVI among its members."""
    svis = [member for member in row.get("capabilities") or [] if isinstance(member, dict) and is_svi(member)]
    if len(svis) < 2:
        return []
    return [
        f"{_row_where(row)} has {len(svis)} SVIs ({', '.join(sorted(str(svi.get('name')) for svi in svis))}) "
        "(rule R10: a row is realized as one SVI)."
    ]


def _device_names(ports: list[Any]) -> set[str]:
    return {str((port.get("device") or {}).get("name")) for port in ports if isinstance(port, dict)}


def ospf_setting_issues(setting: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(errors, warnings) of one cleaned RoutingOSPFInterface node: R7, R8 and R9."""
    where = f"OSPF interface setting '{setting.get('name')}'"
    ports = [port for port in setting.get("capabilities") or [] if isinstance(port, dict)]
    if not ports:
        return [], [f"{where} lists no interface (rule R8: nothing applies it; delete it)."]
    devices = _device_names(ports)
    if len(devices) > 1:
        return [
            f"{where} lists interfaces of {len(devices)} devices ({', '.join(sorted(devices))}) "
            "(rule R7: the interfaces of one setting are all on one device)."
        ], []
    processes = [
        process for process in (setting.get("peering") or {}).get("ospf_process") or [] if isinstance(process, dict)
    ]
    run_on = {
        str(device.get("name")) for process in processes for device in process.get("capabilities") or [] if device
    }
    (device,) = devices
    if run_on and device not in run_on:
        return [
            f"{where} is on '{device}', which runs none of the OSPF processes of its peering "
            f"({', '.join(sorted(run_on))}) (rule R9)."
        ], []
    return [], []


class CheckInterfaceLayers(WarningCheck):
    """A port is a switchport or a routed port, a LAG member carries neither (R1, R2); a segment deployment or a row
    lists only switch ports (R3, a row also its SVI); one row of a segment per port (R4); a row member nobody asked for is a WARNING
    (R5); a row lists ports and an SVI of its own switch (R6) and at most one SVI (R10); an OSPF interface setting is on one device (R7), lists an
    interface (R8, WARNING) and is on a device of its peering (R9)."""

    query = "interface_layers"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for port in (*(cleaned.get("DcimPhysicalInterface") or []), *(cleaned.get("DcimLAGInterface") or [])):
            for message in switch_port_errors(port) + membership_errors(port):
                self.log_error(message=message, object_id=port.get("id"))
        for deployment in cleaned.get("ManagedSegmentDeployment") or []:
            owner = (
                f"segment '{(deployment.get('segment') or {}).get('name')}' in "
                f"'{(deployment.get('deployment') or {}).get('name')}'"
            )
            for message in kind_errors(owner, deployment.get("capabilities") or []):
                self.log_error(message=message, object_id=deployment.get("id"))
        hosts = component_hosts(cleaned.get("AppComponent") or [])
        for row in cleaned.get("ManagedSegmentVlan") or []:
            for message in (
                kind_errors(_row_where(row), row.get("capabilities") or [], allow_svi=True)
                + row_device_errors(row)
                + row_svi_errors(row)
            ):
                self.log_error(message=message, object_id=row.get("id"))
            for message in unrequested_members(row, hosts):
                self.log_warning(message=message)
        for setting in cleaned.get("RoutingOSPFInterface") or []:
            errors, warnings = ospf_setting_issues(setting)
            for message in errors:
                self.log_error(message=message, object_id=setting.get("id"))
            for message in warnings:
                self.log_warning(message=message)
