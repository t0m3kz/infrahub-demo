"""Rule (e) of CheckVrfInstances over every device role and kind, and the S3 / S5 passing cases (shared MLAG rows)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from checks.vrf_instances import ERROR_RULES, WARNING_RULES, uncarried_instance_errors
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_instance, raw_mlag, raw_row, raw_svi
from utils.vrf_demand import PARTY_DEPLOYMENT_KINDS, PARTY_GROUPS, VRF_DEVICE_KINDS, VRF_ROLES

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _device_roles() -> list[str]:
    """The choices of the DcimDevice ``role`` dropdown (schemas/base/dcim.yml)."""
    document = yaml.safe_load((SCHEMAS / "base" / "dcim.yml").read_text())
    device = next(generic for generic in document["generics"] if generic["name"] == "Device")
    role = next(attribute for attribute in device["attributes"] if attribute["name"] == "role")
    return [choice["name"] for choice in role["choices"]]


def _device_kinds() -> list[str]:
    """Every concrete node kind that inherits ``DcimDevice``, from the schema files."""
    kinds = []
    for path in SCHEMAS.rglob("*.yml"):
        document = yaml.safe_load(path.read_text()) or {}
        for node in document.get("nodes") or []:
            if "DcimDevice" in (node.get("inherit_from") or []):
                kinds.append(f"{node['namespace']}{node['name']}")
    return sorted(kinds)


NON_CARRYING_ROLES = sorted(set(_device_roles()) - VRF_ROLES)
NON_CARRYING_KINDS = sorted(set(_device_kinds()) - VRF_DEVICE_KINDS)


def _with_instance(**kwargs: Any) -> dict[str, Any]:
    """A device carrying one PROD instance."""
    return raw_device("d1", "dev-1", capabilities=[raw_instance("v1", "dev-1", "PROD")], **kwargs)


def test_the_schema_scan_finds_the_roles_and_kinds() -> None:
    """The parametrisation below is not empty: carrying roles and kinds are all in the schema."""
    assert VRF_ROLES <= set(_device_roles()) and {"spine", "firewall", "controller", "provider"} <= set(
        NON_CARRYING_ROLES
    )
    assert VRF_DEVICE_KINDS <= set(_device_kinds())
    assert {"DcimVirtualDevice", "ManagedControllerPhysical", "ManagedControllerVirtual"} <= set(NON_CARRYING_KINDS)


@pytest.mark.parametrize("role", NON_CARRYING_ROLES)
def test_an_instance_on_a_non_carrying_role_is_an_error(role: str) -> None:
    """(e) every role of the dropdown that is not a routing role."""
    errors = uncarried_instance_errors(facts_of(_with_instance(role=role)))
    assert len(errors) == 1 and f"role '{role}'" in errors[0]


@pytest.mark.parametrize("role", sorted(VRF_ROLES))
def test_an_instance_on_a_carrying_role_is_not_an_uncarried_error(role: str) -> None:
    """(e) positive: every routing role may hold an instance."""
    assert uncarried_instance_errors(facts_of(_with_instance(role=role))) == []


@pytest.mark.parametrize("typename", NON_CARRYING_KINDS)
def test_an_instance_on_a_non_carrying_kind_is_an_error(typename: str) -> None:
    """(e) virtual devices and controllers, even with a routing role."""
    assert len(uncarried_instance_errors(facts_of(_with_instance(typename=typename)))) == 1


@pytest.mark.parametrize("typename", sorted(VRF_DEVICE_KINDS))
def test_an_instance_on_a_carrying_kind_is_not_an_uncarried_error(typename: str) -> None:
    """(e) positive: a physical device and our provider-hosted appliance."""
    assert uncarried_instance_errors(facts_of(_with_instance(typename=typename))) == []


@pytest.mark.parametrize("deployment_kind", sorted(PARTY_DEPLOYMENT_KINDS))
def test_an_instance_on_a_party_deployment_is_an_error(deployment_kind: str) -> None:
    """(e) a cloud provider router or an ISP PE with a routing role."""
    assert len(uncarried_instance_errors(facts_of(_with_instance(role="edge", deployment_kind=deployment_kind)))) == 1


@pytest.mark.parametrize("group", sorted(PARTY_GROUPS))
def test_an_instance_on_a_party_group_member_is_an_error(group: str) -> None:
    """(e) a provider stub inside our colocation zone."""
    assert len(uncarried_instance_errors(facts_of(_with_instance(role="edge", groups=(group,))))) == 1


def _no_findings(*devices: dict[str, Any]) -> None:
    """Assert every rule is silent on each device."""
    for device in devices:
        facts = facts_of(device)
        for rule in (*ERROR_RULES, *WARNING_RULES):
            assert rule(facts) == [], (device["name"], rule)


def test_s3_a_default_gateway_row_whose_svi_has_no_vrf_passes() -> None:
    """S3: ``vrf`` may be unset for a ``default`` row, and the row needs no instance."""
    row = raw_row("r1", 100, "default", segment="infra")
    _no_findings(raw_device("d1", "leaf-1", rows=[row], interfaces=[raw_svi("s1", 100, "r1")]))


def test_s5_rows_shared_through_an_mlag_domain_pass_with_an_svi_on_each_member() -> None:
    """S5: both members of a pair see the domain's rows, and each holds its own SVI and instance."""
    row = raw_row("r1", 100, "PROD", segment="web")
    members = [
        raw_device(
            f"d{i}",
            f"leaf-{i}",
            capabilities=[raw_mlag("mlag-1", row), raw_instance(f"v{i}", f"leaf-{i}", "PROD")],
            interfaces=[raw_svi(f"s{i}", 100, "r1", f"v{i}", "PROD")],
        )
        for i in (1, 2)
    ]
    _no_findings(*members)
