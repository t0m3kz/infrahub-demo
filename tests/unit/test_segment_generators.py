"""Unit tests for VxlanSegmentGenerator.

VlanSegment has no generator — vlan_id is a plain manual attribute
(single-site, no pool allocation). Only VXLAN needs generator-driven
per-deployment realization (SegmentDeployment) since it can stretch
across multiple sites.

VxlanSegmentGenerator.generate() cleans GraphQL data, guards on missing
id/name/customer_deployments, resolves each customer footprint to its
hosting parent (TopologyDataCenter or TopologyColocationMetro) via
_resolve_hosting_parent(), then calls _activate_segment_in_deployment()
for each resolved parent (VNI-and-status only — LOCAL VLAN ID realization
moved to a separate per-VLAN-domain mechanism, see
_assign_segment_to_dc_interfaces/ManagedVlanDomainSegment), followed by
interface assignment and inline sub-interface creation.

vni_pool is read straight from the vxlan_segment query's
TopologySegmentHosting parent fragment (queries/topology/add/vxlan_segment.gql)
— no separate client.get() round-trip per deployment. _activate_segment_in_deployment()
does an idempotency check via client.filters(), allocates VNI from that
pool dict, then calls client.create() / save().

Tests use asyncio.run() directly — same pattern as test_circuit_generators.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import ManagedSegmentDeployment, ManagedVxlanSegment, SecurityZone
from generators.topology.segment import VxlanSegmentGenerator

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
    # PoolMixin.upsert_number_pool — used by the standalone-VLAN-domain path
    # in _assign_segment_to_dc_interfaces, not under test in this file.
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vlan-pool-1"))
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
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()
        data = {"ManagedVxlanSegment": {"edges": []}}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "No ManagedVxlanSegment" in gen.logger.error.call_args[0][0]

    def test_missing_segment_id_logs_error(self):
        gen = _make_gen()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()
        # id="" triggers the "missing id or name" guard
        data = _seg_response(seg_id="", seg_name="vxlan-1000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called_once()
        assert "missing id or name" in gen.logger.error.call_args[0][0]

    def test_no_deployments_logs_error(self):
        gen = _make_gen()
        gen._assign_to_deployment_interfaces = AsyncMock()
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
        gen._assign_to_deployment_interfaces = AsyncMock()
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
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))

        call = gen._activate_segment_in_deployment.call_args
        assert call.kwargs["vni_pool"] == {"id": "pool-vni-1", "name": "DC1-VNI-Pool"}

    def test_deployment_missing_id_is_skipped(self):
        """A customer deployment entry with id='' is skipped; only the valid one triggers activation."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
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

    def test_generate_calls_interface_assignment_and_inline_creation(self):
        """generate() calls _activate_segment_in_deployment, _assign_to_deployment_interfaces,
        and _create_inline_sub_interfaces."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        data = _seg_response(seg_id="seg-3", seg_name="vxlan-2000", deployments=[_DEP_1])
        asyncio.run(gen.generate(data))

        gen._activate_segment_in_deployment.assert_awaited_once()
        gen._assign_to_deployment_interfaces.assert_awaited_once()
        gen._create_inline_sub_interfaces.assert_awaited_once()

    def test_missing_vni_pool_waits_on_parent_dc_generators(self):
        """A target deployment with no vni_pool (its own add_dc/dc_pod_cascade
        hasn't run yet) triggers a wait on both parent generators before
        _resolve_target_deployments is retried."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        dep_no_pool = {"id": "cust-1", "name": "C001-P-DC1", "parent": {"id": "dc-1", "name": "DC-1"}}
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[dep_no_pool])
        asyncio.run(gen.generate(data))

        assert gen.wait_for_parent_generator_and_refetch.await_args_list == [
            (("add_dc", "dc-1"), {}),
            (("dc_pod_cascade", "dc-1"), {}),
        ]

    def test_refetched_data_is_reparsed_when_parent_was_in_flight(self):
        """If add_dc was in-flight, the refreshed data (now carrying the
        vni_pool that was missing the first time) replaces the segment
        before target deployments are re-resolved."""
        gen = _make_gen()
        gen._activate_segment_in_deployment = AsyncMock()
        gen._assign_to_deployment_interfaces = AsyncMock()
        gen._create_inline_sub_interfaces = AsyncMock()

        refreshed_payload = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed_payload, None])

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
        gen._assign_to_deployment_interfaces = AsyncMock()
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
        gen._assign_to_deployment_interfaces = AsyncMock()
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

    def test_reuses_existing_vni_from_other_deployment(self):
        """When another DC already has a SegmentDeployment with a VNI, that value
        is reused as a literal integer — no pool allocation for VNI."""
        gen = _make_gen()

        existing_dep = MagicMock()
        existing_dep.resolve = AsyncMock()
        existing_dep.vni = MagicMock()
        existing_dep.vni.value = 10100

        # First call: idempotency check → no existing for this dc
        # Second call: VNI reuse check → one existing SegmentDeployment with VNI
        gen.client.filters = AsyncMock(side_effect=[[], [existing_dep]])

        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)

        self._run(gen)

        call_data = gen.client.create.call_args.kwargs["data"]
        assert call_data["vni"] == 10100

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

    def test_stretched_allocates_from_local_pool_with_shared_identifier(self):
        """First stretched deployment allocates from local vni_pool using shared segment identifier."""
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


class TestEnsureVlanDomainSegment:
    """Tests for _ensure_vlan_domain_segment — per-(segment, domain) allocation."""

    def test_existing_record_skips_allocation(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[MagicMock()])

        asyncio.run(gen._ensure_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        gen.client.create.assert_not_called()

    def test_domain_without_pool_logs_error(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        domain_obj = MagicMock()
        domain_obj.vlan_pool = None
        gen.client.get = AsyncMock(return_value=domain_obj)

        asyncio.run(gen._ensure_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        gen.client.create.assert_not_called()
        error_msg = gen.logger.error.call_args[0][0]
        assert "vlan_pool" in error_msg

    def test_allocates_vlan_id_from_domain_pool(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        domain_obj = MagicMock()
        domain_obj.vlan_pool = MagicMock(id="pool-vlan-1")
        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=domain_obj)
        gen.client.create = AsyncMock(return_value=activation)

        asyncio.run(gen._ensure_vlan_domain_segment("seg-1", "vxlan-1000", "mlag-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["segment"] == {"id": "seg-1"}
        assert call_kwargs["data"]["vlan_domain"] == {"id": "mlag-1"}
        assert call_kwargs["data"]["vlan_id"]["from_pool"]["id"] == "pool-vlan-1"
        activation.save.assert_called_once()


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
        segment_obj.save.assert_awaited_once_with(allow_upsert=True)

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

    def test_non_production_codes_all_map_to_nonprod_zone(self):
        for environment in ("n", "s", "d", "t"):
            gen = self._make_gen()
            gen.client.filters = AsyncMock(return_value=[MagicMock(id="zone-1")])
            gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))

            asyncio.run(
                gen._ensure_security_zone(segment_id="seg-1", segment_name="vxlan-1000", environment=environment)
            )

            gen.client.filters.assert_awaited_once_with(kind=SecurityZone, name__value="NONPROD-ZONE")

    def test_segment_update_failure_is_logged_not_raised(self):
        gen = self._make_gen()
        gen.client.filters = AsyncMock(return_value=[MagicMock(id="zone-1")])
        gen.client.create = AsyncMock(side_effect=Exception("boom"))

        asyncio.run(gen._ensure_security_zone(segment_id="seg-1", segment_name="vxlan-1000", environment="p"))

        gen.logger.warning.assert_called_once()
