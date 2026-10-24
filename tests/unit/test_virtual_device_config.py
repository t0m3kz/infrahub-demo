"""A dedicated virtual firewall gets a real config artifact, not the no-platform stub.

queries/config/firewall.gql roots at the DcimDevice generic, so a DcimVirtualDevice member of the `firewalls`
target group is found like a DcimPhysicalDevice one. The load-balancer config query keeps one root per kind (its
fragment is shared with the validation query), so both kinds are guarded too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from graphql import FieldNode, OperationDefinitionNode, parse

from checks.firewall import CheckFirewall
from tests.smoke.fixture_builders.common import run_transform
from tests.smoke.fixture_builders.transit import build_virtual_firewall_data
from transforms.config.firewall import Firewall

EMPTY_ROOTS = {"DcimDevice": {"edges": []}, "SecurityZone": {"edges": []}}


def _root_fields(root_dir: Path, query_file: str) -> list[str]:
    """The root fields of the (single) operation of a query file."""
    document = parse((root_dir / query_file).read_text())
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return [s.name.value for s in operation.selection_set.selections if isinstance(s, FieldNode)]


def test_firewall_query_roots_at_the_device_generic(root_dir: Path) -> None:
    """Both device kinds are found by name: the first root is DcimDevice, never one kind."""
    assert _root_fields(root_dir, "queries/config/firewall.gql") == ["DcimDevice", "SecurityZone"]


def test_loadbalancer_query_selects_both_device_kinds(root_dir: Path) -> None:
    """The load balancer config query already reaches a dedicated virtual instance."""
    assert _root_fields(root_dir, "queries/config/loadbalancer.gql") == ["DcimPhysicalDevice", "DcimVirtualDevice"]


def test_virtual_firewall_payload_renders_its_contexts() -> None:
    """A dedicated virtual firewall renders its context legs, routes, NAT, isolation and cluster."""
    output = run_transform(Firewall, build_virtual_firewall_data())
    assert "has no platform defined" not in output
    assert "set hostname fw-dc10101-C007-p-dedicated" in output
    assert "add virtual-system name fw-C007-p-dedicated-context" in output
    assert "set interface eth1.3001 ipv4-address 100.66.0.13 mask-length 29" in output
    assert "set static-route default nexthop gateway address 100.66.32.9 on" in output
    assert "set cluster sync-interface eth7" in output
    assert 'mgmt_cli set simple-cluster name "fw-C007-p-dedicated-ha"' in output


def test_virtual_firewall_payload_is_validated_like_a_physical_one() -> None:
    """The firewall check reads the same query shape: a complete dedicated instance raises nothing."""
    errors: list[str] = []
    check = cast(Any, CheckFirewall.__new__(CheckFirewall))
    check.log_error = lambda message: errors.append(message)
    check.validate(build_virtual_firewall_data())
    assert errors == []


@pytest.mark.parametrize("data", [EMPTY_ROOTS, {"DcimDevice": {"edges": [{"node": {"name": {"value": "fw-x"}}}]}}])
def test_device_without_data_or_platform_still_yields_the_stub(data: dict) -> None:
    """No device found, or one without a platform: the placeholder, so the artifact never fails."""
    output = run_transform(Firewall, data)
    assert "has no platform defined" in output
    assert "No configuration generated." in output
