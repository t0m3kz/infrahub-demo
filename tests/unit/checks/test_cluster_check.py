"""checks/customers/cluster.py: the rules CL1 to CL7 on canned query data, and the registration of the check."""

from __future__ import annotations

from typing import Any, cast

import pytest
import yaml

from checks.customers.cluster import CheckCluster
from tests.integration.demo_data import ROOT


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _ref(**fields: Any) -> dict[str, Any]:
    return {"node": fields}


def _host(name: str, deployment: str = "pod-1") -> dict[str, Any]:
    return {"id": f"h-{name}", "name": _v(name), "deployment": _ref(id=f"d-{deployment}", display_label=deployment)}


def _instance(name: str, host: str) -> dict[str, Any]:
    """A VM instance of a component: it stands on a host (``hosting_device``), which is all the cluster it has."""
    return {
        "id": f"vm-{name}",
        "name": _v(name),
        "display_label": name,
        "hosting_device": _ref(id=f"h-{host}", name=_v(host)),
    }


def _cluster(name: str = "cl1", hosts: tuple[str, ...] = ("esx1", "esx2"), dep: str = "pod-1") -> dict[str, Any]:
    return {
        "id": f"c-{name}",
        "name": _v(name),
        "deployment": _ref(id=f"d-{dep}", display_label=dep),
        "hosts": _edges(*(_host(h) for h in hosts)),
    }


def _sdn(overlay: str | None = None, service: str | None = None, gateways: tuple[dict, ...] = ()) -> dict[str, Any]:
    """The SDN of a cluster with its ranges and the VMs that carry its gateways (each with the host it stands on)."""
    return {
        "id": "sdn1",
        "name": _v("sdn1"),
        "overlay_cidr": _v(overlay),
        "service_cidr": _v(service),
        "gateway_nodes": _edges(*gateways),
    }


def _vm(name: str, host: str) -> dict[str, Any]:
    return {"id": f"vm-{name}", "name": _v(name), "hosting_device": _ref(id=f"h-{host}", name=_v(host))}


def _k8s(**over: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "k1",
        "name": _v("eks1"),
        "sdn": _sdn(service="172.20.0.0/16"),
        "account": _ref(id="a1", name=_v("acct")),
        "virtual_network": _ref(
            id="n1",
            name=_v("vpc1"),
            account=_ref(id="a1", name=_v("acct")),
            cidr_blocks=_edges({"id": "b1", "prefix": _v("10.200.0.0/16")}),
        ),
        "node_segments": _edges({"id": "s1", "name": _v("subnet"), "virtual_network": _ref(id="n1", name=_v("vpc1"))}),
    }
    node.update(over)
    return node


def _component(name: str, *instances: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"comp-{name}", "name": _v(name), "instances": _edges(*instances)}


def _errors(**kinds: list[dict[str, Any]]) -> list[str]:
    check = cast(Any, CheckCluster.__new__(CheckCluster))
    out: list[str] = []
    check.log_error = lambda message, **_kw: out.append(message)
    check.validate({kind: _edges(*nodes) for kind, nodes in kinds.items()})
    return out


def test_a_consistent_set_of_clusters_passes() -> None:
    """Two instances of one component on the two hosts of a cluster, a Kubernetes cluster on its own network."""
    spread = (_instance("a", "esx1"), _instance("b", "esx2"))
    assert (
        _errors(
            ManagedHypervisorCluster=[_cluster()],
            CloudKubernetesCluster=[_k8s(sdn=_sdn("100.64.0.0/16", "172.20.0.0/16"))],
            AppComponent=[_component("web", *spread)],
        )
        == []
    )


def test_a_server_in_two_clusters_is_cl1() -> None:
    errors = _errors(ManagedHypervisorCluster=[_cluster("vc1", ("esx1", "esx2")), _cluster("vc2", ("esx2", "esx3"))])
    assert [e[:3] for e in errors] == ["CL1"] and "esx2" in errors[0]


def test_a_host_outside_the_cluster_deployment_is_cl2() -> None:
    cluster = _cluster()
    cluster["hosts"] = _edges(_host("esx1"), _host("esx2", deployment="pod-2"))
    (error,) = _errors(ManagedHypervisorCluster=[cluster])
    assert error.startswith("CL2") and "esx2" in error


def test_node_subnet_of_another_network_and_a_network_of_another_account_are_cl3() -> None:
    other = _ref(id="n2", name=_v("vpc2"))
    foreign = _ref(id="n1", name=_v("vpc1"), account=_ref(id="a2", name=_v("other")), cidr_blocks=_edges())
    errors = _errors(
        CloudKubernetesCluster=[
            _k8s(node_segments=_edges({"id": "s2", "name": _v("alien"), "virtual_network": other})),
            _k8s(id="k2", name=_v("eks2"), virtual_network=foreign),
        ]
    )
    assert sorted(e[:3] for e in errors) == ["CL3", "CL3"]


@pytest.mark.parametrize(
    ("sdn", "needle"),
    [
        (_sdn("10.200.8.0/21", "172.20.0.0/16"), "overlaps an address block"),
        (_sdn(None, "not-a-network"), "is no network"),
        (_sdn("172.20.4.0/24", "172.20.0.0/16"), "overlap"),
    ],
)
def test_the_ranges_of_an_sdn_must_be_networks_that_overlap_nothing_cl4(sdn: dict[str, Any], needle: str) -> None:
    (error,) = _errors(CloudKubernetesCluster=[_k8s(sdn=sdn)])
    assert error.startswith("CL4") and needle in error


def test_the_sdn_of_a_hypervisor_or_an_on_prem_cluster_is_judged_too_cl4() -> None:
    bad = _sdn("10.0.0.0/16", "10.0.1.0/24")
    cluster = {**_cluster(), "sdn": bad}
    k8s = {"id": "o1", "name": _v("rke2"), "sdn": bad, "control_plane_nodes": _edges()}
    errors = _errors(ManagedHypervisorCluster=[cluster], ManagedKubernetesCluster=[k8s])
    assert [e[:3] for e in errors] == ["CL4", "CL4"]


def test_the_gateway_nodes_of_an_sdn_must_stand_on_hosts_of_its_cluster_cl7() -> None:
    inside = {**_cluster(), "sdn": _sdn(gateways=(_vm("edge1", "esx1"), _vm("edge2", "esx2")))}
    assert _errors(ManagedHypervisorCluster=[inside]) == []
    outside = {**_cluster(), "sdn": _sdn(gateways=(_vm("edge1", "esx1"), _vm("edge2", "bare9")))}
    (error,) = _errors(ManagedHypervisorCluster=[outside])
    assert error.startswith("CL7") and "edge2" in error and "bare9" in error


def _control_plane(*hosts: str) -> dict[str, Any]:
    return {
        "id": "o1",
        "name": _v("rke2"),
        "control_plane_nodes": _edges(*(_vm(f"cp{i}", h) for i, h in enumerate(hosts, 1))),
    }


def test_a_control_plane_of_three_needs_three_hosts_cl6() -> None:
    assert _errors(ManagedKubernetesCluster=[_control_plane("esx1", "esx2", "esx3")]) == []
    (error,) = _errors(ManagedKubernetesCluster=[_control_plane("esx1", "esx1", "esx2")])
    assert error.startswith("CL6") and "esx1" in error and "quorum" in error
    assert _errors(ManagedKubernetesCluster=[_control_plane("esx1", "esx1")]) == []  # fewer than three: no quorum rule


def test_all_instances_on_one_host_of_a_two_host_cluster_is_cl5_but_a_spread_a_single_host_or_a_stranger_is_not() -> (
    None
):
    """The cluster of an instance is its host's: nothing on the instance names it."""
    same = (_instance("a", "esx1"), _instance("b", "esx1"))
    (error,) = _errors(ManagedHypervisorCluster=[_cluster()], AppComponent=[_component("web", *same)])
    assert error.startswith("CL5") and "esx1" in error
    spread = (_instance("a", "esx1"), _instance("b", "esx2"))
    assert _errors(ManagedHypervisorCluster=[_cluster()], AppComponent=[_component("web", *spread)]) == []
    assert _errors(ManagedHypervisorCluster=[_cluster(hosts=("esx1",))], AppComponent=[_component("web", *same)]) == []
    assert (
        _errors(
            ManagedHypervisorCluster=[_cluster()],
            AppComponent=[_component("web", _instance("a", "bare1"), _instance("b", "bare1"))],
        )
        == []
    )


def test_the_check_and_its_query_are_registered() -> None:
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert checks["validate_cluster"]["class_name"] == "CheckCluster"
    assert CheckCluster.query == "cluster"
    assert (ROOT / queries["cluster"]).is_file()
