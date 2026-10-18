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
generate() reads the segment's existing deployments once (idempotency and the
stretched VNI to reuse; a failed read is fatal); _activate_segment_in_deployment()
then allocates VNI from the pool dict (or reuses the stretched segment's VNI)
and calls client.create() / save().

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
    # Tenant addressing has its own test class (TestEnsureSegmentAddressing).
    gen.ensure_segment_addressing = AsyncMock()
    return gen


@pytest.fixture(autouse=True)
def ensure_zone(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the shared zone helper (own tests: test_security_zone_helper.py).

    Keeps generate()-level tests focused on SegmentDeployment/interface/
    inline-sub-interface behavior; returned so tests can assert the call.
    """
    mock = AsyncMock(return_value="PROD-ZONE")
    monkeypatch.setattr("generators.topology.segment.ensure_security_zone", mock)
    return mock


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
    fragment in the real vxlan_segment query. An office entry has no "parent"
    but its own "vni_pool" key (value None when the pool is not set yet),
    mirroring `... on TopologyCustomerOffice { vni_pool }`.
    """

    def _pool_node(pool: dict | None) -> dict | None:
        if pool is None:
            return None
        return {"node": {"id": pool["id"], "name": {"value": pool["name"]}}}

    dep_edges = []
    for d in deployments:
        node: dict[str, Any] = {"id": d["id"], "name": {"value": d["name"]}}
        if "parent" in d:
            parent = d["parent"]
            parent_node: dict[str, Any] = {"id": parent["id"], "name": {"value": parent["name"]}}
            if "vni_pool" in parent:
                parent_node["vni_pool"] = _pool_node(parent["vni_pool"])
            node["parent"] = {"node": parent_node}
        if "vni_pool" in d:
            # `... on TopologyCustomerOffice { vni_pool }`: the footprint hosts itself.
            node["vni_pool"] = _pool_node(d["vni_pool"])
        dep_edges.append({"node": node})
    return {
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": seg_id,
                        "__typename": "ManagedVxlanSegment",
                        "name": {"value": seg_name},
                        "environment": {"value": "p"},
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

        gen.wait_for_parent_generator_and_refetch.assert_awaited_once_with(
            ("add_dc", "dc_pod_cascade", "add_colocation_metro", "add_office_fabric"), "dc-1"
        )

    def test_office_without_pool_waits_on_its_own_id(self):
        """An office hosts its own segments, so the wait is on the office itself (add_office_fabric
        creates its vni_pool) — not on its parent, which hosts nothing."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        office_no_pool = {"id": "office-1", "name": "C005-P", "vni_pool": None}
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[office_no_pool])
        asyncio.run(gen.generate(data))

        gen.wait_for_parent_generator_and_refetch.assert_awaited_once_with(
            ("add_dc", "dc_pod_cascade", "add_colocation_metro", "add_office_fabric"), "office-1"
        )

    def test_office_segment_is_activated_on_the_office_with_its_own_pool(self):
        """The SegmentDeployment goes on the office footprint and draws the VNI from its vni_pool."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock(return_value=True)
        gen._create_inline_sub_interfaces = AsyncMock()

        office = {"id": "office-1", "name": "C005-P", "vni_pool": {"id": "pool-office", "name": "c005-p-vni-pool"}}
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[office])
        asyncio.run(gen.generate(data))

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        call = gen._activate_segment_in_deployment.call_args
        assert call.kwargs["deployment_id"] == "office-1"
        assert call.kwargs["deployment_name"] == "C005-P"
        assert call.kwargs["vni_pool"] == {"id": "pool-office", "name": "c005-p-vni-pool"}

    def test_refetched_data_is_reparsed_when_parent_was_in_flight(self):
        """If add_dc was in-flight, the refreshed data (now carrying the
        vni_pool that was missing the first time) replaces the segment
        before target deployments are re-resolved."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        refreshed_payload = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed_payload])

        dep_no_pool = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[dep_no_pool])
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_awaited_once()
        assert gen._activate_segment_in_deployment.call_args.kwargs["vni_pool"] == {
            "id": "pool-vni-1",
            "name": "DC1-VNI-Pool",
        }

    def test_zone_helper_gets_the_segments_own_kind_environment_and_zone(self, ensure_zone: AsyncMock) -> None:
        """The kind is the segment's real typename and the environment is its own."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        node = data["ManagedVxlanSegment"]["edges"][0]["node"]
        node["environment"] = {"value": "d"}
        node["security_zone"] = {"node": {"name": {"value": "NONPROD-ZONE"}}}
        asyncio.run(gen.generate(data))

        ensure_zone.assert_awaited_once_with(
            gen.client,
            kind="ManagedVxlanSegment",
            segment_id="seg-1",
            environment="d",
            current_zone="NONPROD-ZONE",
        )

    def test_missing_environment_is_not_defaulted_to_production(self, ensure_zone: AsyncMock) -> None:
        """No environment reaches the helper as None, which fails closed there."""
        gen = _make_gen()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[])
        del data["ManagedVxlanSegment"]["edges"][0]["node"]["environment"]
        asyncio.run(gen.generate(data))

        assert ensure_zone.call_args.kwargs["environment"] is None

    def test_zone_helper_failure_stops_the_run(self, ensure_zone: AsyncMock) -> None:
        """A zone that cannot be set is not logged away: the run fails."""
        ensure_zone.side_effect = LookupError("Security zone PROD-ZONE not found")
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()

        with pytest.raises(LookupError):
            asyncio.run(gen.generate(_seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])))

        gen._activate_segment_in_deployment.assert_not_awaited()

    def test_segment_tag_helper_gets_the_segment_before_any_activation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The group tag is ensured once per run, with the run's lock, before a deployment is activated."""
        order: list[str] = []
        ensure_tag = AsyncMock(side_effect=lambda client, lock, *, segment: order.append(f"tag {segment['id']}"))
        monkeypatch.setattr("generators.topology.segment.ensure_segment_tag", ensure_tag)
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock(side_effect=lambda **kw: order.append("activate"))
        gen._create_inline_sub_interfaces = AsyncMock()

        asyncio.run(gen.generate(_seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])))

        assert order == ["tag seg-1", "activate"]
        (call,) = ensure_tag.await_args_list
        assert call.args == (gen.client, gen.resource_lock)
        assert call.kwargs["segment"]["typename"] == "ManagedVxlanSegment"

    def test_segment_tag_failure_stops_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tag that cannot be drawn is not logged away: no deployment is activated."""
        monkeypatch.setattr(
            "generators.topology.segment.ensure_segment_tag", AsyncMock(side_effect=LookupError("No group tag pool"))
        )
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()

        with pytest.raises(LookupError, match="No group tag pool"):
            asyncio.run(gen.generate(_seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])))

        gen._activate_segment_in_deployment.assert_not_awaited()

    def test_query_selects_the_tenant_test_and_the_segment_tag(self, root_dir: Path) -> None:
        """The fast path (tag present) and the routed-tenant test (namespace type) come from the query."""
        query = (root_dir / "queries/topology/add/vxlan_segment.gql").read_text()
        assert "security_tag { node { id } }" in query
        assert "namespace_type { value }" in query

    def test_zone_runs_even_when_no_deployments_resolved(self, ensure_zone: AsyncMock) -> None:
        """Zone classification is a property of the segment itself, not of
        whether its hosting parent could be resolved."""
        gen = _make_gen()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[])
        asyncio.run(gen.generate(data))

        ensure_zone.assert_awaited_once()


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

    def test_office_hosts_itself(self):
        """CustomerOffice is TopologySegmentHosting: its vni_pool is on the footprint, so the
        footprint is the hosting node (its parent, the OfficeCustomer, hosts nothing)."""
        gen = _make_gen()
        office = {"id": "office-1", "name": "C005-P", "vni_pool": {"id": "pool-1", "name": "c005-p-vni-pool"}}
        assert gen._resolve_hosting_parent(office, "vxlan-1000") is office
        gen.logger.error.assert_not_called()

    def test_office_without_a_pool_yet_still_hosts_itself(self):
        """The key's presence decides, not its value: an office whose add_office_fabric has not run
        has vni_pool None, and that must not fall through to the (absent) parent."""
        gen = _make_gen()
        office = {"id": "office-1", "name": "C005-P", "vni_pool": None}
        assert gen._resolve_hosting_parent(office, "vxlan-1000") is office
        gen.logger.error.assert_not_called()

    def test_dc_footprint_keeps_resolving_to_its_parent(self):
        """A CustomerDC carries no vni_pool of its own: the pool is on its parent."""
        gen = _make_gen()
        dep = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "vni_pool": {"id": "p"}}}
        assert gen._resolve_hosting_parent(dep, "vxlan-1000") == {"id": "dc-1", "vni_pool": {"id": "p"}}

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


def _activation_node() -> MagicMock:
    """A fresh SegmentDeployment node whose save() is awaitable."""
    node = MagicMock()
    node.save = AsyncMock()
    return node


class TestActivateSegmentInDeployment:
    """Tests for _activate_segment_in_deployment on VxlanSegmentGenerator.

    VNI-and-status only now — LOCAL VLAN ID realization moved to a separate
    per-VLAN-domain mechanism (ManagedVlanDomainSegment), tested in
    TestAssignSegmentToDcInterfaces below. The method does no reads of its
    own: generate() hands it the deployment's existing record and the VNI to
    reuse.
    """

    # Common invocation kwargs used in every test
    _CALL = dict(
        segment_id="seg-1",
        segment_name="vxlan-1000",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen, **overrides) -> bool:
        return asyncio.run(gen._activate_segment_in_deployment(**{**self._CALL, **overrides}))

    def test_idempotent_existing_deployment_skips_create(self):
        """A passed-in existing record is only re-saved: no create, no query."""
        gen = _make_gen()
        existing = _activation_node()

        assert self._run(gen, existing_deployment=existing) is True

        gen.client.create.assert_not_called()
        gen.client.filters.assert_not_called()
        existing.save.assert_called_once()
        info_msgs = " ".join(str(c) for c in gen.logger.info.call_args_list)
        assert "already exists" in info_msgs

    def test_existing_deployment_resave_failure_is_not_a_failed_activation(self):
        """The tracker re-save of an existing record is best effort: it warns, the activation stands."""
        gen = _make_gen()
        existing = _activation_node()
        existing.save = AsyncMock(side_effect=Exception("conflict"))

        assert self._run(gen, existing_deployment=existing) is True

        gen.client.create.assert_not_called()
        assert "Failed to re-save" in gen.logger.warning.call_args[0][0]

    def test_creates_segment_deployment(self):
        """Happy path: no existing record → create() called with segment/deployment/status only."""
        gen = _make_gen()
        activation = _activation_node()
        gen.client.create = AsyncMock(return_value=activation)

        assert self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"}) is True

        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == ManagedSegmentDeployment
        call_data = call_kwargs["data"]

        assert call_data["segment"] == {"id": "seg-1"}
        assert call_data["deployment"] == {"id": "dc-1"}
        assert "vlan_id" not in call_data
        assert call_data["vni"]["from_pool"]["id"] == "pool-vni"
        activation.save.assert_called_once()
        gen.client.filters.assert_not_called()

    def test_create_exception_logs_error_and_reports_failure(self):
        """client.create() raising an exception results in an error log and a False result."""
        gen = _make_gen()
        gen.client.create = AsyncMock(side_effect=Exception("network timeout"))

        assert self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"}) is False

        assert "Failed to create" in gen.logger.error.call_args[0][0]


# ===========================================================================
# TestVxlanVniAllocation
# ===========================================================================


class TestVxlanVniAllocation:
    """VNI choice inside _activate_segment_in_deployment (no reads: pure decision)."""

    _CALL = dict(
        segment_id="seg-2",
        segment_name="vxlan-1001",
        deployment_id="dc-1",
        deployment_name="DC-1",
    )

    def _run(self, gen, **overrides) -> None:
        asyncio.run(gen._activate_segment_in_deployment(**{**self._CALL, **overrides}))

    def _created_data(self, gen) -> dict[str, Any]:
        return gen.client.create.call_args.kwargs["data"]

    def _gen(self) -> Any:
        gen = _make_gen()
        gen.client.create = AsyncMock(return_value=_activation_node())
        return gen

    def test_local_segment_allocates_from_its_own_pool(self):
        """A local segment draws from the pool it is given; the from_pool dict carries nothing else (a Number
        pool ignores sibling keys such as identifier)."""
        gen = self._gen()

        self._run(gen, vni_pool={"id": "pool-vni", "name": "DC1-VNI-Pool"})

        data = self._created_data(gen)
        assert data["vni"] == {"from_pool": {"id": "pool-vni"}}

    def test_no_vni_pool_logs_warning_but_still_creates(self):
        """If no vni_pool is found (and no prior VNI to reuse), a warning is logged
        but client.create() is still called — 'vni' is simply absent from call_data."""
        gen = self._gen()

        self._run(gen, vni_pool=None)

        assert "vni_pool" in " ".join(str(c) for c in gen.logger.warning.call_args_list)
        gen.client.create.assert_called_once()
        assert "vni" not in self._created_data(gen)

    def test_stretched_reuses_the_given_vni_and_ignores_the_pool(self):
        """A reusable VNI is written as a literal: the pool is not drawn from again."""
        gen = self._gen()

        self._run(
            gen,
            reusable_vni=10100,
            vni_pool={"id": "pool-global", "name": STRETCHED_VNI_POOL_NAME},
        )

        assert self._created_data(gen)["vni"] == 10100

    def test_stretched_first_deployment_allocates_from_given_pool(self):
        """The first stretched deployment allocates from the pool it is given (generate()
        passes GLOBAL-L2VNI)."""
        gen = self._gen()

        self._run(gen, vni_pool={"id": "pool-global", "name": "x"})

        assert self._created_data(gen)["vni"] == {"from_pool": {"id": "pool-global"}}


# ===========================================================================
# TestSegmentVniInvariant — behaviour of generate() against a stateful fake
# ===========================================================================


class _FakeSegmentDeploymentStore:
    """In-memory ManagedSegmentDeployment store behind client.filters/create.

    A node created with a pool request gets the next pool number (a new
    consumer id draws a new number, as on a real Number pool); a literal VNI is
    stored as written. ``pool_draws`` counts pool allocations.
    """

    def __init__(self, first_vni: int = 10100) -> None:
        self.records: list[MagicMock] = []
        self.created_payloads: list[dict[str, Any]] = []
        self.pool_draws = 0
        self.filter_calls = 0
        self._next_vni = first_vni

    def seed(self, deployment_id: str, vni: int | None) -> None:
        self._store(deployment_id, vni)

    def _store(self, deployment_id: str, vni: int | None) -> MagicMock:
        record = MagicMock()
        record.deployment.id = deployment_id
        record.vni.value = vni
        record.save = AsyncMock()
        self.records.append(record)
        return record

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        assert kind == ManagedSegmentDeployment
        self.filter_calls += 1
        return list(self.records)

    async def create(self, *, kind: Any, data: dict[str, Any]) -> MagicMock:
        assert kind == ManagedSegmentDeployment
        self.created_payloads.append(data)
        vni = data.get("vni")
        if isinstance(vni, dict):
            self.pool_draws += 1
            vni = self._next_vni
            self._next_vni += 1
        record = self._store(data["deployment"]["id"], vni)
        # Persist only on save(), like the SDK: a created-but-unsaved node is invisible.
        self.records.remove(record)

        async def _save(**_: Any) -> None:
            self.records.append(record)

        record.save = AsyncMock(side_effect=_save)
        return record

    def vnis(self) -> dict[str, int | None]:
        return {r.deployment.id: r.vni.value for r in self.records}


def _stateful_gen(store: _FakeSegmentDeploymentStore) -> Any:
    gen = _make_gen()
    gen.client.filters = AsyncMock(side_effect=store.filters)
    gen.client.create = AsyncMock(side_effect=store.create)
    gen.client.get = AsyncMock(return_value=MagicMock(id="pool-global"))
    gen._create_inline_sub_interfaces = AsyncMock()
    return gen


class TestSegmentVniInvariant:
    """A stretched segment carries ONE VNI, a re-run allocates nothing, a first activation allocates one."""

    def _stretched(self) -> dict:
        return _seg_response(
            seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2], stretch_scope="global"
        )

    def test_stretched_segment_over_two_deployments_gets_one_vni(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.vnis() == {"dc-1": 10100, "dc-2": 10100}
        assert store.pool_draws == 1

    def test_rerun_of_a_stretched_segment_allocates_nothing_new(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        asyncio.run(gen.generate(self._stretched()))
        created_before = len(store.created_payloads)

        asyncio.run(gen.generate(self._stretched()))

        assert len(store.created_payloads) == created_before
        assert store.pool_draws == 1
        assert store.vnis() == {"dc-1": 10100, "dc-2": 10100}

    def test_stretched_activation_adding_a_second_deployment_reuses_the_existing_vni(self):
        """Only the first site exists (seeded): the new site takes its VNI as a literal."""
        store = _FakeSegmentDeploymentStore()
        store.seed("dc-1", 10555)
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.pool_draws == 0
        assert store.created_payloads[0]["vni"] == 10555
        assert store.vnis() == {"dc-1": 10555, "dc-2": 10555}

    def test_stretched_segment_reads_the_store_once_up_front_and_once_after_the_draw(self):
        """No per-deployment re-queries: the prefetch plus one read-back of the freshly drawn VNI."""
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.filter_calls == 2

    def test_rerun_of_a_stretched_segment_reads_the_store_once(self):
        store = _FakeSegmentDeploymentStore()
        store.seed("dc-1", 10100)
        store.seed("dc-2", 10100)
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.filter_calls == 1
        assert store.created_payloads == []

    def test_first_local_activation_allocates_exactly_one_vni_per_deployment_from_its_own_pool(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])

        asyncio.run(gen.generate(data))

        assert [p["vni"]["from_pool"]["id"] for p in store.created_payloads] == ["pool-vni-1", "pool-vni-2"]
        assert store.pool_draws == 2
        assert store.filter_calls == 1

    def test_local_rerun_allocates_nothing_new(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])
        asyncio.run(gen.generate(data))

        asyncio.run(gen.generate(data))

        assert len(store.created_payloads) == 2
        assert store.pool_draws == 2

    def test_local_segment_never_reuses_another_sites_vni(self):
        """A VNI already on another site's deployment is not written as a literal on a local segment."""
        store = _FakeSegmentDeploymentStore()
        store.seed("dc-1", 10100)
        gen = _stateful_gen(store)
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])

        asyncio.run(gen.generate(data))

        payload = store.created_payloads[0]
        assert payload["deployment"] == {"id": "dc-2"}
        assert payload["vni"]["from_pool"]["id"] == "pool-vni-2"

    def test_unreadable_existing_deployments_fail_the_run_before_any_write(self):
        """A blind prefetch would allocate a second VNI / duplicate records: fatal, nothing written."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(side_effect=Exception("graphql timeout"))
        gen.client.create = AsyncMock()
        gen._activate_segment_in_deployment = AsyncMock()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])

        with pytest.raises(GeneratorError, match="existing SegmentDeployments"):
            asyncio.run(gen.generate(data))

        gen.client.create.assert_not_called()
        gen._activate_segment_in_deployment.assert_not_called()

    def test_unreadable_vni_read_back_fails_instead_of_drawing_a_second_vni(self):
        """If the read-back after the first draw fails, the run stops before the second site allocates."""
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        calls = {"n": 0}

        async def _flaky(*, kind: Any, **kwargs: Any) -> list[Any]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise Exception("graphql timeout")
            return await store.filters(kind=kind, **kwargs)

        gen.client.filters = AsyncMock(side_effect=_flaky)

        with pytest.raises(Exception, match="graphql timeout"):
            asyncio.run(gen.generate(self._stretched()))

        assert store.pool_draws == 1
        assert len(store.created_payloads) == 1


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
        assert call_kwargs["data"]["vlan_id"] == {"from_pool": {"id": "pool-vlan-1"}}
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
        gen._fetch_segment_vlan_state = AsyncMock(return_value={"segment": {"capabilities": tagged}, "activations": {}})
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
            relation_to_update="capabilities", related_nodes=["bl-fw-new"]
        )
        gen.segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="capabilities", related_nodes=["bl-fw-old"]
        )

    def test_terminate_inline_off_untags_every_service_port(self) -> None:
        """Turning terminate_inline off leaves no border service port tagged."""
        gen = self._gen([{"id": "bl-fw-old", "typename": "DcimPhysicalInterface", "role": "firewall"}])

        legs = asyncio.run(gen._reconcile_inline_service_ports({**_INLINE_SEGMENT, "terminate_inline": False}))

        assert legs == []
        gen._inline_legs.assert_not_awaited()
        gen.segment_obj.add_relationships.assert_not_awaited()
        gen.segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="capabilities", related_nodes=["bl-fw-old"]
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
