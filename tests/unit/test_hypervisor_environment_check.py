"""Unit tests for CheckHypervisorEnvironment, fed canned hypervisor_environment query responses.

Two rules: prod and non-prod VMs never share a hypervisor, and a VM runs in the DC (or metro) of its footprint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from checks.hypervisor_environment import CheckHypervisorEnvironment
from tests.integration.demo_data import declared, distinct, name

_ROOT = Path(__file__).parents[2]
_ALL_DEMO = _ROOT / "data" / "demos" / "30_all"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _vm(
    name: str,
    host: str | None,
    deployment: str | None,
    environment: str | None,
    *,
    kind: str = "TopologyCustomerDC",
    footprint_dc: str | None = "DC1",
    host_dc: str | None = "DC1",
    host_kind: str = "TopologyPod",
) -> dict[str, Any]:
    """One DcimVirtualDevice edge in the raw query shape.

    ``footprint_dc`` is the footprint's parent (DC or metro), ``host_dc`` the parent of the host's deployment
    (a pod's DC, a zone's metro); None leaves either out (no parent selected / host not in a deployment).
    """
    deployment_node: dict[str, Any] | None = None
    if deployment:
        deployment_node = {"id": deployment, "__typename": kind, "name": _v(deployment)}
        if environment is not None:
            deployment_node["environment"] = _v(environment)
        if footprint_dc:
            deployment_node["parent"] = {"node": {"id": footprint_dc, "name": _v(footprint_dc)}}
    host_deployment: dict[str, Any] | None = None
    if host_dc:
        host_deployment = {
            "id": f"{host_dc}-pod",
            "__typename": host_kind,
            "name": _v(f"{host_dc}-pod"),
            "parent": {"node": {"id": host_dc, "name": _v(host_dc)}},
        }
    return {
        "node": {
            "id": name,
            "name": _v(name),
            "hosting_device": {
                "node": {"id": host, "name": _v(host), "deployment": {"node": host_deployment}} if host else None
            },
            "deployment": {"node": deployment_node},
        }
    }


def _run(vms: list[dict[str, Any]]) -> list[str]:
    """Validate the VMs and return the logged errors."""
    check = cast(Any, CheckHypervisorEnvironment.__new__(CheckHypervisorEnvironment))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate({"DcimVirtualDevice": {"edges": vms}})
    return errors


class TestCheckHypervisorEnvironment:
    """One environment class per hypervisor: prod and non-prod never share a host."""

    def test_one_class_per_host_passes(self) -> None:
        """Prod VMs of two customers on one host and dev + staging VMs on another are each one class."""
        errors = _run(
            [
                _vm("a-1", "host-1", "C001-P-DC1", "p"),
                _vm("b-1", "host-1", "C002-P-DC1", "p"),
                _vm("c-1", "host-2", "C003-D-DC1", "d"),
                _vm("d-1", "host-2", "C004-S-DC1", "s"),
            ]
        )

        assert errors == []

    def test_prod_and_non_prod_on_one_host_is_an_error(self) -> None:
        """A dev VM next to a prod VM is reported once per host, naming both sides."""
        errors = _run(
            [
                _vm("prod-1", "host-1", "C001-P-DC1", "p"),
                _vm("dev-1", "host-1", "C002-D-DC1", "d"),
                _vm("dev-2", "host-1", "C002-D-DC1", "d"),
                _vm("prod-2", "host-2", "C001-P-DC1", "p"),
            ]
        )

        assert len(errors) == 1
        assert "host-1" in errors[0]
        assert "prod: ['prod-1 (C001-P-DC1)']" in errors[0]
        assert "non_prod: ['dev-1 (C002-D-DC1)', 'dev-2 (C002-D-DC1)']" in errors[0]

    def test_office_vm_counts_as_prod(self) -> None:
        """An office has no environment: its VM is PROD, so it cannot share a host with a dev VM."""
        errors = _run(
            [
                _vm("office-1", "host-1", "C005-OFFICE", None, kind="TopologyCustomerOffice", footprint_dc=None),
                _vm("dev-1", "host-1", "C002-D-DC1", "d"),
            ]
        )

        assert len(errors) == 1
        assert "prod: ['office-1 (C005-OFFICE)']" in errors[0]

    def test_office_vm_next_to_prod_vm_passes(self) -> None:
        """Office and `p` footprint VMs are one class."""
        errors = _run(
            [
                _vm("office-1", "host-1", "C005-OFFICE", None, kind="TopologyCustomerOffice", footprint_dc=None),
                _vm("prod-1", "host-1", "C001-P-DC1", "p"),
            ]
        )

        assert errors == []

    @pytest.mark.parametrize(
        ("host", "deployment", "environment"),
        [
            (None, "C002-D-DC1", "d"),  # an unplaced VM
            ("host-1", None, None),  # no deployment
            ("host-1", "DC1", None),  # a deployment that is no customer footprint
        ],
    )
    def test_vm_without_a_class_is_skipped(
        self, host: str | None, deployment: str | None, environment: str | None
    ) -> None:
        """A VM with no host or no footprint environment has nothing to compare."""
        errors = _run([_vm("prod-1", "host-1", "C001-P-DC1", "p"), _vm("other", host, deployment, environment)])

        assert errors == []

    def test_empty_response_passes(self) -> None:
        """No VMs, no errors."""
        assert _run([]) == []


class TestVmRunsInItsFootprintsDc:
    """A VM's host sits in the DC (or metro) its footprint is a child of."""

    def test_host_in_the_footprints_dc_passes(self) -> None:
        """A pod of DC1 under a DC1 footprint is the same DC."""
        assert _run([_vm("vm-1", "host-1", "C001-P-DC1", "p", footprint_dc="DC1", host_dc="DC1")]) == []

    def test_host_in_another_dc_is_an_error(self) -> None:
        """A VM of a DC10 footprint on a DC12 host is served by another fabric."""
        errors = _run([_vm("vm-1", "host-1", "C001-P-DC10", "p", footprint_dc="DC10", host_dc="DC12")])

        assert len(errors) == 1
        assert "vm-1" in errors[0] and "C001-P-DC10" in errors[0] and "host-1" in errors[0]
        assert "'DC10'" in errors[0] and "'DC12'" in errors[0]

    def test_host_deployed_directly_in_the_dc_passes(self) -> None:
        """A host whose deployment is the DC itself (not a pod) matches by its own id."""
        vm = _vm("vm-1", "host-1", "C001-P-DC1", "p", footprint_dc="DC1", host_dc=None)
        vm["node"]["hosting_device"]["node"]["deployment"] = {
            "node": {"id": "DC1", "__typename": "TopologyDataCenter", "name": _v("DC1")}
        }

        assert _run([vm]) == []

    def test_colocation_zone_host_matches_its_metro(self) -> None:
        """A cage host in zone NY1 serves the footprint of metro NY; metro VA is another."""
        ok = _vm(
            "vm-1",
            "h",
            "C011-P-NY",
            "p",
            kind="TopologyCustomerColocation",
            footprint_dc="NY",
            host_dc="NY",
            host_kind="TopologyColocationZone",
        )
        bad = _vm(
            "vm-2",
            "h2",
            "C011-P-NY",
            "p",
            kind="TopologyCustomerColocation",
            footprint_dc="NY",
            host_dc="VA",
            host_kind="TopologyColocationZone",
        )

        assert _run([ok]) == []
        assert len(_run([bad])) == 1

    def test_host_not_in_a_deployment_is_skipped(self) -> None:
        """add_endpoint sets the host's deployment when it cables it: until then there is no DC to compare."""
        assert _run([_vm("vm-1", "host-1", "C001-P-DC10", "p", footprint_dc="DC10", host_dc=None)]) == []

    def test_office_vm_is_not_compared(self) -> None:
        """An office hosts its own VMs: it has no DC parent to compare."""
        vm = _vm("o-1", "host-1", "C005-OFFICE", None, kind="TopologyCustomerOffice", footprint_dc=None, host_dc="DC12")

        assert _run([vm]) == []

    def test_dc_error_does_not_hide_the_environment_rule(self) -> None:
        """Both rules report on the same data: a wrong-DC dev VM next to a prod VM gives two errors."""
        errors = _run(
            [
                _vm("prod-1", "host-1", "C001-P-DC10", "p", footprint_dc="DC10", host_dc="DC10"),
                _vm("dev-1", "host-1", "C002-D-DC10", "d", footprint_dc="DC10", host_dc="DC10"),
                _vm("dev-2", "host-1", "C002-D-DC12", "d", footprint_dc="DC12", host_dc="DC10"),
            ]
        )

        assert len(errors) == 2


def test_query_is_registered_and_selects_what_the_check_reads() -> None:
    """The check's query is registered in .infrahub.yml and selects host, host DC and footprint environment/parent."""
    config = yaml.safe_load((_ROOT / ".infrahub.yml").read_text())
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    checks = {check["name"]: check for check in config["check_definitions"]}

    assert checks["validate_hypervisor_environment"]["class_name"] == "CheckHypervisorEnvironment"
    query = (_ROOT / queries[CheckHypervisorEnvironment.query]).read_text()
    assert "... on TopologyEnvironmentScoped { environment { value } }" in query
    assert "TopologyCustomerOffice" not in query  # an office has no environment to select
    assert "... on TopologyCustomer {" not in query
    assert "... on TopologyPod { parent { node { id name { value } } } }" in query
    assert "... on TopologyColocationZone { parent { node { id name { value } } } }" in query
    assert "... on TopologyCustomerDC { parent { node { id name { value } } } }" in query


def _all_demo_vms() -> list[dict[str, Any]]:
    """The 30_all VMs in the query shape: environment and parent from the footprint, host DC from its declaration.

    A DC host is declared under data/demos/30_all/03_dc/<dc>/ (add_endpoint puts it in a pod of that DC); a
    colocation cage host declares its zone, whose parent is the metro it is nested under.
    """
    footprints = {
        name(footprint): footprint.item
        for kind in ("TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud")
        for footprint in distinct(declared(kind, _ALL_DEMO))
    }
    metro_of_zone = {
        str(zone.item["name"]): str(zone.enclosing.item["name"])
        for zone in declared("TopologyColocationZone", _ALL_DEMO)
        if zone.enclosing is not None
    }
    host_dc: dict[str, str] = {}
    for host in declared("DcimPhysicalDevice", _ALL_DEMO):
        if "03_dc" in host.path.parts:
            host_dc[host.item["name"]] = host.path.parent.name.upper()
        elif host.item.get("deployment") in metro_of_zone:
            host_dc[host.item["name"]] = metro_of_zone[host.item["deployment"]]
    vms = []
    for vm in declared("DcimVirtualDevice", _ALL_DEMO):
        footprint = footprints.get(vm.item.get("deployment") or "", {})
        host = vm.item.get("hosting_device")
        vms.append(
            _vm(
                vm.item["name"],
                host,
                vm.item.get("deployment"),
                footprint.get("environment"),
                kind="TopologyCustomerColocation"
                if str(footprint.get("parent")) in metro_of_zone.values()
                else "TopologyCustomerDC",
                footprint_dc=str(footprint["parent"]).upper() if footprint.get("parent") else None,
                host_dc=host_dc.get(host),
            )
        )
    return vms


def test_the_scan_finds_the_all_demo_vms() -> None:
    """Every 30_all VM is placed on a host in a DC and deployed for a footprint with an environment."""
    vms = _all_demo_vms()
    assert len(vms) == 38
    assert all(vm["node"]["deployment"]["node"]["environment"]["value"] for vm in vms)
    assert all(vm["node"]["hosting_device"]["node"]["deployment"]["node"] for vm in vms)


def test_all_demo_placement_passes_the_check() -> None:
    """The check run on the 30_all data: no hypervisor mixes prod and non-prod VMs, every VM runs in its footprint's DC."""
    assert _run(_all_demo_vms()) == []
