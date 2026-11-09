"""checks/virtual_switch.py: the rules VS1 to VS4 on canned query data, and the registration of the check."""

from __future__ import annotations

from typing import Any, cast

import yaml

from checks.virtual_switch import CheckVirtualSwitch
from tests.integration.demo_data import ROOT


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _ref(**fields: Any) -> dict[str, Any]:
    return {"node": fields}


def _cluster(*hosts: str) -> dict[str, Any]:
    return {"id": "c1", "name": _v("cl1"), "hosts": _edges(*({"id": f"h-{h}", "name": _v(h)} for h in hosts))}


def _uplink(host: str, port: str = "bond0", *, dc: str = "dc1") -> dict[str, Any]:
    """A host port of ``host``, which stands in a pod of ``dc`` (a server port carries no segment membership)."""
    pod = {
        "__typename": "TopologyPod",
        "id": f"pod-{dc}",
        "name": _v(f"{dc}-pod"),
        "parent": _ref(__typename="TopologyDataCenter", id=dc, name=_v(dc.upper())),
    }
    return {
        "id": f"u-{host}-{port}",
        "display_label": port,
        "name": _v(port),
        "device": _ref(id=f"h-{host}", name=_v(host), deployment={"node": pod}),
    }


def _nic(vm: str, host: str) -> dict[str, Any]:
    device = {"id": f"vm-{vm}", "name": _v(vm), "hosting_device": _ref(id=f"h-{host}", name=_v(host))}
    return {"id": f"n-{vm}", "name": _v("eth0"), "device": {"node": device}}


def _group(
    name: str = "web",
    vlan: int | None = 100,
    segment: str | None = "web-p",
    *nics: dict[str, Any],
    realized_in: tuple[str, ...] = ("dc1",),
) -> dict:
    """A port group of ``segment``, which has a deployment in each DC of ``realized_in``."""
    deployments = _edges(*({"deployment": _ref(id=dc)} for dc in realized_in))
    return {
        "id": f"g-{name}",
        "name": _v(name),
        "vlan_id": _v(vlan),
        "segment": _ref(id=f"s-{segment}", name=_v(segment), segment_deployments=deployments)
        if segment
        else {"node": None},
        "nics": _edges(*nics),
    }


def _switch(uplinks: tuple[dict, ...], groups: tuple[dict, ...], name: str = "sw1") -> dict[str, Any]:
    return {
        "id": f"w-{name}",
        "name": _v(name),
        "cluster": _ref(id="c1", name=_v("cl1")),
        "uplinks": _edges(*uplinks),
        "port_groups": _edges(*groups),
    }


def _errors(*switches: dict[str, Any], hosts: tuple[str, ...] = ("esx1", "esx2")) -> list[str]:
    check = cast(Any, CheckVirtualSwitch.__new__(CheckVirtualSwitch))
    out: list[str] = []
    check.log_error = lambda message, **_kw: out.append(message)
    check.validate({"ManagedHypervisorCluster": _edges(_cluster(*hosts)), "ManagedVirtualSwitch": _edges(*switches)})
    return out


def _good() -> dict[str, Any]:
    uplinks = (_uplink("esx1", "bond0"), _uplink("esx2", "eno1"))
    return _switch(uplinks, (_group("web", 100, "web-p", _nic("a", "esx1"), _nic("b", "esx2")),))


def test_a_consistent_switch_passes() -> None:
    assert _errors(_good()) == []


def test_an_uplink_of_a_server_outside_the_cluster_and_a_host_without_one_are_vs1() -> None:
    switch = _switch((_uplink("esx1", "bond0"), _uplink("alien", "eno1")), (_group(),))
    errors = _errors(switch)
    assert [e[:3] for e in errors] == ["VS1", "VS1"]
    assert any("alien" in e for e in errors) and any("esx2" in e and "no uplink" in e for e in errors)


def test_a_vlan_used_by_two_port_groups_is_vs2() -> None:
    switch = _switch(
        (_uplink("esx1", "bond0"), _uplink("esx2", "eno1")),
        (_group("web", 100, "web-p"), _group("db", 100, "db-p")),
    )
    (error,) = _errors(switch)
    assert error.startswith("VS2") and "VLAN 100" in error


def test_an_uplink_whose_host_stands_where_the_segment_of_a_port_group_is_not_realized_is_vs3() -> None:
    """The uplinks trunk the port groups' VLANs: the segment must have a deployment where each uplink's host stands."""
    switch = _switch((_uplink("esx1", "bond0"), _uplink("esx2", "eno1", dc="dc2")), (_group(),))
    (error,) = _errors(switch)
    assert error.startswith("VS3") and "esx2" in error and "web-p" in error and "DC2" in error


def test_a_segment_realized_where_every_host_stands_is_no_vs3() -> None:
    """Hosts in two DCs and a segment realized in both: nothing to report."""
    switch = _switch(
        (_uplink("esx1", "bond0"), _uplink("esx2", "eno1", dc="dc2")), (_group(realized_in=("dc1", "dc2")),)
    )
    assert _errors(switch) == []


def test_a_nic_on_a_vm_of_another_cluster_and_a_nic_in_two_groups_are_vs4() -> None:
    uplinks = (_uplink("esx1", "bond0"), _uplink("esx2", "eno1"))
    stranger = _switch(uplinks, (_group("web", 100, "web-p", _nic("a", "bare9")),))
    (error,) = _errors(stranger)
    assert error.startswith("VS4") and "bare9" in error
    twice = _switch(
        uplinks, (_group("web", 100, "web-p", _nic("a", "esx1")), _group("db", 200, "db-p", _nic("a", "esx1")))
    )
    (error,) = _errors(twice)
    assert error.startswith("VS4") and "2 port groups" in error


def test_the_check_and_its_query_are_registered() -> None:
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert checks["validate_virtual_switch"]["class_name"] == "CheckVirtualSwitch"
    assert CheckVirtualSwitch.query == "virtual_switch"
    assert (ROOT / queries["virtual_switch"]).is_file()
