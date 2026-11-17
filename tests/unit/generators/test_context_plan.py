"""The plan of a service context and its legs (generators/customers/context_plan.py) as data, and against the schema."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.capabilities.ha import EDGE_CONTEXT, FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.customers.context_plan import (
    VIP_POOL_KEY,
    ExistingLegPrefix,
    LegInterface,
    LegsReads,
    context_vrf,
    exchange_specs_of,
    legs_of,
    needs_snat,
    plan_context,
    plan_legs,
)
from generators.customers.firewall_context_spec import ContextTarget, TransitMember
from generators.plan import (
    AddressAllocationSpec,
    EdgesSpec,
    Hfid,
    NodeRef,
    NodeSpec,
    PrefixAllocationSpec,
    PruneEdgesSpec,
    TopologyPlan,
)
from generators.schema_check import candidates_missing_mandatory, check_nodes, missing_mandatory
from tests.unit.schema_fakes import real_schema

ZONES = {"prod": "PROD", "internet": "INTERNET", "management": "MANAGEMENT", "non_prod": "NONPROD"}
VRFS = {(border, zone): f"vrf-{zone}" for border in ("bl-1", "bl-2") for zone in ZONES.values()}


def namespaces(*types: str) -> dict[str, Any]:
    return {kind: SimpleNamespace(id=f"zone-{kind}", name=SimpleNamespace(value=ZONES[kind])) for kind in types}


def target(*, track: bool = False, edge_hosted: bool = False, spec: Any = FIREWALL_CONTEXT) -> ContextTarget:
    return ContextTarget(
        spec=spec,
        name_base="DC10-FW1-FW2",
        deployment_id="dc10-id",
        tenant_id="cust-1" if track else None,
        track=track,
        devices=[{"id": "fw-1", "name": "FW1"}, {"id": "fw-2", "name": "FW2"}],
        parent_name="DC10",
        edge_hosted=edge_hosted,
    )


def members() -> list[TransitMember]:
    return [
        TransitMember(
            name=name,
            device_id=name,
            trunk=SimpleNamespace(id=f"up-{name}", name=SimpleNamespace(value="Ethernet1/25")),
            bl_port=SimpleNamespace(device=SimpleNamespace(id=border)),
            offset=offset,
        )
        for name, border, offset in (("fw-1", "bl-1", 5), ("fw-2", "bl-2", 6))
    ]


def context(**fields: Any) -> Any:
    """The stored context: id, name, VLAN and what it points at."""
    stored: dict[str, Any] = {
        "id": "ctx-1",
        "name": SimpleNamespace(value="shared-prod"),
        "vlan_id": SimpleNamespace(value=3000),
    }
    return SimpleNamespace(**{**stored, **fields})


def reads(**overrides: Any) -> LegsReads:
    values: dict[str, Any] = {
        "context": context(),
        "vlan_id": 3000,
        "pool_names": {
            "prod": "FW-Transit-PROD-IPv4",
            "internet": "FW-Transit-INTERNET-IPv4",
            "management": "FW-Transit-MANAGEMENT-IPv4",
        },
        "existing_prefixes": {},
        "interfaces": {},
    }
    return LegsReads(**{**values, **overrides})


def build(
    *,
    tracked: bool = False,
    spec: Any = FIREWALL_CONTEXT,
    legs: tuple[str, ...] = ("prod", "internet", "management"),
    **overrides: Any,
) -> TopologyPlan:
    plan = TopologyPlan(root_id=None)
    plan_legs(
        plan,
        spec=spec,
        target=target(track=tracked, spec=spec),
        reads=reads(**overrides),
        tenant_type="prod",
        members=members(),
        namespaces=namespaces("prod", "internet", "management"),
        leg_types=legs,
        vrfs=VRFS,
    )
    plan.validate()
    return plan


def nodes(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if getattr(spec.kind, "__name__", spec.kind) == kind]


class TestPlanContext:
    def test_a_new_shared_context_draws_its_vlan_and_is_shared(self) -> None:
        plan = TopologyPlan(root_id=None)

        plan_context(
            plan, spec=FIREWALL_CONTEXT, target=target(), name="shared-prod", existing=None, vlan_pool_id="pool-1"
        )

        (spec,) = plan.of(NodeSpec)
        assert spec.data == {
            "name": "shared-prod",
            "deployment": {"id": "dc10-id"},
            "shared_devices": [{"id": "fw-1"}, {"id": "fw-2"}],
            "vlan_id": {"from_pool": {"id": "pool-1"}},
        }
        assert (spec.ownership, spec.sequential, spec.hfid) == (
            "shared",
            True,
            Hfid("ManagedFirewallContext", ("shared-prod",)),
        )

    def test_a_dedicated_context_carries_its_tenant_and_is_owned(self) -> None:
        plan = TopologyPlan(root_id=None)

        plan_context(
            plan,
            spec=LOADBALANCER_CONTEXT,
            target=target(track=True),
            name="c-prod",
            existing=None,
            vlan_pool_id="pool-1",
        )

        (spec,) = plan.of(NodeSpec)
        assert spec.data["tenant"] == {"id": "cust-1"}
        assert (spec.kind, spec.ownership) == (LOADBALANCER_CONTEXT.context_kind, "owned")

    def test_a_context_that_has_its_vlan_is_updated_by_id_and_draws_nothing(self) -> None:
        plan = TopologyPlan(root_id=None)

        plan_context(
            plan, spec=FIREWALL_CONTEXT, target=target(), name="shared-prod", existing=context(), vlan_pool_id=None
        )

        (spec,) = plan.of(NodeSpec)
        assert spec.data["id"] == "ctx-1" and "vlan_id" not in spec.data and not spec.sequential

    def test_a_context_without_a_vlan_is_given_one_under_its_id(self) -> None:
        plan = TopologyPlan(root_id=None)

        plan_context(
            plan,
            spec=FIREWALL_CONTEXT,
            target=target(),
            name="shared-prod",
            existing=context(vlan_id=SimpleNamespace(value=None)),
            vlan_pool_id="pool-1",
        )

        (spec,) = plan.of(NodeSpec)
        assert spec.data["id"] == "ctx-1" and spec.data["vlan_id"] == {"from_pool": {"id": "pool-1"}}

    def test_a_context_that_needs_a_vlan_and_has_no_pool_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no VLAN and no pool"):
            plan_context(
                TopologyPlan(root_id=None),
                spec=FIREWALL_CONTEXT,
                target=target(),
                name="x",
                existing=None,
                vlan_pool_id=None,
            )


class TestLegsOfAContext:
    def test_the_context_name_is_scoped_by_ownership(self) -> None:
        assert target().context_name("PROD") == "shared-DC10-FW1-FW2-prod"
        assert target(track=True).context_name("PROD") == "DC10-FW1-FW2-prod"

    def test_an_edge_pair_takes_the_internet_leg_from_the_customers_contexts(self) -> None:
        assert legs_of(FIREWALL_CONTEXT, target(), "prod") == ("prod", "internet", "management")
        assert legs_of(FIREWALL_CONTEXT, target(edge_hosted=True), "prod") == ("prod", "management")
        assert legs_of(EDGE_CONTEXT, target(spec=EDGE_CONTEXT), "prod") == ("prod", "internet")
        assert legs_of(LOADBALANCER_CONTEXT, target(), "non_prod") == ("non_prod",)

    def test_the_snat_address_follows_the_internet_leg_or_a_dedicated_context_behind_an_edge(self) -> None:
        assert needs_snat(target(), ("prod", "internet"))
        assert not needs_snat(target(), ("prod", "management"))
        assert not needs_snat(target(edge_hosted=True), ("prod", "management"))
        assert needs_snat(target(track=True, edge_hosted=True), ("prod", "management"))

    def test_one_exchange_per_peer_of_the_tenant(self) -> None:
        assert exchange_specs_of(
            "ctx", "prod", ("prod", "internet", "management"), namespaces("prod", "internet", "management")
        ) == [
            ("ctx-PROD-INTERNET", "prod", "internet"),
            ("ctx-PROD-MANAGEMENT", "prod", "management"),
        ]

    def test_the_border_attachments_must_share_one_instance_per_zone(self) -> None:
        assert context_vrf(members(), VRFS, "PROD") == "vrf-PROD"
        with pytest.raises(ValueError, match="missing or conflicting VRF instances for PROD"):
            context_vrf(members(), {**VRFS, ("bl-2", "PROD"): "vrf-other"}, "PROD")
        with pytest.raises(ValueError, match="missing or conflicting VRF instances for PROD"):
            context_vrf(members(), {key: value for key, value in VRFS.items() if key != ("bl-2", "PROD")}, "PROD")


class TestPlanLegs:
    def test_a_fresh_shared_context_plans_the_snat_a_prefix_per_leg_and_the_legs(self) -> None:
        plan = build()

        (snat,) = plan.of(AddressAllocationSpec)
        assert (snat.identifier, snat.prefix_length, snat.pool_name, snat.ownership) == (
            "ctx-1-snat",
            32,
            "FW-SNAT-INTERNET-IPv4",
            "shared",
        )
        refs = next(spec for spec in plan.of(NodeSpec) if spec.key == "context-refs")
        assert refs.data == {"id": "ctx-1", "snat_address": NodeRef("snat")} and refs.ownership == "shared"
        prefixes = {spec.key: spec for spec in plan.of(PrefixAllocationSpec)}
        assert sorted(prefixes) == ["leg:internet", "leg:management", "leg:prod"]
        prod = prefixes["leg:prod"]
        assert (prod.identifier, prod.prefix_length, prod.pool_name, prod.role, prod.member_type) == (
            "ctx-1-PROD-transit",
            29,
            "FW-Transit-PROD-IPv4",
            "technical",
            "address",
        )
        assert prod.address_offsets == (1, 4, 5, 6) and prod.address_ownership == "shared"

    def test_the_legs_of_each_member_sit_on_its_uplink_in_the_instance_of_the_leg(self) -> None:
        plan = build()

        subs = {spec.key: spec for spec in nodes(plan, "DcimVirtualInterface")}
        assert sorted(subs) == sorted(
            f"sub:{member}:{leg}" for member in ("fw-1", "fw-2") for leg in ("prod", "internet", "management")
        )
        sub = subs["sub:fw-2:internet"]
        assert sub.data == {
            "name": "Ethernet1/25.3400",
            "device": {"id": "fw-2"},
            "status": "active",
            "role": "service",
            "ip_address": NodeRef("leg:internet#6"),
            "parent": {"id": "up-fw-2"},
            "vlan_id": 3400,
        }
        assert sub.hfid == Hfid("DcimVirtualInterface", ("fw-2", "Ethernet1/25.3400"))
        adds = {spec.node: spec.peers for spec in plan.of(EdgesSpec) if spec.relation == "capabilities"}
        assert adds[NodeRef("sub:fw-2:internet")] == ("vrf-INTERNET",)

    def test_one_exchange_per_peer_between_the_instances_of_its_legs(self) -> None:
        plan = build()

        exchanges = {spec.data["name"]: spec.data for spec in nodes(plan, "TopologyRoutedExchange")}
        assert exchanges == {
            "shared-prod-PROD-INTERNET": {
                "name": "shared-prod-PROD-INTERNET",
                "vrf_a": {"id": "vrf-PROD"},
                "vrf_z": {"id": "vrf-INTERNET"},
                "gateway": {"id": "ctx-1"},
                "status": "active",
            },
            "shared-prod-PROD-MANAGEMENT": {
                "name": "shared-prod-PROD-MANAGEMENT",
                "vrf_a": {"id": "vrf-PROD"},
                "vrf_z": {"id": "vrf-MANAGEMENT"},
                "gateway": {"id": "ctx-1"},
                "status": "active",
            },
        }

    def test_the_context_joins_the_service_contexts_group_last_by_hfid(self) -> None:
        plan = build()

        join = plan.of(EdgesSpec)[-1]
        assert (join.node, join.relation, join.peers) == (
            "ctx-1",
            "member_of_groups",
            (Hfid("CoreStandardGroup", ("service_contexts",)),),
        )

    def test_a_context_with_its_snat_and_vip_plans_neither(self) -> None:
        plan = build(context=context(snat_address=SimpleNamespace(id="snat-1")))

        assert plan.of(AddressAllocationSpec) == []
        assert all(spec.key != "context-refs" for spec in plan.of(NodeSpec))

    def test_a_dedicated_context_owns_everything_it_plans(self) -> None:
        plan = build(tracked=True)

        assert {spec.ownership for spec in plan.of(NodeSpec)} == {"owned", "shared"} - {"shared"}
        assert {spec.address_ownership for spec in plan.of(PrefixAllocationSpec)} == {"owned"}
        assert [spec.ownership for spec in plan.of(AddressAllocationSpec)] == ["shared"]  # the pool owns an allocation

    def test_a_load_balancer_context_plans_its_vip_range_and_one_leg_without_exchange(self) -> None:
        plan = build(
            spec=LOADBALANCER_CONTEXT,
            legs=("prod",),
            context=context(),
            pool_names={"prod": "LB-Leg-PROD-IPv4", VIP_POOL_KEY: "LB-VIP-PROD-IPv4"},
        )

        vip = next(spec for spec in plan.of(PrefixAllocationSpec) if spec.key == VIP_POOL_KEY)
        assert (vip.identifier, vip.prefix_length, vip.pool_name, vip.member_type) == (
            "ctx-1-PROD-lb-vip",
            26,
            "LB-VIP-PROD-IPv4",
            "address",
        )
        leg = next(spec for spec in plan.of(PrefixAllocationSpec) if spec.key == "leg:prod")
        assert leg.identifier == "ctx-1-PROD-lb-leg"
        assert nodes(plan, "TopologyRoutedExchange") == []
        assert plan.of(AddressAllocationSpec) == []
        refs = next(spec for spec in plan.of(NodeSpec) if spec.key == "context-refs")
        assert refs.data == {"id": "ctx-1", "vip_prefix": NodeRef(VIP_POOL_KEY)}

    def test_a_leg_that_carries_a_prefix_already_upserts_its_addresses_instead_of_allocating(self) -> None:
        existing = ExistingLegPrefix(
            network="100.66.0.40/29", namespace_id="ns-default", namespace_name="default", address_ids={5: "ip-5"}
        )

        plan = build(existing_prefixes={"prod": existing})

        assert sorted(spec.key for spec in plan.of(PrefixAllocationSpec)) == ["leg:internet", "leg:management"]
        addresses = {spec.key: spec for spec in nodes(plan, "IpamIPAddress")}
        assert sorted(addresses) == ["leg:prod#1", "leg:prod#4", "leg:prod#5", "leg:prod#6"]
        assert addresses["leg:prod#5"].data == {
            "id": "ip-5",
            "address": "100.66.0.45/29",
            "ip_namespace": {"id": "ns-default"},
        }
        assert addresses["leg:prod#6"].data == {"address": "100.66.0.46/29", "ip_namespace": {"id": "ns-default"}}
        assert addresses["leg:prod#6"].hfid == Hfid("IpamIPAddress", ("100.66.0.46/29", "default"))
        sub = next(spec for spec in nodes(plan, "DcimVirtualInterface") if spec.key == "sub:fw-1:prod")
        assert sub.data["ip_address"] == NodeRef("leg:prod#5")

    def test_a_leg_in_another_instance_is_taken_out_of_it_and_a_leg_in_its_own_is_left(self) -> None:
        plan = build(
            interfaces={
                ("fw-1", "prod"): LegInterface("sub-1", ("vrf-PROD",)),
                ("fw-2", "prod"): LegInterface("sub-2", ("vrf-OLD", "vrf-PROD")),
            }
        )

        (prune,) = plan.of(PruneEdgesSpec)
        assert prune.holder_id == NodeRef("sub:fw-2:prod")
        assert (prune.keep_peer_ids, prune.known_peer_ids) == (frozenset({"vrf-PROD"}), frozenset({"vrf-OLD"}))

    def test_conflicting_border_instances_fail_the_plan_before_anything_is_written(self) -> None:
        plan = TopologyPlan(root_id=None)

        with pytest.raises(ValueError, match="missing or conflicting VRF instances for INTERNET"):
            plan_legs(
                plan,
                spec=FIREWALL_CONTEXT,
                target=target(),
                reads=reads(),
                tenant_type="prod",
                members=members(),
                namespaces=namespaces("prod", "internet", "management"),
                leg_types=("prod", "internet", "management"),
                vrfs={**VRFS, ("bl-1", "INTERNET"): "vrf-other"},
            )


class TestAgainstTheSchema:
    """The specs of the context and its legs name fields the kinds have, with peers of the kinds they take."""

    @staticmethod
    def problems(plan: TopologyPlan) -> list[str]:
        schema = real_schema()
        candidates = candidates_missing_mandatory(plan.specs, schema)
        return [str(p) for p in [*check_nodes(plan.specs, schema), *missing_mandatory(plan.specs, [], candidates)]]

    @pytest.mark.parametrize("tracked", [False, True])
    def test_a_new_context(self, tracked: bool) -> None:
        plan = TopologyPlan(root_id=None)
        plan_context(
            plan, spec=FIREWALL_CONTEXT, target=target(track=tracked), name="c", existing=None, vlan_pool_id="pool"
        )

        assert self.problems(plan) == []

    @pytest.mark.parametrize("tracked", [False, True])
    def test_the_legs_of_a_firewall_context(self, tracked: bool) -> None:
        assert self.problems(build(tracked=tracked)) == []

    def test_the_legs_of_a_load_balancer_context(self) -> None:
        plan = build(
            spec=LOADBALANCER_CONTEXT,
            legs=("prod",),
            pool_names={"prod": "LB-Leg-PROD-IPv4", VIP_POOL_KEY: "LB-VIP-PROD-IPv4"},
        )

        assert self.problems(plan) == []

    def test_a_leg_that_carries_a_prefix_already(self) -> None:
        existing = ExistingLegPrefix(
            network="100.66.0.40/29", namespace_id="ns-default", namespace_name="default", address_ids={}
        )

        assert self.problems(build(existing_prefixes={"prod": existing})) == []
