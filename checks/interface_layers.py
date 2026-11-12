"""Check that a port is on one layer, a switchport (L2) or a routed port (L3), and its segment membership.

Rules R1-R10 (both layers, deployments and rows, SVIs, OSPF interface settings): see AGENTS.md "Interface addresses and
layers".
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
    """{segment id: ids of the physical hosts of its components' instances}."""
    hosts: dict[str, set[str]] = {}
    for component in components:
        segment_id = (component.get("network_segment") or {}).get("id")
        for instance in component.get("instances") or []:
            host = physical_host(instance) if isinstance(instance, dict) else None
            if segment_id and host and host.get("id"):
                hosts.setdefault(str(segment_id), set()).add(str(host["id"]))
    return hosts


def _faces_a_host(port: dict[str, Any], hosts: set[str]) -> bool:
    """Whether the port's cable ends on an uplink or LAG NIC of one of ``hosts``."""
    return any(
        far.get("id") != port.get("id")
        and (far.get("device") or {}).get("id") in hosts
        and far.get("role") in ENDPOINT_NIC_ROLES
        for far in (port.get("cable") or {}).get("endpoints") or []
        if isinstance(far, dict)
    )


def is_derived(port: dict[str, Any], hosts: set[str], terminated: bool) -> bool:
    """Whether the reconcile derives a row member (port or LAG facing a host, or a terminated firewall port)."""
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
    """R3: the ports ``owner`` lists that are no physical port or LAG (``allow_svi``: a row's SVI is allowed)."""
    return [
        f"{owner} lists {port.get('typename')} '{(port.get('device') or {}).get('name')}' {port.get('name')} "
        "(rule R3: only a physical port or a LAG carries a segment)."
        for port in ports
        if isinstance(port, dict)
        and port.get("typename") not in SWITCHED_PORT_KINDS
        and not (allow_svi and is_svi(port))
    ]


def unrequested_members(row: dict[str, Any], hosts: dict[str, set[str]]) -> list[str]:
    """R5: row members neither assigned to the row's deployment nor derived."""
    deployment = row.get("segment_deployment") or {}
    segment = deployment.get("segment") or {}
    requested = {str(port.get("id")) for port in deployment.get("capabilities") or [] if isinstance(port, dict)}
    segment_hosts = hosts.get(str(segment.get("id")), set())
    terminated = bool(segment.get("firewall_gateway"))
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
    """Rules R1 to R10 of the module docstring."""

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
