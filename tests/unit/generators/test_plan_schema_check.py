"""The schema pre-check of the reconciler (generators/schema_check.py): every NodeSpec against the schema of its kind,
before the first write; and the plans of the DC, pod, rack and MLAG flows against the schema of the repository."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.capabilities.mlag import MlagMember, MlagReads, plan_mlag_domains
from generators.dcim.cabling_plan import PortFacts
from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from generators.model.errors import GeneratorError
from generators.plan import Hfid, NodeRef, NodeSpec, PrefixAllocationSpec, TopologyPlan
from generators.reconciler import PlanReconciler
from generators.schema_check import candidates_missing_mandatory, check_nodes, missing_mandatory
from tests.unit.generators.test_dc_plan import _PORTS
from tests.unit.generators.test_dc_plan import _generator as dc_generator
from tests.unit.generators.test_dc_plan import _with_bootstrap_groups, _with_services
from tests.unit.generators.test_fabric_services_plan import _entries
from tests.unit.generators.test_fabric_services_plan import _plan as services_plan
from tests.unit.generators.test_pod_plan import _BORDER_SPINE_PORTS, _FW_PORTS, _border_spine_payload
from tests.unit.generators.test_pod_plan import _plan_of as pod_plan_of
from tests.unit.generators.test_rack_plan import _leaf_plan
from tests.unit.plan_graph_helpers import HfidClient, wire_hfid_generator
from tests.unit.plan_reconciler_helpers import PlanGenerator, make_generator_of
from tests.unit.rack_generator_helpers import build_rack_generator, rack_reads
from tests.unit.schema_fakes import FakeSchema, real_schema
from utils.limits import MLAG_CONTROL_IPV4_POOL

_DEVICE = Hfid("DcimPhysicalDevice", ("leaf-01",))


def _problems(plan: TopologyPlan) -> list[str]:
    """What the pre-check says of ``plan`` on a branch where nothing exists yet: every spec is created."""
    schema = real_schema()
    candidates = candidates_missing_mandatory(plan.specs, schema)
    return [str(p) for p in [*check_nodes(plan.specs, schema), *missing_mandatory(plan.specs, [], candidates)]]


def _spec(kind: str, key: str = "k", **data: Any) -> NodeSpec:
    return NodeSpec(key=key, kind=kind, data=data, hfid=Hfid(kind, ("x",)))


def _plan(*specs: NodeSpec) -> TopologyPlan:
    return TopologyPlan(root_id=None, specs=list(specs))


class TestChecks:
    def test_a_kind_the_schema_does_not_know_is_named(self) -> None:
        assert _problems(_plan(_spec("NoSuchKind"))) == ["NoSuchKind 'k' field '-': the schema has no such kind"]

    def test_a_field_the_kind_does_not_have_is_named_where_the_sdk_would_drop_it_silently(self) -> None:
        plan = _plan(_spec("RoutingAutonomousSystem", asn=1, status="active"))

        assert _problems(plan) == [
            "RoutingAutonomousSystem 'k' field 'status': the kind has no such attribute or relationship"
        ]

    def test_a_peer_of_the_wrong_kind_is_named(self) -> None:
        group = Hfid("CoreStandardGroup", ("g",))
        plan = _plan(
            _spec(
                "DcimLAGInterface",
                name="Po1",
                lag_id=1,
                device=group,
                status="active",
                mlag_domain=Hfid("ManagedMLAG", ("m",)),
            )
        )

        (problem,) = _problems(plan)
        assert "field 'device': points at CoreStandardGroup, the relationship takes" in problem

    def test_a_node_ref_is_a_peer_of_the_kind_its_spec_saves(self) -> None:
        group = _spec("CoreStandardGroup", key="group", name="g")
        lag = _spec("DcimLAGInterface", key="lag", name="Po1", lag_id=1, device=NodeRef("group"), status="active")

        (problem,) = [p for p in _problems(_plan(group, lag)) if "field 'device'" in p]

        assert "DcimLAGInterface 'lag' field 'device': points at CoreStandardGroup" in problem

    def test_a_generic_peer_takes_the_kinds_that_inherit_it(self) -> None:
        domain = _spec("ManagedMLAG", name="m", peer_devices="a,b", domain_id=1, capabilities=[_DEVICE])

        assert _problems(_plan(domain)) == []

    def test_a_spec_that_creates_must_carry_every_mandatory_field_create_only_included(self) -> None:
        bare = _spec("ManagedMLAG", name="m")
        full = NodeSpec(
            key="m2",
            kind="ManagedMLAG",
            data={"name": "m2", "peer_devices": "a,b", "capabilities": [_DEVICE]},
            create_only={"domain_id": 1},
            hfid=Hfid("ManagedMLAG", ("m2",)),
        )

        problems = _problems(_plan(bare, full))

        assert problems == [
            f"ManagedMLAG 'k' field '{name}': mandatory, not set, and the object does not exist"
            for name in ("peer_devices", "domain_id", "capabilities")
        ]

    def test_a_spec_that_updates_is_not_asked_for_its_mandatory_fields(self) -> None:
        existing = NodeSpec(
            key="a", kind="ManagedMLAG", data={"name": "m"}, existing=True, hfid=Hfid("ManagedMLAG", ("m",))
        )
        by_id = NodeSpec(key="b", kind="ManagedMLAG", data={"id": "mlag-1", "domain_id": 3})
        from_template = NodeSpec(
            key="c", kind="DcimPhysicalDevice", data={"name": "d", "object_template": {"id": "t"}}, hfid=_DEVICE
        )

        assert candidates_missing_mandatory([existing, by_id, from_template], real_schema()) == {}

    def test_an_object_that_exists_is_not_created_so_it_needs_no_mandatory_field(self) -> None:
        bare = _spec("ManagedMLAG", name="m")
        candidates = candidates_missing_mandatory([bare], real_schema())

        assert candidates and missing_mandatory([bare], [Hfid("ManagedMLAG", ("x",))], candidates) == []


@pytest.mark.asyncio
class TestReconcilerBeforeTheFirstWrite:
    @staticmethod
    def _reconciler(client: HfidClient) -> PlanReconciler:
        client.schema = FakeSchema()
        return PlanReconciler(wire_hfid_generator(make_generator_of(PlanGenerator, client), client))

    async def test_every_offending_spec_is_named_with_its_key_kind_field_and_the_generator_and_nothing_is_written(
        self,
    ) -> None:
        client = HfidClient()
        good = _spec("CoreStandardGroup", key="group", name="g")
        bad = [
            _spec("RoutingAutonomousSystem", key="as", asn=1, status="active"),
            _spec("ManagedMLAG", key="mlag", name="m"),
            _spec("NoSuchKind", key="ghost"),
        ]
        plan = _plan(good, *bad)

        with pytest.raises(GeneratorError) as raised:
            await self._reconciler(client).apply(plan)

        message = str(raised.value)
        assert message.startswith("PlanGenerator: the plan does not fit the schema, nothing is written")
        for expected in (
            "RoutingAutonomousSystem 'as' field 'status'",
            "ManagedMLAG 'mlag' field 'peer_devices'",
            "ManagedMLAG 'mlag' field 'capabilities'",
            "NoSuchKind 'ghost'",
        ):
            assert expected in message
        assert client.saves == []

    async def test_a_plan_that_fits_is_written(self) -> None:
        client = HfidClient()
        plan = _plan(_spec("CoreStandardGroup", key="group", name="g"))

        await self._reconciler(client).apply(plan)

        assert [kind for kind, *_ in client.saves] == ["CoreStandardGroup"]

    async def test_a_spec_missing_a_mandatory_field_passes_when_its_object_exists(self) -> None:
        client = HfidClient()
        client.seed("ManagedMLAG", name="x", domain_id=1)
        plan = _plan(_spec("ManagedMLAG", key="mlag", name="x"))

        # the HFID of the spec exists: an update of the object, which the server validates with the fields it holds
        await self._reconciler(client).apply(plan)

        assert [kind for kind, *_ in client.saves] == ["ManagedMLAG"]

    async def test_a_plan_without_a_node_never_loads_the_schema(self) -> None:
        client = HfidClient()
        client.schema = MagicMock()

        await PlanReconciler(wire_hfid_generator(make_generator_of(PlanGenerator, client), client)).apply(
            TopologyPlan(root_id="root-1")
        )

        assert client.schema.mock_calls == []


# The plans of the converted flows against the schema of the repository: a field the kind does not have, a peer of the
# wrong kind, a mandatory field left out


def _with_owner(plan: TopologyPlan) -> list[str]:
    """``_problems`` without the owner of a device, which the templates of these tests do not name."""
    return [problem for problem in _problems(plan) if "field 'owner'" not in problem]


class TestThePlansOfTheConvertedFlowsFitTheSchema:
    def test_the_service_devices_their_ha_domains_and_their_cables(self) -> None:
        plan, _ = services_plan(_entries("internal-firewall", "load-balancer", "external-firewall"))

        assert _with_owner(plan) == []

    def test_the_mlag_domains_of_both_modes(self) -> None:
        for mode in ("back-to-back", "virtual"):
            plan = TopologyPlan(root_id="rack-1")
            names = ["leaf-01", "leaf-02"]
            plan_mlag_domains(
                plan,
                names=names,
                role="leaf",
                template={"id": "t", "interfaces": [{"name": "Eth1/49", "role": "mlag-peer"}]},
                mlag_create=mode,
                supports_virtual=True,
                members={
                    name: MlagMember(name, "sonic", (PortFacts(name, "Eth1/49", "100gbase-x-qsfp28"),))
                    for name in names
                },
                cable_deployment_id="dc-1",
                reads=MlagReads(control_pools=frozenset({MLAG_CONTROL_IPV4_POOL})),
                positions={},
                logger=MagicMock(),
                problems=[],
            )
            plan.specs[:] = [spec for spec in plan.specs if not isinstance(spec, PrefixAllocationSpec)]

            assert [p for p in _problems(plan) if "points at" in p or "no such" in p] == [], mode

    def test_the_racks_leafs_with_their_mlag_pair(self) -> None:
        gen = build_rack_generator()
        gen.data["deployment"]["mlag_create"] = "virtual"
        reads = rack_reads(
            template_owners={"tmpl-leaf": "org-1"},
            template_ports={
                "tmpl-leaf": [
                    TemplatePort("Eth1/1", "uplink", "10gbase-x-sfpp"),
                    TemplatePort("Eth1/2", "uplink", "10gbase-x-sfpp"),
                ],
                "tmpl-spine": [
                    TemplatePort("Eth1/10", "downlink", "10gbase-x-sfpp"),
                    TemplatePort("Eth1/11", "downlink", "10gbase-x-sfpp"),
                ],
            },
            mlag=MlagReads(control_pools=frozenset({MLAG_CONTROL_IPV4_POOL})),
        )
        plan, _ = _leaf_plan(gen, reads)
        gen._plan_mlag(plan, reads)

        assert _problems(plan) == []

    @pytest.mark.asyncio
    async def test_the_dc_with_its_services_on_a_first_run_and_a_rerun(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _with_bootstrap_groups(HfidClient())
        gen, client = dc_generator(monkeypatch, client)
        client.schema = FakeSchema()
        owners = {"tmpl-ss": "org-1", "tmpl-hs": "org-1", "tmpl-fw": "org-1", "tmpl-lb": "org-1"}

        async def read_templates(_client: Any, ids: Any) -> TemplateReads:
            return TemplateReads(ports={i: _PORTS.get(i, []) for i in ids}, owners={i: owners.get(i) for i in ids})

        monkeypatch.setattr("generators.dc.dc.read_template_ports", read_templates)

        await gen.generate(_with_services("internal-firewall", "load-balancer", "external-firewall"))
        await gen.generate(_with_services("internal-firewall", "load-balancer", "external-firewall"))

    @pytest.mark.asyncio
    async def test_the_pod_with_border_spines_and_its_own_services(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = _border_spine_payload(
            entries=[
                {"role": "internal-firewall", "quantity": 2, "template": {"id": "tmpl-fw", "interfaces": []}},
            ]
        )
        plan = await pod_plan_of(monkeypatch, payload, ports={"tmpl-bs": _BORDER_SPINE_PORTS, "tmpl-fw": _FW_PORTS})

        assert _with_owner(plan) == []
