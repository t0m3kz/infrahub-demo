"""Unit tests for CheckHypervisorEnvironment, fed canned hypervisor_environment query responses."""

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


def _vm(name: str, host: str | None, deployment: str | None, environment: str | None) -> dict[str, Any]:
    """One DcimVirtualDevice edge in the raw query shape."""
    deployment_node: dict[str, Any] | None = None
    if deployment:
        deployment_node = {"id": deployment, "__typename": "TopologyCustomerDC", "name": _v(deployment)}
        if environment is not None:
            deployment_node["environment"] = _v(environment)
    return {
        "node": {
            "id": name,
            "name": _v(name),
            "hosting_device": {"node": {"id": host, "name": _v(host)} if host else None},
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


def test_query_is_registered_and_selects_what_the_check_reads() -> None:
    """The check's query is registered in .infrahub.yml and selects host name and footprint environment."""
    config = yaml.safe_load((_ROOT / ".infrahub.yml").read_text())
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    checks = {check["name"]: check for check in config["check_definitions"]}

    assert checks["validate_hypervisor_environment"]["class_name"] == "CheckHypervisorEnvironment"
    query = (_ROOT / queries[CheckHypervisorEnvironment.query]).read_text()
    assert "hosting_device { node { id name { value } } }" in query
    assert "... on TopologyCustomer { environment { value } }" in query


def _all_demo_vms() -> list[dict[str, Any]]:
    """The 30_all VMs in the query shape, each deployment's environment read from its footprint."""
    environments = {
        name(footprint): footprint.value("environment")
        for kind in ("TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud")
        for footprint in distinct(declared(kind, _ALL_DEMO))
    }
    return [
        _vm(
            vm.item["name"],
            vm.item.get("hosting_device"),
            vm.item.get("deployment"),
            environments.get(vm.item.get("deployment") or ""),
        )
        for vm in declared("DcimVirtualDevice", _ALL_DEMO)
    ]


def test_the_scan_finds_the_all_demo_vms() -> None:
    """Every 30_all VM is placed on a host and deployed for a footprint with an environment."""
    vms = _all_demo_vms()
    assert len(vms) == 38
    assert all(vm["node"]["deployment"]["node"]["environment"]["value"] for vm in vms)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Owner decision pending: C005-P-DC12's four prod VMs run on dc12-pod1-server-1/2, the P027 shared "
        "non-prod hosts that also carry C008-D-DC12 and C010-D-DC12 (03_dc/dc12/05_servers.yml says prod never "
        "lands there). Remove this marker once the placement is fixed."
    ),
)
def test_all_demo_placement_passes_the_check() -> None:
    """The check run on the 30_all data: no hypervisor mixes prod and non-prod VMs."""
    assert _run(_all_demo_vms()) == []
