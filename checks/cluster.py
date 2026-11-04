"""Validate the clusters: an on-prem hypervisor cluster and managed Kubernetes (rules CL1 to CL7).

A VM has no cluster of its own: it stands on a host (``DcimVirtualDevice.hosting_device``) and the host is in a cluster
(``ManagedHypervisorCluster.hosts``), so the cluster is derived and never stated twice
(schemas/extensions/service/cluster.yml). What the schema cannot hold:

CL1  a server is a host of at most one cluster
CL2  the hosts of a cluster stand in the cluster's own deployment
CL3  a managed Kubernetes cluster's node subnets are subnets of its own virtual network, and that network is in its own account
CL4  the ranges of a cluster's SDN are valid networks that overlap neither each other nor an address block of the cluster's
     virtual network
CL5  anti-affinity: the instances of one application component that stand in one cluster of two or more hosts are not
     all on one host
CL6  the control plane of an on-prem Kubernetes cluster keeps its quorum: with three or more nodes no single host runs more
     than half of them
CL7  the gateway nodes of a hypervisor cluster's SDN (the NSX Edge VMs) stand on hosts of that cluster
"""

from __future__ import annotations

import ipaddress
from collections import Counter, defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

ON_PREM = "ManagedHypervisorCluster"
KUBERNETES = "CloudKubernetesCluster"
ON_PREM_KUBERNETES = "ManagedKubernetesCluster"
COMPONENT = "AppComponent"


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


def _network(value: Any) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """The network a text holds, None when it is empty or no network."""
    try:
        return ipaddress.ip_network(str(value).strip(), strict=False) if value else None
    except ValueError:
        return None


class CheckCluster(InfrahubCheck):
    """The clusters hold what their kind promises and the workloads sit where the cluster says (rules CL1 to CL7)."""

    query = "cluster"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        on_prem = _dicts(cleaned.get(ON_PREM))
        self._validate_hosts(on_prem)
        for cluster in on_prem:
            self._validate_sdn(cluster, ON_PREM, [])
            self._validate_gateway_nodes(cluster)
        for cluster in _dicts(cleaned.get(KUBERNETES)):
            self._validate_kubernetes(cluster)
        for cluster in _dicts(cleaned.get(ON_PREM_KUBERNETES)):
            self._validate_sdn(cluster, ON_PREM_KUBERNETES, [])
            self._validate_quorum(cluster)
        self._validate_anti_affinity(_dicts(cleaned.get(COMPONENT)), on_prem)

    def _error(self, message: str, node: dict[str, Any], kind: str) -> None:
        self.log_error(message=message, object_id=node.get("id"), object_type=kind)

    def _validate_hosts(self, clusters: list[dict[str, Any]]) -> None:
        """CL1 (one cluster per server) and CL2."""
        seen: dict[str, str] = {}
        for cluster in clusters:
            for host in _dicts(cluster.get("hosts")):
                host_id = str(host.get("id"))
                if host_id in seen and seen[host_id] != _name(cluster):
                    self._error(
                        f"CL1: server '{_name(host)}' is a host of both '{seen[host_id]}' and '{_name(cluster)}': "
                        "a host belongs to one cluster.",
                        host,
                        "DcimPhysicalDevice",
                    )
                seen.setdefault(host_id, _name(cluster))
                where, home = _ref(host.get("deployment")), _ref(cluster.get("deployment"))
                if where and home and where != home:
                    self._error(
                        f"CL2: host '{_name(host)}' of cluster '{_name(cluster)}' stands in "
                        f"'{_name(host.get('deployment'))}' but the cluster is in '{_name(cluster.get('deployment'))}'.",
                        cluster,
                        ON_PREM,
                    )

    def _validate_kubernetes(self, cluster: dict[str, Any]) -> None:
        """CL3 and CL4."""
        network = cluster.get("virtual_network")
        network = network if isinstance(network, dict) else {}
        if (
            _ref(cluster.get("account"))
            and _ref(network.get("account"))
            and _ref(cluster["account"]) != _ref(network["account"])
        ):
            self._error(
                f"CL3: cluster '{_name(cluster)}' is in account '{_name(cluster.get('account'))}' but its virtual "
                f"network '{_name(network)}' in '{_name(network.get('account'))}'.",
                cluster,
                KUBERNETES,
            )
        for segment in _dicts(cluster.get("node_segments")):
            if _ref(segment.get("virtual_network")) != _ref(network):
                self._error(
                    f"CL3: node subnet '{_name(segment)}' of cluster '{_name(cluster)}' is in virtual network "
                    f"'{_name(segment.get('virtual_network'))}', not in the cluster's '{_name(network)}'.",
                    cluster,
                    KUBERNETES,
                )
        self._validate_sdn(cluster, KUBERNETES, [_network(b.get("prefix")) for b in _dicts(network.get("cidr_blocks"))])

    def _validate_sdn(self, cluster: dict[str, Any], kind: str, blocks: list[Any]) -> None:
        """CL4: the SDN's ranges are networks and overlap neither each other nor the blocks of the cluster's network."""
        sdn = cluster.get("sdn")
        if not isinstance(sdn, dict):
            return
        ranges = {label: _network(sdn.get(label)) for label in ("overlay_cidr", "service_cidr")}
        for label, net in ranges.items():
            if sdn.get(label) and net is None:
                self._error(f"CL4: {label} '{sdn.get(label)}' of SDN '{_name(sdn)}' is no network.", cluster, kind)
            elif net is not None and any(
                block is not None and net.version == block.version and net.overlaps(block) for block in blocks
            ):
                self._error(
                    f"CL4: {label} {net} of SDN '{_name(sdn)}' overlaps an address block of the virtual network of "
                    f"cluster '{_name(cluster)}'.",
                    cluster,
                    kind,
                )
        overlay, service = ranges["overlay_cidr"], ranges["service_cidr"]
        if (
            overlay is not None
            and service is not None
            and overlay.version == service.version
            and overlay.overlaps(service)
        ):
            self._error(
                f"CL4: overlay_cidr {overlay} and service_cidr {service} of SDN '{_name(sdn)}' overlap.", cluster, kind
            )

    def _validate_gateway_nodes(self, cluster: dict[str, Any]) -> None:
        """CL7: an SDN gateway node is a VM on a host of the cluster that owns the SDN."""
        sdn = cluster.get("sdn")
        if not isinstance(sdn, dict):
            return
        hosts = {_ref(host) for host in _dicts(cluster.get("hosts"))}
        for node in _dicts(sdn.get("gateway_nodes")):
            host = _ref(node.get("hosting_device"))
            if host not in hosts:
                self._error(
                    f"CL7: gateway node '{_name(node)}' of SDN '{_name(sdn)}' stands on "
                    f"'{_name(node.get('hosting_device'))}', which is no host of cluster '{_name(cluster)}'.",
                    cluster,
                    ON_PREM,
                )

    def _validate_quorum(self, cluster: dict[str, Any]) -> None:
        """CL6: with three or more control plane nodes no host runs more than half of them."""
        nodes = _dicts(cluster.get("control_plane_nodes"))
        if len(nodes) < 3:
            return
        per_host = Counter(_ref(node.get("hosting_device")) or str(node.get("id")) for node in nodes)
        host, count = per_host.most_common(1)[0]
        if count * 2 > len(nodes):
            self._error(
                f"CL6: {count} of the {len(nodes)} control plane nodes of cluster '{_name(cluster)}' stand on one host "
                f"({host}): losing it loses the quorum.",
                cluster,
                ON_PREM_KUBERNETES,
            )

    def _validate_anti_affinity(self, components: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> None:
        """CL5: the cluster of an instance is the cluster of its host."""
        cluster_of: dict[str, str] = {}
        size: dict[str, int] = {}
        for cluster in clusters:
            hosts = _dicts(cluster.get("hosts"))
            size[str(cluster["id"])] = len(hosts)
            cluster_of.update({str(host["id"]): str(cluster["id"]) for host in hosts})
        for component in components:
            by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for instance in _dicts(component.get("instances")):
                host = _ref(instance.get("hosting_device"))
                if host and host in cluster_of:
                    by_cluster[cluster_of[host]].append(instance)
            for cluster, instances in by_cluster.items():
                hosts = Counter(_ref(i.get("hosting_device")) for i in instances)
                if len(instances) >= 2 and len(hosts) == 1 and size.get(cluster, 0) >= 2:
                    names = ", ".join(sorted(_name(i) for i in instances))
                    self._error(
                        f"CL5: all {len(instances)} instances of component '{_name(component)}' ({names}) stand on host "
                        f"'{_name(instances[0].get('hosting_device'))}' of a cluster with {size[cluster]} hosts: "
                        "one host failure takes the component down.",
                        component,
                        COMPONENT,
                    )
