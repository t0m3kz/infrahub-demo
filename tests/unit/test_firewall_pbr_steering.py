"""Which firewall context the leaf PBR steers a footprint to (transforms/helpers/firewall_contexts.py _steering_only).

A footprint lists its internal context and, when it has one, its Internet edge context. The leaf steers its segments to
the internal context in the device's DC; a footprint with no internal context there is steered to its edge (then its
only firewall). The edge is never the shared fallback of the other footprints.
"""

from __future__ import annotations

from typing import Any

from tests.unit.firewall_helpers_shared import _make_pbr_activation
from tests.unit.routing_helpers import in_vrf, make_vrf
from transforms.helpers.firewall_contexts import _flatten_deployment_firewall_contexts
from transforms.helpers.firewall_pbr import get_customer_pbr_rules
from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE, INTERNAL_FIREWALL_ROLE


def _leg(role: str, address: str) -> dict[str, Any]:
    """A context leg of a member of ``role`` in PROD."""
    return {
        **in_vrf(make_vrf({"name": "PROD"})),
        "device": {"role": role},
        "ip_address": {"address": address, "ip_namespace": {"node": {"name": {"value": "default"}}}},
    }


def _context(context_id: str, role: str, address: str, served: list[str]) -> dict[str, Any]:
    """A context as ``_flatten_deployment_firewall_contexts`` returns it."""
    return {
        "typename": "ManagedFirewallContext",
        "id": context_id,
        "served_deployments": [{"id": footprint} for footprint in served],
        "legs": [_leg(role, address)],
        "shared_devices": [{"role": role}],
    }


INTERNAL = _context("ctx-int", INTERNAL_FIREWALL_ROLE, "10.65.0.5/29", ["dep-a"])
EDGE = _context("ctx-edge", EXTERNAL_FIREWALL_ROLE, "10.66.0.5/29", ["dep-a", "dep-b"])


def _firewall(name: str, role: str, context: dict[str, Any]) -> dict[str, Any]:
    """A firewall member of the hosting deployment holding ``context``."""
    cap = {key: context[key] for key in ("typename", "id")} | {"served_deployments": context["served_deployments"]}
    return {"name": name, "role": role, "shared_capabilities": [cap], "interfaces": []}


def test_the_edge_keeps_only_the_footprints_with_no_internal_context() -> None:
    """dep-a lists both and is steered to its internal context; dep-b has only the edge and is steered to it."""
    deployment = {
        "devices": [
            _firewall("fw-1", INTERNAL_FIREWALL_ROLE, INTERNAL),
            _firewall("xfw-1", EXTERNAL_FIREWALL_ROLE, EDGE),
        ]
    }
    contexts = {ctx["id"]: ctx for ctx in _flatten_deployment_firewall_contexts(deployment)}
    assert [d["id"] for d in contexts["ctx-int"]["served_deployments"]] == ["dep-a"]
    assert [d["id"] for d in contexts["ctx-edge"]["served_deployments"]] == ["dep-b"]


def test_an_edge_that_steers_no_footprint_is_no_next_hop() -> None:
    """Every footprint of the edge has an internal context: the edge drops out of the PBR contexts."""
    edge = _context("ctx-edge", EXTERNAL_FIREWALL_ROLE, "10.66.0.5/29", ["dep-a"])
    deployment = {
        "devices": [
            _firewall("fw-1", INTERNAL_FIREWALL_ROLE, INTERNAL),
            _firewall("xfw-1", EXTERNAL_FIREWALL_ROLE, edge),
        ]
    }
    assert [ctx["id"] for ctx in _flatten_deployment_firewall_contexts(deployment)] == ["ctx-int"]


def test_a_footprint_with_both_steers_to_the_internal_context() -> None:
    edge = {**EDGE, "served_deployments": [{"id": "dep-b"}]}
    rules = get_customer_pbr_rules(
        [_make_pbr_activation(deployment_id="dep-a", security_policies=[])], [INTERNAL, edge]
    )
    assert rules[0]["fw_nexthop"] == "10.65.0.4"


def test_a_footprint_with_only_the_edge_steers_to_it() -> None:
    edge = {**EDGE, "served_deployments": [{"id": "dep-b"}]}
    rules = get_customer_pbr_rules([_make_pbr_activation(deployment_id="dep-b", security_policies=[])], [edge])
    assert rules[0]["fw_nexthop"] == "10.66.0.4"


def test_the_edge_is_never_the_shared_fallback() -> None:
    """A footprint the edge does not serve keeps the shared internal context, whatever the order of the contexts."""
    edge = {**EDGE, "served_deployments": [{"id": "dep-b"}]}
    shared = {**INTERNAL, "served_deployments": []}
    for contexts in ([shared, edge], [edge, shared]):
        rules = get_customer_pbr_rules([_make_pbr_activation(deployment_id="dep-c", security_policies=[])], contexts)
        assert rules[0]["fw_nexthop"] == "10.65.0.4"
