"""Unit tests for VxlanSegmentGenerator.

A VXLAN segment needs generator-driven per-deployment realization
(SegmentDeployment) since it can stretch across multiple sites.

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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.protocols import ManagedSegmentDeployment
from generators.topology.segment import VxlanSegmentGenerator
from tests.unit import segment_generators_helpers
from tests.unit.segment_generators_helpers import _DEP_1, _DEP_2, _make_gen, _seg_response
from utils.limits import STRETCHED_VNI_POOL

# Autouse fixture, re-exported so pytest registers it in this module.
ensure_zone = segment_generators_helpers.ensure_zone


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

        assert gen.client.get.call_args.kwargs["name__value"] == STRETCHED_VNI_POOL
        pools = [c.kwargs["vni_pool"] for c in gen._activate_segment_in_deployment.call_args_list]
        assert pools == [{"id": "pool-global", "name": STRETCHED_VNI_POOL}] * 2

    def test_stretched_segment_without_global_pool_is_a_hard_error(self):
        """Missing GLOBAL-L2VNI fails the run instead of falling back to a site pool."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=None)
        gen._activate_segment_in_deployment = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1], stretch_scope="global")
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_not_called()
        assert STRETCHED_VNI_POOL in gen.logger.error.call_args[0][0]

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
        gen.reconcile_vxlan_segment.assert_awaited_once_with("seg-3", "vxlan-2000")
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

        office_no_pool = {"id": "office-1", "name": "C005-OFFICE", "vni_pool": None}
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

        office = {
            "id": "office-1",
            "name": "C005-OFFICE",
            "vni_pool": {"id": "pool-office", "name": "c005-office-vni-pool"},
        }
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[office])
        asyncio.run(gen.generate(data))

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        call = gen._activate_segment_in_deployment.call_args
        assert call.kwargs["deployment_id"] == "office-1"
        assert call.kwargs["deployment_name"] == "C005-OFFICE"
        assert call.kwargs["vni_pool"] == {"id": "pool-office", "name": "c005-office-vni-pool"}

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

    def test_routing_tag_is_ensured_after_the_group_tag_and_before_any_activation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The routing tag helper gets the cleaned segment (with its stored tag) and the client, once per run."""
        order: list[str] = []
        monkeypatch.setattr(
            "generators.topology.segment.ensure_segment_tag",
            AsyncMock(side_effect=lambda client, lock, *, segment: order.append("group tag")),
        )
        ensure_routing = AsyncMock(side_effect=lambda client, *, segment: order.append("routing tag"))
        monkeypatch.setattr("generators.topology.segment.ensure_segment_routing_tag", ensure_routing)
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock(side_effect=lambda **kw: order.append("activate"))
        gen._create_inline_sub_interfaces = AsyncMock()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        data["ManagedVxlanSegment"]["edges"][0]["node"]["routing_tag"] = {"value": 12}

        asyncio.run(gen.generate(data))

        assert order == ["group tag", "routing tag", "activate"]
        (call,) = ensure_routing.await_args_list
        assert call.args == (gen.client,)
        assert call.kwargs["segment"]["id"] == "seg-1"
        assert call.kwargs["segment"]["routing_tag"] == 12

    def test_routing_tag_failure_stops_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tag that cannot be drawn (pool not loaded) is not logged away: no deployment is activated."""
        monkeypatch.setattr("generators.topology.segment.ensure_segment_tag", AsyncMock())
        monkeypatch.setattr(
            "generators.topology.segment.ensure_segment_routing_tag",
            AsyncMock(side_effect=LookupError("Routing tag pool Segment-Routing-Tag not found")),
        )
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()

        with pytest.raises(LookupError, match="Segment-Routing-Tag"):
            asyncio.run(gen.generate(_seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])))

        gen._activate_segment_in_deployment.assert_not_awaited()

    def test_query_selects_the_tenant_test_and_the_segment_tag(self, root_dir: Path) -> None:
        """The fast path (tag present) and the routed-tenant test (namespace type) come from the query."""
        query = (root_dir / "queries/topology/add/vxlan_segment.gql").read_text()
        assert "security_tag { node { id } }" in query
        assert "routing_tag { value }" in query
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
        footprint is the hosting node (its parent is just the \"Campus and Office\" label, which hosts nothing)."""
        gen = _make_gen()
        office = {"id": "office-1", "name": "C005-OFFICE", "vni_pool": {"id": "pool-1", "name": "c005-office-vni-pool"}}
        assert gen._resolve_hosting_parent(office, "vxlan-1000") is office
        gen.logger.error.assert_not_called()

    def test_office_without_a_pool_yet_still_hosts_itself(self):
        """The key's presence decides, not its value: an office whose add_office_fabric has not run
        has vni_pool None, and that must not fall through to the (absent) parent."""
        gen = _make_gen()
        office = {"id": "office-1", "name": "C005-OFFICE", "vni_pool": None}
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

    def test_existing_deployment_resave_failure_stops_the_run(self):
        """The tracker re-save of an existing record is what keeps it out of this run's cleanup, which would
        delete it and, with it, the VLAN rows below it: a failed one fails the run."""
        gen = _make_gen()
        existing = _activation_node()
        existing.save = AsyncMock(side_effect=Exception("conflict"))

        with pytest.raises(GeneratorError, match="Failed to re-save"):
            self._run(gen, existing_deployment=existing)

        gen.client.create.assert_not_called()

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
            vni_pool={"id": "pool-global", "name": STRETCHED_VNI_POOL},
        )

        assert self._created_data(gen)["vni"] == 10100

    def test_stretched_first_deployment_allocates_from_given_pool(self):
        """The first stretched deployment allocates from the pool it is given (generate()
        passes GLOBAL-L2VNI)."""
        gen = self._gen()

        self._run(gen, vni_pool={"id": "pool-global", "name": "x"})

        assert self._created_data(gen)["vni"] == {"from_pool": {"id": "pool-global"}}


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
        """A metro with no zone or virtual colocation (no children came back) has only itself to look in."""
        assert VxlanSegmentGenerator._device_deployment_ids({"id": "metro-1"}) == ["metro-1"]
        assert VxlanSegmentGenerator._device_deployment_ids({"id": "metro-1", "children": None}) == ["metro-1"]

    def test_metro_includes_its_zones_and_virtual_colocations(self) -> None:
        """The edges are deployed into the metro's zones and virtual colocations, the metro owns no device: both
        kinds of child are looked in, after the metro itself."""
        metro = {"id": "metro-1", "children": [{"id": "zone-fr2"}, {"id": "zone-fr6"}, {"id": "vc-am-ne"}]}
        assert VxlanSegmentGenerator._device_deployment_ids(metro) == ["metro-1", "zone-fr2", "zone-fr6", "vc-am-ne"]

    def test_metro_skips_children_that_arrive_empty(self) -> None:
        """A child of another kind comes back as an empty node (ids are selected on zones and virtual
        colocations only) and is skipped."""
        metro = {"id": "metro-1", "children": [{}, {"id": "zone-fr2"}]}
        assert VxlanSegmentGenerator._device_deployment_ids(metro) == ["metro-1", "zone-fr2"]

    def test_vlan_domain_query_selects_zone_and_virtual_colocation_ids_under_the_metro(self, root_dir: Path) -> None:
        """The zone and virtual colocation filter lives in segment_vlan_domains.gql, not in Python."""
        query = (root_dir / "queries" / "topology" / "add" / "segment_vlan_domains.gql").read_text()
        compact = " ".join(query.split())
        assert "... on TopologyColocationMetro { children { edges { node {" in compact
        assert "... on TopologyColocationZone { id } ... on TopologyVirtualColocation { id }" in compact
