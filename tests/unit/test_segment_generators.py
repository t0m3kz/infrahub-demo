"""Unit tests for VxlanSegmentGenerator.

VlanSegment has no generator — vlan_id is a plain manual attribute
(single-site, no pool allocation). Only VXLAN needs generator-driven
per-deployment realization (SegmentDeployment) since it can stretch
across multiple sites.

VxlanSegmentGenerator.generate() cleans GraphQL data, guards on missing
id/name/customer_deployments, resolves each customer footprint to its
hosting parent (TopologyDataCenter or TopologyColocationMetro) via
_resolve_hosting_parent(), then calls _activate_segment_in_deployment()
for each resolved parent (VNI-and-status only), followed by border-gateway
realization for a stretched segment (_realize_border_gateways —
customer-facing interface assignment is driven by AppComponent.instances
instead, see generators/topology/app_instance_segment.py and
tests/unit/test_app_instance_segment_generator.py) and inline sub-interface
creation.

For a local segment vni_pool is read straight from the vxlan_segment query's
TopologySegmentHosting parent fragment (queries/topology/add/vxlan_segment.gql)
— no separate client.get() round-trip per deployment. A stretched segment
instead gets the global GLOBAL-L2VNI pool, looked up once per run.
_activate_segment_in_deployment() does an idempotency check via
client.filters(), allocates VNI from that pool dict (or reuses a stretched
segment's VNI), then calls client.create() / save().

Tests use asyncio.run() directly — same pattern as test_circuit_generators.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedSegmentDeployment,
    ManagedStandaloneVlanDomain,
    ManagedVlanDomainSegment,
    ManagedVxlanSegment,
    SecurityZone,
)
from generators.topology.segment import STRETCHED_VNI_POOL_NAME, VxlanSegmentGenerator

# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    """Return a VxlanSegmentGenerator instance with mocked client and logger.

    Typed as Any so that ty does not flag mock attribute assignments or
    mock method calls (e.g. gen.logger.error.assert_called_once()).
    """
    gen = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    # generate() waits for an in-flight add_dc/dc_pod_cascade on a target
    # deployment missing its vni_pool (see customer_dc.py's identical
    # wait) — no in-flight parent in these unit tests, so no-op.
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    # Inline termination has its own test classes below — no legs by default.
    gen._reconcile_inline_service_ports = AsyncMock(return_value=[])
    # VLAN domain activation reconciliation has its own tests
    # (tests/unit/test_vlan_domain_reconcile.py) — stubbed at the boundary.
    gen.reconcile_segment_vlan_domains = AsyncMock()
    # security_zone assignment is a separate concern with its own test class
    # below (TestEnsureSecurityZone) — stub it out here so generate()-level
    # tests stay focused on SegmentDeployment/interface/inline-sub-interface
    # behavior.
    gen._ensure_security_zone = AsyncMock()
    return gen


# ---------------------------------------------------------------------------
# Raw GQL response builder
# ---------------------------------------------------------------------------


def _seg_response(
    seg_id: str,
    seg_name: str,
    deployments: list[dict],
    stretch_scope: str | None = None,
) -> dict:
    """Build a raw (un-cleaned) GraphQL response for a vxlan_segment_data query.

    Each entry in `deployments` is a customer footprint (CustomerDC/Colocation)
    dict with "id", "name", and "parent" (the hosting DC/Metro dict, optionally
    carrying a "vni_pool" sub-dict with "id"/"name") — mirroring the
    `... on TopologyCustomerDC/Colocation { parent { node {... vni_pool ...} } }`
    fragment in the real vxlan_segment query.
    """

    def _pool_node(pool: dict | None) -> dict | None:
        if pool is None:
            return None
        return {"node": {"id": pool["id"], "name": {"value": pool["name"]}}}

    dep_edges = []
    for d in deployments:
        parent = d["parent"]
        parent_node: dict[str, Any] = {"id": parent["id"], "name": {"value": parent["name"]}}
        if "vni_pool" in parent:
            parent_node["vni_pool"] = _pool_node(parent["vni_pool"])
        dep_edges.append(
            {
                "node": {
                    "id": d["id"],
                    "name": {"value": d["name"]},
                    "parent": {"node": parent_node},
                }
            }
        )
    return {
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": seg_id,
                        "name": {"value": seg_name},
                        "customer_deployments": {"edges": dep_edges},
                        **({"stretch_scope": {"value": stretch_scope}} if stretch_scope else {}),
                    }
                }
            ]
        }
    }


# Convenience customer-deployment fixtures — each resolves to hosting parent dc-1/dc-2
_DEP_1 = {
    "id": "cust-1",
    "name": "C001-P-DC1",
    "parent": {
        "id": "dc-1",
        "name": "DC-1",
        "vni_pool": {"id": "pool-vni-1", "name": "DC1-VNI-Pool"},
    },
}
_DEP_2 = {
    "id": "cust-2",
    "name": "C002-P-DC2",
    "parent": {
        "id": "dc-2",
        "name": "DC-2",
        "vni_pool": {"id": "pool-vni-2", "name": "DC2-VNI-Pool"},
    },
}

# ===========================================================================
# TestVxlanSegmentGeneratorGenerate
# ===========================================================================


class TestVxlanSegmentGeneratorGenerate:
    """Tests for generate() on VxlanSegmentGenerator."""

    def test_empty_response_logs_error(self):
        gen = _make_gen()
        gen._create_inline_sub_interfaces = AsyncMock()
        data = {"ManagedVxlanSegment": {"edges": []}}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "No ManagedVxlanSegment" in gen.logger.error.call_args[0][0]

    def test_missing_segment_id_logs_error(self):
        gen = _make_gen()
        gen._create_inline_sub_interfaces = AsyncMock()
        # id="" triggers the "missing id or name" guard
        data = _seg_response(seg_id="", seg_name="vxlan-1000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "missing id or name" in gen.logger.error.call_args[0][0]

    def test_no_deployments_logs_error(self):
        gen = _make_gen()
        gen._create_inline_sub_interfaces = AsyncMock()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[])
        asyncio.run(gen.generate(data))
        gen.logger.warning.assert_called_once()
        warning_msg = gen.logger.warning.call_args[0][0]
        assert "customer deployment" in warning_msg.lower()
        gen.logger.error.assert_called_once()
        assert "could not resolve" in gen.logger.error.call_args[0][0].lower()
        # Must not have called client.create (no SegmentDeployment creation)
        gen.client.create.assert_not_called()

    def test_happy_path_calls_activate_for_each_deployment(self):
        """Two deployments produce two _activate_segment_in_deployment calls."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])
        asyncio.run(gen.generate(data))

        assert gen._activate_segment_in_deployment.call_count == 2
        calls = gen._activate_segment_in_deployment.call_args_list
        dep_ids_called = {c.kwargs["deployment_id"] for c in calls}
        assert dep_ids_called == {"dc-1", "dc-2"}
        for c in calls:
            assert c.kwargs["segment_id"] == "seg-1"

    def test_happy_path_passes_vni_pool_through(self):
        """vni_pool from the query's parent fragment is forwarded as-is."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))

        call = gen._activate_segment_in_deployment.call_args
        assert call.kwargs["vni_pool"] == {"id": "pool-vni-1", "name": "DC1-VNI-Pool"}

    def test_stretched_segment_uses_global_pool_for_every_deployment(self):
        """A stretched segment never draws from a site pool: every deployment gets
        GLOBAL-L2VNI, so its one VNI cannot collide with a site's local segments."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="pool-global"))
        gen.client.filters = AsyncMock(return_value=[])
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(
            seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2], stretch_scope="global"
        )
        asyncio.run(gen.generate(data))

        assert gen.client.get.call_args.kwargs["name__value"] == STRETCHED_VNI_POOL_NAME
        pools = [c.kwargs["vni_pool"] for c in gen._activate_segment_in_deployment.call_args_list]
        assert pools == [{"id": "pool-global", "name": STRETCHED_VNI_POOL_NAME}] * 2

    def test_stretched_segment_without_global_pool_is_a_hard_error(self):
        """Missing GLOBAL-L2VNI fails the run instead of falling back to a site pool."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=None)
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1], stretch_scope="global")
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_not_called()
        assert STRETCHED_VNI_POOL_NAME in gen.logger.error.call_args[0][0]

    def test_local_segment_does_not_reuse_prefetched_vni(self):
        """A local segment active in two sites gets each site's own pool and no
        reusable VNI, even when another site's deployment already has one."""
        gen = _make_gen()
        existing_dep = MagicMock()
        existing_dep.vni.value = 10100
        existing_dep.deployment = None
        gen.client.filters = AsyncMock(return_value=[existing_dep])
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])
        asyncio.run(gen.generate(data))

        calls = gen._activate_segment_in_deployment.call_args_list
        assert [c.kwargs["vni_pool"]["id"] for c in calls] == ["pool-vni-1", "pool-vni-2"]
        assert all(c.kwargs["reusable_vni"] is None for c in calls)

    def test_deployment_missing_id_is_skipped(self):
        """A customer deployment entry with id='' is skipped; only the valid one triggers activation."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = {
            "ManagedVxlanSegment": {
                "edges": [
                    {
                        "node": {
                            "id": "seg-1",
                            "name": {"value": "vxlan-1000"},
                            "customer_deployments": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "cust-1",
                                            "name": {"value": "C001-P-DC1"},
                                            "parent": {"node": {"id": "dc-1", "name": {"value": "DC-1"}}},
                                        }
                                    },
                                    {"node": {"id": "", "name": {"value": "CUST-BAD"}}},
                                ]
                            },
                        }
                    }
                ]
            }
        }
        asyncio.run(gen.generate(data))

        assert gen._activate_segment_in_deployment.call_count == 1
        assert gen._activate_segment_in_deployment.call_args.kwargs["deployment_id"] == "dc-1"

    def test_generate_reconciles_vlan_domains_and_creates_inline_interfaces(self):
        """generate() activates each deployment, reconciles the segment's VLAN
        domain activations (local or stretched alike), and creates inline
        sub-interfaces."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-3", seg_name="vxlan-2000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_awaited_once()
        gen.reconcile_segment_vlan_domains.assert_awaited_once_with("seg-3", "vxlan-2000")
        gen._create_inline_sub_interfaces.assert_awaited_once()

    def test_missing_vni_pool_waits_on_parent_dc_generators(self):
        """A target deployment with no vni_pool (its own add_dc/dc_pod_cascade,
        or add_colocation_metro for a metro, hasn't run yet) triggers a wait on
        every parent generator before _resolve_target_deployments is retried."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        dep_no_pool = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[dep_no_pool])
        asyncio.run(gen.generate(data))

        assert gen.wait_for_parent_generator_and_refetch.await_args_list == [
            (("add_dc", "dc-1"), {}),
            (("dc_pod_cascade", "dc-1"), {}),
            (("add_colocation_metro", "dc-1"), {}),
        ]

    def test_refetched_data_is_reparsed_when_parent_was_in_flight(self):
        """If add_dc was in-flight, the refreshed data (now carrying the
        vni_pool that was missing the first time) replaces the segment
        before target deployments are re-resolved."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        refreshed_payload = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed_payload, None, None])

        dep_no_pool = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[dep_no_pool])
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_awaited_once()
        assert gen._activate_segment_in_deployment.call_args.kwargs["vni_pool"] == {
            "id": "pool-vni-1",
            "name": "DC1-VNI-Pool",
        }

    def test_ensure_security_zone_called_with_segment_environment(self):
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        data["ManagedVxlanSegment"]["edges"][0]["node"]["environment"] = {"value": "d"}
        asyncio.run(gen.generate(data))

        gen._ensure_security_zone.assert_awaited_once_with(
            segment_id="seg-1", segment_name="vxlan-1000", environment="d"
        )

    def test_ensure_security_zone_defaults_to_production_environment(self):
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))

        assert gen._ensure_security_zone.call_args.kwargs["environment"] == "p"

    def test_ensure_security_zone_runs_even_when_no_deployments_resolved(self):
        """Zone classification is a property of the segment itself, not of
        whether its hosting parent could be resolved."""
        gen = _make_gen()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[])
        asyncio.run(gen.generate(data))

        gen._ensure_security_zone.assert_awaited_once()


# ===========================================================================
# TestResolveHostingParent
# ===========================================================================


class TestResolveHostingParent:
    """Tests for _resolve_hosting_parent — resolves a customer footprint
    (CustomerDC/CustomerColocation) to its hosting parent (DC/Metro dict)."""

    def test_returns_parent_when_present(self):
        gen = _make_gen()
        customer_deployment = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
        result = gen._resolve_hosting_parent(customer_deployment, "vxlan-1000")
        assert result == {"id": "dc-1", "name": "DC-1"}

    def test_missing_id_logs_warning_returns_none(self):
        gen = _make_gen()
        result = gen._resolve_hosting_parent({"name": "no-id"}, "vxlan-1000")
        assert result is None
        gen.logger.warning.assert_called_once()

    def test_missing_parent_logs_error_returns_none(self):
        """No fallback fetch: if the query response has no parent, log and skip."""
        gen = _make_gen()
        result = gen._resolve_hosting_parent({"id": "cust-1", "name": "C001-P-DC1"}, "vxlan-1000")
        assert result is None
        gen.logger.error.assert_called_once()
        assert "no parent in the query response" in gen.logger.error.call_args[0][0]
        gen.client.get.assert_not_called()


# ===========================================================================
# TestActivateSegmentInDeployment
# ===========================================================================


class TestActivateSegmentInDeployment:
    """Tests for _activate_segment_in_deployment on VxlanSegmentGenerator.

    VNI-and-status only now — LOCAL VLAN ID realization moved to a separate
    per-VLAN-domain mechanism (ManagedVlanDomainSegment), tested in
    TestAssignSegmentToDcInterfaces below.
    """

    # Common invocation kwargs used in every test
    _CALL = dict(
        segment_id="seg-1",
        segment_name="vxlan-1000",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen, **overrides) -> None:
        asyncio.run(gen._activate_segment_in_deployment(**{**self._CALL, **overrides}))

    def test_idempotent_existing_deployment_skips_create(self):
        """When client.filters returns an existing record, create is never called."""
        gen = _make_gen()
        existing = MagicMock()
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])

        self._run(gen)

        gen.client.create.assert_not_called()
        existing.save.assert_called_once()
        # An info message mentioning "already exists" should be logged
        info_msgs = " ".join(str(c) for c in gen.logger.info.call_args_list)
        assert "already exists" in info_msgs

    def test_creates_segment_deployment(self):
        """Happy path: no existing record → create() called with segment/deployment/status only."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        vni_pool = {"id": "pool-vni", "name": "DC1-VNI-Pool"}

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen, vni_pool=vni_pool)

        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == ManagedSegmentDeployment
        call_data = call_kwargs["data"]

        assert call_data["segment"] == {"id": "seg-1"}
        assert call_data["deployment"] == {"id": "dc-1"}
        assert "vlan_id" not in call_data
        assert "from_pool" in call_data["vni"]
        assert call_data["vni"]["from_pool"]["id"] == "pool-vni"
        activation.save.assert_called_once()

    def test_create_exception_logs_error(self):
        """client.create() raising an exception results in an error log."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=Exception("network timeout"))

        self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"})

        error_msg = gen.logger.error.call_args[0][0]
        assert "Failed to create" in error_msg

    def test_idempotency_check_exception_still_proceeds(self):
        """If client.filters raises, a warning is logged but execution continues
        through to create() — does not silently drop the activation."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(side_effect=Exception("timeout"))

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"})

        # Warning about the exception during idempotency check
        warning_msgs = " ".join(str(c) for c in gen.logger.warning.call_args_list)
        assert "Error checking existing activations" in warning_msgs or len(gen.logger.warning.call_args_list) >= 1
        # Fell through to create despite the filters() exception
        gen.client.create.assert_called_once()


# ===========================================================================
# TestVxlanVniAllocation
# ===========================================================================


class TestVxlanVniAllocation:
    """Tests for VNI allocation logic inside _activate_segment_in_deployment."""

    _CALL = dict(
        segment_id="seg-2",
        segment_name="vxlan-1001",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen, **overrides) -> None:
        asyncio.run(gen._activate_segment_in_deployment(**{**self._CALL, **overrides}))

    def test_local_segment_never_reuses_other_site_vni(self):
        """A local segment allocates from its own site's pool even when another
        site already has a VNI for it — reusing that literal is how one site's
        pool value collided with another site's own allocation."""
        gen = _make_gen()

        existing_dep = MagicMock()
        existing_dep.resolve = AsyncMock()
        existing_dep.vni = MagicMock()
        existing_dep.vni.value = 10100

        # Idempotency check → no existing for this dc. The reuse lookup must
        # not happen for a local segment, so the second answer stays unused.
        gen.client.filters = AsyncMock(side_effect=[[], [existing_dep]])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"}, reusable_vni=10100)

        call_data = gen.client.create.call_args.kwargs["data"]
        assert call_data["vni"]["from_pool"]["id"] == "pool-vni"
        assert call_data["vni"]["identifier"] == "seg-2-dc-1-vni"
        assert gen.client.filters.await_count == 1

    def test_allocates_vni_from_pool_when_first_dc(self):
        """When no prior SegmentDeployment exists, VNI is allocated from vni_pool
        via from_pool dict syntax."""
        gen = _make_gen()

        # Both idempotency and VNI-reuse checks return empty
        gen.client.filters = AsyncMock(side_effect=[[], []])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"})

        call_data = gen.client.create.call_args.kwargs["data"]
        assert "from_pool" in call_data["vni"]
        assert call_data["vni"]["from_pool"]["id"] == "pool-vni"

    def test_no_vni_pool_logs_warning_but_still_creates(self):
        """If no vni_pool is found (and no prior VNI to reuse), a warning is logged
        but client.create() is still called — 'vni' is simply absent from call_data."""
        gen = _make_gen()

        gen.client.filters = AsyncMock(side_effect=[[], []])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen, vni_pool=None)

        warning_msgs = " ".join(str(c) for c in gen.logger.warning.call_args_list)
        assert "vni_pool" in warning_msgs

        gen.client.create.assert_called_once()
        call_data = gen.client.create.call_args.kwargs["data"]
        assert "vni" not in call_data

    def test_stretched_reuses_existing_vni_and_skips_vni_pool(self):
        """Stretched allocation reuses existing VNI and avoids new vni_pool calls."""
        gen = _make_gen()

        existing_dep = MagicMock()
        existing_dep.resolve = AsyncMock()
        existing_dep.vni = MagicMock()
        existing_dep.vni.value = 10100

        # idempotency check -> no existing for this deployment
        # reusable-VNI lookup -> one existing deployment with vni=10100
        gen.client.filters = AsyncMock(side_effect=[[], [existing_dep]])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(
            gen._activate_segment_in_deployment(
                **self._CALL,
                stretch_scope="dc_pair",
            )
        )

        call_data = gen.client.create.call_args.kwargs["data"]
        assert call_data["vni"] == 10100

    def test_stretched_allocates_from_given_pool_with_shared_identifier(self):
        """First stretched deployment allocates from the pool it is given (generate()
        passes GLOBAL-L2VNI) using the shared segment identifier."""
        gen = _make_gen()

        # idempotency check -> no existing for this deployment
        # reusable-VNI lookup -> none
        gen.client.filters = AsyncMock(side_effect=[[], []])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(
            gen._activate_segment_in_deployment(
                **self._CALL,
                vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"},
                stretch_scope="dc_pair",
            )
        )

        call_data = gen.client.create.call_args.kwargs["data"]
        assert call_data["vni"]["from_pool"]["id"] == "pool-vni"
        assert call_data["vni"]["identifier"] == "seg-2-vni"


# ===========================================================================
# TestResolveVlanDomain / TestAssignSegmentToDcInterfaces
# ===========================================================================


class TestResolveVlanDomain:
    """Tests for _resolve_vlan_domain — MLAG-or-standalone domain resolution."""

    def test_device_with_mlag_capability_returns_mlag_domain(self):
        gen = _make_gen()
        device = MagicMock(id="dev-1")
        mlag_peer = MagicMock(id="mlag-1")
        mlag_peer.typename = "ManagedMLAG"
        device.capabilities = MagicMock(peers=[mlag_peer])

        result = asyncio.run(gen._resolve_vlan_domain(device))
        assert result == ("ManagedMLAG", "mlag-1")

    def test_device_without_mlag_capability_returns_standalone_domain(self):
        gen = _make_gen()
        device = MagicMock(id="dev-1")
        other_peer = MagicMock(id="bgp-1")
        other_peer.typename = "ManagedBGP"
        device.capabilities = MagicMock(peers=[other_peer])

        result = asyncio.run(gen._resolve_vlan_domain(device))
        assert result == ("DcimPhysicalDevice", "dev-1")

    def test_device_with_no_capabilities_attribute_returns_standalone_domain(self):
        gen = _make_gen()
        device = MagicMock(id="dev-1", spec=["id"])

        result = asyncio.run(gen._resolve_vlan_domain(device))
        assert result == ("DcimPhysicalDevice", "dev-1")


class TestCreateVlanDomainSegment:
    """_create_vlan_domain_segment — one new (segment, domain) pair, untracked."""

    def test_domain_without_pool_logs_error(self):
        gen = _make_gen()
        domain_obj = MagicMock()
        domain_obj.vlan_pool = None
        gen.client.get = AsyncMock(return_value=domain_obj)

        asyncio.run(gen._create_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        gen.client.create.assert_not_called()
        error_msg = gen.logger.error.call_args[0][0]
        assert "vlan_pool" in error_msg

    def test_allocates_vlan_id_from_domain_pool_untracked(self) -> None:
        """The pair is shared desired state of the segment: saved with
        update_group_context=False, so no run's cleanup can delete it."""
        gen = _make_gen()
        domain_obj = MagicMock()
        domain_obj.vlan_pool = MagicMock(id="pool-vlan-1")
        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=domain_obj)
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(gen._create_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["segment"] == {"id": "seg-1"}
        assert call_kwargs["data"]["vlan_domain"] == {"id": "mlag-1"}
        assert call_kwargs["data"]["vlan_id"]["from_pool"]["id"] == "pool-vlan-1"
        assert call_kwargs["data"]["vlan_id"]["identifier"] == "seg-1-mlag-1-vlan"
        activation.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_known_pool_skips_reading_the_domain(self) -> None:
        """A pool id handed over by the caller is used as is, without a domain read."""
        gen = _make_gen()
        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.get = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(gen._create_vlan_domain_segment("seg-1", "vxlan-1000", "domain-1", "pool-1"))

        gen.client.get.assert_not_called()
        assert gen.client.create.call_args.kwargs["data"]["vlan_id"]["from_pool"]["id"] == "pool-1"


class TestEnsureStandaloneVlanDomain:
    """The standalone domain and its pool belong to the device's generator:
    looked up by name, never created or saved here."""

    @staticmethod
    def _device() -> MagicMock:
        device = MagicMock(id="dev-1")
        device.name.value = "bl-dc101101"
        return device

    def test_existing_domain_is_read_not_saved(self) -> None:
        """The domain is found by its device-derived name and returned with its pool."""
        gen = _make_gen()
        domain = MagicMock(id="domain-1")
        domain.vlan_pool = MagicMock(id="pool-1")
        domain.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=domain)

        result = asyncio.run(gen._ensure_standalone_vlan_domain(self._device()))

        assert result == ("domain-1", "pool-1")
        kwargs = gen.client.get.call_args.kwargs
        assert kwargs["kind"] == ManagedStandaloneVlanDomain
        assert kwargs["name__value"] == "bl-dc101101-vlan-domain"
        assert kwargs["include"] == ["vlan_pool"]
        domain.save.assert_not_called()
        gen.client.create.assert_not_called()

    def test_domain_without_pool_returns_none_pool(self) -> None:
        """A domain whose pool is not attached yet hands back no pool id."""
        gen = _make_gen()
        domain = MagicMock(id="domain-1")
        domain.vlan_pool = None
        gen.client.get = AsyncMock(return_value=domain)

        assert asyncio.run(gen._ensure_standalone_vlan_domain(self._device())) == ("domain-1", None)

    def test_missing_domain_is_an_error(self) -> None:
        """The device generator owns the domain; a missing one fails the run instead of being created."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=None)

        with pytest.raises(GeneratorError, match="bl-dc101101-vlan-domain"):
            asyncio.run(gen._ensure_standalone_vlan_domain(self._device()))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()


def _iface(iface_id: str, name: str, device_id: str, role: str = "uplink", cable_id: str | None = None) -> MagicMock:
    """A DcimPhysicalInterface SDK node (role, device and cable relationships)."""
    iface = MagicMock(id=iface_id)
    iface.name.value = name
    iface.role.value = role
    iface.device.id = device_id
    iface.cable = MagicMock(id=cable_id) if cable_id else None
    return iface


def _cable(cable_id: str, *endpoint_ids: str) -> MagicMock:
    cable = MagicMock(id=cable_id)
    cable.endpoints.peers = [MagicMock(id=endpoint_id) for endpoint_id in endpoint_ids]
    return cable


def _inline_gen(filters: dict[Any, Any]) -> Any:
    """A generator whose client.filters answers per kind from ``filters``,
    with the real inline-termination methods (not _make_gen's stub)."""
    gen = _make_gen()
    del gen._reconcile_inline_service_ports

    async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        value = filters.get(kind, [])
        return value(**kwargs) if callable(value) else value

    gen.client.filters = AsyncMock(side_effect=_filters)

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        yield

    gen.resource_lock = _lock
    return gen


_INLINE_SEGMENT: dict[str, Any] = {
    "id": "seg-1",
    "name": "c001-web-p",
    "terminate_inline": True,
    "inline_service": {
        "id": "ha-1",
        "capabilities": [{"id": "fw-1", "name": "fw-01"}, {"id": "fw-2", "name": "fw-02"}],
    },
    "gateway": {
        "id": "gw-1",
        "address": "10.1.0.1/24",
        "ip_prefix": {"id": "pfx-1", "prefix": "10.1.0.0/24", "ip_namespace": {"id": "ns-prod"}},
    },
}


class TestInlineLegs:
    """Where an inline segment terminates: each HA member port cabled to a border-leaf service port."""

    @staticmethod
    def _filters(routed_parent: str | None = None) -> dict[Any, Any]:
        bl_device = MagicMock(id="bl-1")
        bl_device.name.value = "bl-01"
        routed = []
        if routed_parent:
            sub = MagicMock()
            sub.parent_interface.id = routed_parent
            routed = [sub]
        return {
            DcimPhysicalInterface: lambda **kwargs: (
                [
                    _iface("fw1-up", "ethernet1/1", "fw-1", cable_id="c1"),
                    _iface("fw1-dn", "ethernet1/2", "fw-1", role="downlink", cable_id="c2"),
                    _iface("fw2-ha", "ethernet1/7", "fw-2", role="ha"),
                ]
                if "device__ids" in kwargs
                else [
                    _iface("bl-fw", "Ethernet1/15", "bl-1", role="firewall"),
                    _iface("lb-up", "eth1", "lb-1", role="uplink"),
                ]
            ),
            DcimCable: [_cable("c1", "fw1-up", "bl-fw"), _cable("c2", "fw1-dn", "lb-up")],
            DcimVirtualInterface: routed,
            DcimPhysicalDevice: [bl_device],
        }

    def test_member_port_cabled_to_a_service_port_is_a_leg(self) -> None:
        """Only the port facing a border-leaf firewall port terminates the segment."""
        gen = _inline_gen(self._filters())

        legs = asyncio.run(gen._inline_legs(_INLINE_SEGMENT))

        assert [(leg["member_port"].id, leg["border_port_id"]) for leg in legs] == [("fw1-up", "bl-fw")]
        assert legs[0]["member_device_name"] == "fw-01"
        assert legs[0]["border_device"].id == "bl-1"

    def test_routed_border_port_is_an_error(self) -> None:
        """A pbr-mode border port parenting routed sub-interfaces cannot also be the segment's trunk."""
        gen = _inline_gen(self._filters(routed_parent="bl-fw"))

        assert asyncio.run(gen._inline_legs(_INLINE_SEGMENT)) == []
        assert "routed parent" in gen.logger.error.call_args[0][0]

    def test_no_inline_service_has_no_legs(self) -> None:
        """terminate_inline without an inline_service terminates nowhere."""
        gen = _inline_gen({})

        assert asyncio.run(gen._inline_legs({**_INLINE_SEGMENT, "inline_service": None})) == []
        gen.logger.warning.assert_called_once()


class TestReconcileInlineServicePorts:
    """The segment's border service-port tags follow its inline legs; customer ports are untouched."""

    @staticmethod
    def _gen(tagged: list[dict[str, Any]]) -> Any:
        gen = _inline_gen({})
        gen._inline_legs = AsyncMock(return_value=[{"border_port_id": "bl-fw-new"}])
        gen._fetch_segment_vlan_state = AsyncMock(
            return_value={"segment": {"interface_capabilities": tagged}, "activations": {}}
        )
        gen.segment_obj = MagicMock()
        gen.segment_obj.add_relationships = AsyncMock()
        gen.segment_obj.remove_relationships = AsyncMock()
        gen.client.get = AsyncMock(return_value=gen.segment_obj)
        return gen

    def test_tags_new_and_untags_stale_service_ports(self) -> None:
        """A service port no leg faces any more is untagged; a customer port is never touched."""
        gen = self._gen(
            [
                {"id": "bl-fw-old", "typename": "DcimPhysicalInterface", "role": "firewall"},
                {"id": "leaf-cust", "typename": "DcimPhysicalInterface", "role": "customer"},
            ]
        )

        legs = asyncio.run(gen._reconcile_inline_service_ports(_INLINE_SEGMENT))

        assert legs == [{"border_port_id": "bl-fw-new"}]
        gen.segment_obj.add_relationships.assert_awaited_once_with(
            relation_to_update="interface_capabilities", related_nodes=["bl-fw-new"]
        )
        gen.segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="interface_capabilities", related_nodes=["bl-fw-old"]
        )

    def test_terminate_inline_off_untags_every_service_port(self) -> None:
        """Turning terminate_inline off leaves no border service port tagged."""
        gen = self._gen([{"id": "bl-fw-old", "typename": "DcimPhysicalInterface", "role": "firewall"}])

        legs = asyncio.run(gen._reconcile_inline_service_ports({**_INLINE_SEGMENT, "terminate_inline": False}))

        assert legs == []
        gen._inline_legs.assert_not_awaited()
        gen.segment_obj.add_relationships.assert_not_awaited()
        gen.segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="interface_capabilities", related_nodes=["bl-fw-old"]
        )


class TestCreateInlineSubInterfaces:
    """Each leg gets <member port>.<border VLAN> with the member's own address; the gateway stays the VIP."""

    @staticmethod
    def _gen(vlan_domain: str = "mlag-bl") -> Any:
        activation = MagicMock()
        activation.vlan_domain.id = vlan_domain
        activation.vlan_id.value = 210
        gen = _inline_gen({ManagedVlanDomainSegment: [activation]})
        gen.segment_obj = MagicMock(id="seg-1")
        gen.client.get = AsyncMock(return_value=gen.segment_obj)
        gen._resolve_device_vlan_domain = AsyncMock(return_value=("mlag-bl", None))
        gen.ensure_prefix_address_pool = AsyncMock(return_value=MagicMock(id="pool-1"))
        gen.allocate_prefix_address = AsyncMock(return_value="ip-own-1")
        gen.ensure_vlan_subinterface = AsyncMock()
        return gen

    @staticmethod
    def _leg() -> dict[str, Any]:
        border = MagicMock(id="bl-1")
        border.name.value = "bl-01"
        return {
            "member_port": _iface("fw1-up", "ethernet1/1", "fw-1"),
            "member_device_id": "fw-1",
            "member_device_name": "fw-01",
            "border_port_id": "bl-fw",
            "border_device": border,
        }

    def test_sub_interface_takes_border_vlan_and_own_address(self) -> None:
        """VLAN from the border leaf's domain, address reserved in the segment prefix at its length."""
        gen = self._gen()

        asyncio.run(gen._create_inline_sub_interfaces(_INLINE_SEGMENT, [self._leg()]))

        gen.ensure_prefix_address_pool.assert_awaited_once_with(
            pool_name="inline-seg-1-pool", prefix_id="pfx-1", prefix_length=24, namespace_id="ns-prod"
        )
        alloc = gen.allocate_prefix_address.await_args.kwargs
        assert alloc["identifier"] == "seg-1-fw1-up-inline"
        assert alloc["prefix_length"] == 24
        sub = gen.ensure_vlan_subinterface.await_args.kwargs
        assert sub["vlan_id_value"] == 210
        assert sub["ip_address_id"] == "ip-own-1"
        assert sub["capability_obj"] is gen.segment_obj
        assert sub["device_name"] == "fw-01"

    def test_missing_border_vlan_is_an_error(self) -> None:
        """No VLAN in the facing domain means the leg cannot be terminated."""
        gen = self._gen(vlan_domain="other-domain")

        asyncio.run(gen._create_inline_sub_interfaces(_INLINE_SEGMENT, [self._leg()]))

        gen.ensure_vlan_subinterface.assert_not_awaited()
        gen.logger.error.assert_called_once()

    def test_no_legs_writes_nothing(self) -> None:
        """A segment that is not terminated inline creates no pool and no sub-interface."""
        gen = self._gen()

        asyncio.run(gen._create_inline_sub_interfaces(_INLINE_SEGMENT, []))

        gen.ensure_prefix_address_pool.assert_not_awaited()
        gen.ensure_vlan_subinterface.assert_not_awaited()


# ===========================================================================
# TestEnsureSecurityZone
# ===========================================================================


class TestEnsureSecurityZone:
    """security_zone used to be hand-authored (data/security/01_security_zones.yml,
    never loaded by anything, never set on a real segment). This derives the
    same PROD-ZONE/NONPROD-ZONE classification from the segment's own
    `environment`, unlocking the cross-zone branch in
    generators/helpers/rules.py's RulesPlanner.zone_context/pick_profile_name.

    Builds its own generator rather than using the shared _make_gen(), which
    stubs _ensure_security_zone out for every other test class in this file.
    """

    @staticmethod
    def _make_gen() -> Any:
        gen = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        return gen

    def test_reuses_existing_zone(self):
        gen = self._make_gen()
        existing_zone = MagicMock(id="zone-prod-1")
        existing_zone.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing_zone])
        segment_obj = MagicMock()
        segment_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=segment_obj)

        asyncio.run(gen._ensure_security_zone(segment_id="seg-1", segment_name="vxlan-1000", environment="p"))

        gen.client.filters.assert_awaited_once_with(kind=SecurityZone, name__value="PROD-ZONE")
        gen.client.create.assert_awaited_once_with(
            kind=ManagedVxlanSegment,
            data={"id": "seg-1", "security_zone": {"id": "zone-prod-1"}},
        )
        # The zone is shared by every segment of the environment: not claimed.
        existing_zone.save.assert_not_called()
        # The segment is this run's own target: never put in its group.
        segment_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_creates_zone_when_missing(self):
        gen = self._make_gen()
        zone_obj = MagicMock(id="zone-nonprod-1")
        zone_obj.save = AsyncMock()
        segment_obj = MagicMock()
        segment_obj.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=[zone_obj, segment_obj])

        asyncio.run(gen._ensure_security_zone(segment_id="seg-1", segment_name="vxlan-1000", environment="d"))

        zone_call, segment_call = gen.client.create.call_args_list
        assert zone_call.kwargs["kind"] == SecurityZone
        assert zone_call.kwargs["data"]["name"] == "NONPROD-ZONE"
        assert zone_call.kwargs["data"]["trust_level"] == 50
        assert segment_call.kwargs["data"]["security_zone"] == {"id": "zone-nonprod-1"}
        zone_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
        segment_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_non_production_codes_all_map_to_nonprod_zone(self):
        for environment in ("n", "s", "d", "t"):
            gen = self._make_gen()
            gen.client.filters = AsyncMock(return_value=[MagicMock(id="zone-1", save=AsyncMock())])
            gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))

            asyncio.run(
                gen._ensure_security_zone(segment_id="seg-1", segment_name="vxlan-1000", environment=environment)
            )

            gen.client.filters.assert_awaited_once_with(kind=SecurityZone, name__value="NONPROD-ZONE")

    def test_segment_update_failure_is_logged_not_raised(self):
        gen = self._make_gen()
        gen.client.filters = AsyncMock(return_value=[MagicMock(id="zone-1", save=AsyncMock())])
        gen.client.create = AsyncMock(side_effect=Exception("boom"))

        asyncio.run(gen._ensure_security_zone(segment_id="seg-1", segment_name="vxlan-1000", environment="p"))

        gen.logger.warning.assert_called_once()


class TestDeviceDeploymentIds:
    """_device_deployment_ids — which deployments' devices a segment lands on."""

    def test_dc_includes_its_pods_but_not_customer_footprints(self) -> None:
        """A DC's children mix pods and CustomerDC footprints; the query selects
        ids only on pods, so a footprint arrives as an empty node and is skipped."""
        dc = {"id": "dc-1", "children": [{}, {"id": "pod-1"}, {"id": "pod-2"}]}
        assert VxlanSegmentGenerator._device_deployment_ids(dc) == ["dc-1", "pod-1", "pod-2"]

    def test_query_selects_child_ids_only_on_pods(self, root_dir: Path) -> None:
        """The pod-only filter lives in vxlan_segment.gql, not in Python."""
        query = (root_dir / "queries" / "topology" / "add" / "vxlan_segment.gql").read_text()
        compact = " ".join(query.split())
        assert "... on TopologyDataCenter { children { edges { node { ... on TopologyPod { id } } } } }" in compact

    def test_metro_without_children_is_only_itself(self) -> None:
        """A colocation metro's edges are deployed into the metro directly."""
        assert VxlanSegmentGenerator._device_deployment_ids({"id": "metro-1"}) == ["metro-1"]
        assert VxlanSegmentGenerator._device_deployment_ids({"id": "metro-1", "children": None}) == ["metro-1"]
