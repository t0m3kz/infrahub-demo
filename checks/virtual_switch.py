"""Validate the virtual switches of the hypervisor clusters (rules VS1 to VS4).

A ``ManagedVirtualSwitch`` is the L2 element between the VM NICs and the host ports of a cluster; its port groups are the
VLANs of it and the fabric segments those are (schemas/extensions/service/virtual_switch.yml). What the schema cannot hold:

VS1  the uplinks are ports (or bonds) of the hosts of the switch's own cluster, and every host of the cluster has one
VS2  a VLAN id is used by one port group of a switch only
VS3  the segment of every port group is realized (a segment deployment) in the hosting deployment of every uplink's
     host: the uplinks trunk the port groups' VLANs (``vlan_id``; a server port carries no segment membership, its
     allowed VLAN list is the port groups), so a VM in the port group reaches the fabric whichever host it runs on
VS4  a VM NIC is in one port group, and the VM stands on a host of the switch's cluster
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.segment_addressing import hosting_deployment

CLUSTER = "ManagedHypervisorCluster"
SWITCH = "ManagedVirtualSwitch"


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference (clean_data collapses an object with only an ``id`` to the bare id)."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _name(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("name") or node.get("display_label") or node.get("id") or "<unnamed>")
    return str(node or "<unnamed>")


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [value for value in values or [] if isinstance(value, dict)]


class CheckVirtualSwitch(InfrahubCheck):
    """The uplinks, the VLANs and the NICs of a virtual switch agree with its cluster and the fabric (rules VS1 to VS4)."""

    query = "virtual_switch"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        hosts = {
            str(c["id"]): {str(h["id"]) for h in _dicts(c.get("hosts"))}
            for c in _dicts(cleaned.get(CLUSTER))
            if c.get("id")
        }
        nic_groups: dict[str, list[str]] = defaultdict(list)
        for switch in _dicts(cleaned.get(SWITCH)):
            cluster_hosts = hosts.get(_ref(switch.get("cluster")) or "", set())
            self._validate_uplinks(switch, cluster_hosts)
            self._validate_port_groups(switch, cluster_hosts, nic_groups)
        for nic, groups in nic_groups.items():
            if len(groups) > 1:
                self.log_error(
                    message=f"VS4: VM NIC '{nic}' is in {len(groups)} port groups ({', '.join(sorted(groups))})."
                )

    def _error(self, message: str, node: dict[str, Any], kind: str) -> None:
        self.log_error(message=message, object_id=node.get("id"), object_type=kind)

    def _validate_uplinks(self, switch: dict[str, Any], cluster_hosts: set[str]) -> None:
        """VS1 and VS3."""
        uplinks = _dicts(switch.get("uplinks"))
        have: set[str] = set()
        for uplink in uplinks:
            device = _ref(uplink.get("device"))
            if device:
                have.add(device)
            if device and cluster_hosts and device not in cluster_hosts:
                self._error(
                    f"VS1: uplink '{_name(uplink.get('device'))}:{_name(uplink)}' of switch '{_name(switch)}' is a port "
                    f"of '{_name(uplink.get('device'))}', which is no host of cluster '{_name(switch.get('cluster'))}'.",
                    switch,
                    SWITCH,
                )
        for host in sorted(cluster_hosts - have):
            self._error(
                f"VS1: host {host} of cluster '{_name(switch.get('cluster'))}' has no uplink of switch '{_name(switch)}'.",
                switch,
                SWITCH,
            )
        for group in _dicts(switch.get("port_groups")):
            segment = group.get("segment")
            if not isinstance(segment, dict) or not segment.get("id"):
                continue
            realized = {_ref(dep.get("deployment")) for dep in _dicts(segment.get("segment_deployments"))}
            for uplink in uplinks:
                hosting = hosting_deployment((uplink.get("device") or {}).get("deployment"))
                if hosting and hosting.get("id") and str(hosting["id"]) not in realized:
                    self._error(
                        f"VS3: uplink '{_name(uplink.get('device'))}:{_name(uplink)}' of switch '{_name(switch)}' trunks "
                        f"VLAN {group.get('vlan_id') or 'untagged'} of port group '{_name(group)}', but segment "
                        f"'{_name(segment)}' has no deployment in '{_name(hosting)}', where the host stands.",
                        switch,
                        SWITCH,
                    )

    def _validate_port_groups(
        self, switch: dict[str, Any], cluster_hosts: set[str], nic_groups: dict[str, list[str]]
    ) -> None:
        """VS2 and VS4."""
        vlans = Counter(g.get("vlan_id") for g in _dicts(switch.get("port_groups")) if g.get("vlan_id"))
        for vlan, count in vlans.items():
            if count > 1:
                self._error(
                    f"VS2: VLAN {vlan} is used by {count} port groups of switch '{_name(switch)}'.", switch, SWITCH
                )
        for group in _dicts(switch.get("port_groups")):
            for nic in _dicts(group.get("nics")):
                nic_groups[f"{_name(nic.get('device'))}:{_name(nic)}"].append(f"{_name(switch)}/{_name(group)}")
                device: dict[str, Any] = nic.get("device") or {}
                host = _ref(device.get("hosting_device"))
                if cluster_hosts and host not in cluster_hosts:
                    self._error(
                        f"VS4: VM NIC '{_name(device)}:{_name(nic)}' is in port group '{_name(group)}' of switch "
                        f"'{_name(switch)}' but its VM stands on '{_name(device.get('hosting_device'))}', which is no host "
                        f"of cluster '{_name(switch.get('cluster'))}'.",
                        switch,
                        SWITCH,
                    )
