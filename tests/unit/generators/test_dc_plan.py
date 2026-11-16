"""The DC generator's plan (generators/dc/dc.py): the pools, the shared routing objects, the DC-scoped tiers with their
BGP and cables, built from what exists before the run and written by the reconciler; and what a re-run repeats.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.dc.dc import DCTopologyGenerator
from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from generators.model.errors import GeneratorError
from tests.unit.dc_generator_helpers import _dc_design, _deployment
from tests.unit.fabric_plan_helpers import stored_routing_inputs
from tests.unit.plan_graph_helpers import HfidClient, make_hfid_generator

_SFP = "10gbase-x-sfpp"
_PARENTS = ("Management-IPv4", "Technical-IPv4", "Technical-IPv6", "Loopback-IPv4", "Loopback-IPv6")
_SS_TEMPLATE = {
    "id": "tmpl-ss",
    "interfaces": [{"name": "Ethernet1", "role": "uplink"}, {"name": "Ethernet2", "role": "uplink"}],
}
_HS_TEMPLATE = {
    "id": "tmpl-hs",
    "interfaces": [{"name": "Ethernet1", "role": "downlink"}, {"name": "Ethernet2", "role": "downlink"}],
}
_PORTS = {
    "tmpl-ss": [TemplatePort("Ethernet1", "uplink", _SFP), TemplatePort("Ethernet2", "uplink", _SFP)],
    "tmpl-hs": [TemplatePort("Ethernet1", "downlink", _SFP), TemplatePort("Ethernet2", "downlink", _SFP)],
    "tmpl-fw": [TemplatePort("Ethernet1", "uplink", _SFP), TemplatePort("ha1", "ha", _SFP)],
    "tmpl-lb": [TemplatePort("Ethernet1", "uplink", _SFP), TemplatePort("ha1", "ha", _SFP)],
}


class _EveryDeviceBringsItsPorts(dict[str, list[dict[str, Any]]]):
    """The ports a device saved with its template brings: the same two on every device."""

    def get(self, key: str, default: Any = None) -> list[dict[str, Any]]:  # ty: ignore[invalid-method-override]
        return [{"name": port, "interface_type": _SFP} for port in ("Ethernet1", "Ethernet2", "ha1")]


def _super_and_hyper(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "design": _dc_design(max_super_spines_per_fabric=2, max_hyper_spines_per_fabric=2),
        "amount_of_super_spines": 2,
        "super_spine_template": _SS_TEMPLATE,
        "amount_of_hyper_spines": 2,
        "hyper_spine_template": _HS_TEMPLATE,
        "underlay_protocol": "ipv4",
    }
    return _deployment(**{**kwargs, **overrides})


def _generator(monkeypatch: pytest.MonkeyPatch, client: HfidClient | None = None) -> tuple[Any, HfidClient]:
    """A DC generator on the in-memory client: the parent pools exist, the reads of what other generators wrote are
    answered from the store.
    """
    client = client or HfidClient()
    if not any(node.kind == "CoreIPPrefixPool" for node in client.store.values()):
        for parent in _PARENTS:
            client.seed("CoreIPPrefixPool", name=parent)
    client.template_ports = _EveryDeviceBringsItsPorts()
    gen = make_hfid_generator(client, cls=DCTopologyGenerator)

    async def read_templates(_client: Any, ids: Any) -> TemplateReads:
        return TemplateReads(ports={template_id: _PORTS.get(template_id, []) for template_id in ids}, owners={})

    async def read_routing(generator: Any, *, fabric_name: str, names: list[str], **_: Any) -> Any:
        return stored_routing_inputs(client, fabric_name, list(names))

    monkeypatch.setattr("generators.dc.dc.read_template_ports", read_templates)
    monkeypatch.setattr("generators.dc.dc.read_routing_inputs", read_routing)
    return gen, client


def _kinds(client: HfidClient) -> list[str]:
    return [kind for kind, *_ in client.saves]


def _saved(client: HfidClient, kind: str) -> list[dict[str, Any]]:
    return [data for saved_kind, _, data, _ in client.saves if saved_kind == kind]


def _snapshot(client: HfidClient) -> dict[str, dict[str, Any]]:
    return {
        node_id: {key: value for key, value in node.data.items() if key != "id"}
        for node_id, node in client.store.items()
    }


@pytest.mark.asyncio
class TestGuardClauses:
    async def test_no_deployment_data_logs_error_and_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate({"TopologyDeployment": []})

        gen.logger.error.assert_called_once()
        assert client.saves == []

    async def test_a_response_missing_a_required_field_logs_error_and_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate({"TopologyDeployment": [{"id": "dc-1"}]})

        gen.logger.error.assert_called_once()
        assert "Generation failed" in gen.logger.error.call_args.args[0] and client.saves == []

    async def test_super_spines_over_the_design_write_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)
        data = _super_and_hyper(design=_dc_design(max_super_spines_per_fabric=1, max_hyper_spines_per_fabric=2))

        with pytest.raises(RuntimeError, match="requests 2 super-spines but the assigned design allows at most 1"):
            await gen.generate(data)

        assert client.saves == []

    async def test_hyper_spines_over_the_design_write_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)
        data = _super_and_hyper(design=_dc_design(max_super_spines_per_fabric=2, max_hyper_spines_per_fabric=1))

        with pytest.raises(RuntimeError, match="hyper-spines"):
            await gen.generate(data)

        assert client.saves == []


@pytest.mark.asyncio
class TestDcPlan:
    async def test_the_root_is_tracked_first_and_is_the_dc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        assert client.related_at_first_write == ["dc-1"]

    async def test_the_pools_the_shared_objects_and_the_devices_are_written_in_dependency_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        kinds = _kinds(client)
        last_pool = max(i for i, kind in enumerate(kinds) if kind.endswith("Pool"))
        assert last_pool < kinds.index("DcimPhysicalDevice")
        assert max(i for i, kind in enumerate(kinds) if kind == "DcimPhysicalDevice") < kinds.index("DcimCable")
        assert kinds.index("RoutingAutonomousSystem") < kinds.index("ManagedBGP") < kinds.index("ManagedBGPPeering")
        assert len(_saved(client, "DcimCable")) == 4  # two super-spines, each to both hyper-spines
        # the pools, the asn pool and the vni pool, once each
        names = sorted(data["name"] for data in _saved(client, "CoreNumberPool"))
        assert names == ["dc1-asn-pool", "dc1-vni-pool"]

    async def test_the_dc_gets_its_pools_and_the_fabric_as_in_one_untracked_save(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        (dc_data,) = [data for kind, node_id, data, _ in client.saves if node_id == "dc-1"]
        assert set(dc_data) == {
            "id",
            "loopback_pool",
            "management_pool",
            "technical_pool",
            "vni_pool",
            "fabric_asn_pool",
            "evpn_rt_as",
        }
        (dc_kwargs,) = [kwargs for _, node_id, _, kwargs in client.saves if node_id == "dc-1"]
        assert dc_kwargs == {"allow_upsert": True, "update_group_context": False}

    async def test_the_tiers_draw_loopbacks_from_the_dc_fabric_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        pool_id = next(
            node.id for node in client.store.values() if node.data.get("name") == "dc1-dc-fabric-loopback-pool"
        )
        loopback_pools = {
            node.data["identifier"]
            for node in client.store.values()
            if node.kind == "IpamIPAddress" and node.data.get("description", "").startswith("Loopback IP")
        }
        assert len(loopback_pools) == 4 and pool_id

    async def test_the_super_spines_share_one_underlay_as_and_the_hyper_spines_have_their_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        underlay = {
            data["name"]: data["local_as"]["id"]
            for data in _saved(client, "ManagedBGP")
            if data["process_role"] == "underlay"
        }
        assert underlay["ss-dc1101-bgp-underlay"] == underlay["ss-dc1102-bgp-underlay"]
        assert underlay["hs-dc1101-bgp-underlay"] != underlay["hs-dc1102-bgp-underlay"]
        systems = _saved(client, "RoutingAutonomousSystem")
        assert any(data["description"] == "dc1 super-spine underlay ASN" for data in systems)
        overlay = [data for data in systems if data["description"] == "dc1 overlay ASN for iBGP EVPN"]
        assert len(overlay) == 1 and isinstance(overlay[0]["asn"], int)

    async def test_the_overlay_peers_the_tiers_with_each_other_and_across(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hyper-spines' overlay processes are in the same plan as the cross-tier sessions to the super-spines."""
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        overlay = {data["name"] for data in _saved(client, "ManagedBGPPeering") if data["peering_role"] == "overlay"}
        assert overlay == {
            "overlay-evpn--hs-dc1101--hs-dc1102",
            "overlay-evpn--hs-dc1101--ss-dc1101",
            "overlay-evpn--hs-dc1101--ss-dc1102",
            "overlay-evpn--hs-dc1102--ss-dc1101",
            "overlay-evpn--hs-dc1102--ss-dc1102",
            "overlay-evpn--ss-dc1101--ss-dc1102",
        }

    async def test_the_auth_keys_get_a_secret_when_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        keys = {data["name"]: data for data in _saved(client, "RoutingPassword")}
        assert set(keys) == {"dc1-underlay-key", "dc1-overlay-key"} and all(data["password"] for data in keys.values())

    async def test_an_ospf_underlay_has_no_asn_pool_and_an_area(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper(routing_strategy="ospf-ibgp"))

        assert [data["name"] for data in _saved(client, "CoreNumberPool")] == ["dc1-vni-pool"]
        assert [data["name"] for data in _saved(client, "RoutingOSPFArea")] == ["dc1-ospf-area-0"]
        # the super-spines get the overlay only: no underlay under ospf-ibgp is pre-seeded, the cables carry OSPF
        assert {data["process_role"] for data in _saved(client, "ManagedBGP")} == {"overlay"}
        assert _saved(client, "ManagedOSPF")

    async def test_back_to_back_has_no_super_spine_tier_and_no_cabling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)
        data = _super_and_hyper(
            design=_dc_design(max_super_spines_per_fabric=0, max_hyper_spines_per_fabric=2), amount_of_super_spines=0
        )

        await gen.generate(data)

        names = [device["name"] for device in _saved(client, "DcimPhysicalDevice")]
        assert len(names) == 2 and all("hs-" in name or "hyper" in name or name.startswith("hs") for name in names)
        assert _saved(client, "DcimCable") == []

    async def test_a_tier_without_ports_fails_before_anything_is_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)
        data = _super_and_hyper(super_spine_template={"id": "tmpl-ss", "interfaces": []})

        with pytest.raises(GeneratorError, match="cannot cable super-spine<->hyper-spine"):
            await gen.generate(data)

        assert client.saves == []

    async def test_a_port_the_template_lacks_fails_before_anything_is_written_naming_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)
        ports = {**_PORTS, "tmpl-ss": [TemplatePort("Ethernet1", "uplink", _SFP)]}
        monkeypatch.setattr("generators.dc.dc._ports.__defaults__", None, raising=False)

        async def read_templates(_client: Any, ids: Any) -> TemplateReads:
            return TemplateReads(ports={template_id: ports.get(template_id, []) for template_id in ids}, owners={})

        monkeypatch.setattr("generators.dc.dc.read_template_ports", read_templates)

        with pytest.raises(GeneratorError, match=r"super-spine template has no port \['Ethernet2'\]"):
            await gen.generate(_super_and_hyper())

        assert client.saves == []

    async def test_a_parent_pool_nobody_created_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="Management-IPv4")
        gen, _ = _generator(monkeypatch, client)

        with pytest.raises(GeneratorError, match="Not found after"):
            await gen.generate(_super_and_hyper())

        assert client.saves == []

    async def test_the_run_holds_the_locks_of_what_it_decides_from_a_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, _ = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        assert gen.locked_keys == [
            "hyperspine-cabling-dc-1",
            "pool-alloc-fabric-dc-1",
            "shared-routing-dc-1",
        ]


@pytest.mark.asyncio
class TestRerun:
    async def test_a_rerun_writes_the_same_objects_and_rotates_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch)
        await gen.generate(_super_and_hyper())
        snapshot = _snapshot(client)
        secrets = {data["name"]: data["password"] for data in _saved(client, "RoutingPassword")}
        written = len(client.saves)

        again, _ = _generator(monkeypatch, client)
        await again.generate(_super_and_hyper())

        assert _snapshot(client) == snapshot
        later = client.saves[written:]
        assert not [data for kind, _, data, _ in later if kind == "RoutingPassword" and "password" in data]
        assert {data["name"]: data["password"] for data in _saved(client, "RoutingPassword")[:2]} == secrets
        assert [kind for kind, *_ in later if kind == "RoutingAutonomousSystem"] == [
            "RoutingAutonomousSystem"
        ]  # only the overlay AS, which is pinned and rewritten by id

    async def test_the_super_spine_as_found_is_tracked_and_not_drawn_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)
        await gen.generate(_super_and_hyper())
        found = next(
            node.id for node in client.store.values() if node.data.get("description") == "dc1 super-spine underlay ASN"
        )
        client.group_context.related_node_ids.clear()

        again, _ = _generator(monkeypatch, client)
        await again.generate(_super_and_hyper())

        assert found in client.group_context.related_node_ids


@pytest.mark.asyncio
class TestExistingPods:
    async def test_pods_of_keeps_the_children_that_carry_a_design(self) -> None:
        children = [{"id": "x"}, {"id": "pod-1", "name": "P1", "index": 1, "design": {"id": "d"}}, {}]

        assert DCTopologyGenerator.pods_of({"children": children}) == [children[1]]
        assert DCTopologyGenerator.pods_of({}) == []

    async def test_the_pods_come_with_the_dc_query_and_are_read_never_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)
        data = _super_and_hyper()
        data["TopologyDeployment"][0]["children"] = [{"id": "pod-1", "name": "P1", "index": 1, "design": {"id": "d"}}]

        await gen.generate(data)

        assert "pod-1" not in client.group_context.related_node_ids
        assert [pod["id"] for pod in gen._existing_pods] == ["pod-1"]


def _with_services(*roles: str) -> dict[str, Any]:
    """The DC of ``_super_and_hyper`` that also declares service devices (one HA pair per role)."""
    data = _super_and_hyper()
    templates = {
        "internal-firewall": {"id": "tmpl-fw", "interfaces": []},
        "load-balancer": {"id": "tmpl-lb", "interfaces": []},
        "external-firewall": {"id": "tmpl-fw", "interfaces": []},
    }
    data["TopologyDeployment"][0]["fabric_templates"].extend(
        {"role": role, "quantity": 2, "template": templates[role]} for role in roles
    )
    return data


def _with_bootstrap_groups(client: HfidClient) -> HfidClient:
    client.seed("CoreStandardGroup", name="ha_domains")
    return client


@pytest.mark.asyncio
class TestDcServices:
    """The DC's firewalls, load balancers and Internet-edge pair are part of the DC's one plan."""

    async def test_each_pair_is_a_domain_and_each_role_has_its_context_vlan_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch, _with_bootstrap_groups(HfidClient()))

        await gen.generate(_with_services("internal-firewall", "load-balancer"))

        assert [data["name"] for data in _saved(client, "ManagedFirewallHA")] == ["fw-dc1101-fw-dc1102"]
        assert [data["name"] for data in _saved(client, "ManagedLoadbalancerHA")] == ["lb-dc1101-lb-dc1102"]
        pools = [data["name"] for data in _saved(client, "CoreNumberPool")]
        assert "dc1-fw-context-vlan-pool" in pools and "dc1-lb-context-vlan-pool" in pools

    async def test_the_external_pair_alone_gives_the_dc_its_firewall_context_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch, _with_bootstrap_groups(HfidClient()))

        await gen.generate(_with_services("external-firewall"))

        pools = [data["name"] for data in _saved(client, "CoreNumberPool")]
        assert "dc1-fw-context-vlan-pool" in pools and "dc1-lb-context-vlan-pool" not in pools

    async def test_a_dc_without_service_devices_plans_no_domain_and_no_pool_of_theirs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = _generator(monkeypatch)

        await gen.generate(_super_and_hyper())

        assert not [kind for kind in _kinds(client) if kind.endswith("HA")]
        assert "dc1-fw-context-vlan-pool" not in [data["name"] for data in _saved(client, "CoreNumberPool")]

    async def test_the_pools_and_the_domains_are_created_under_the_locks_of_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, _ = _generator(monkeypatch, _with_bootstrap_groups(HfidClient()))

        await gen.generate(_with_services("internal-firewall", "load-balancer"))

        assert {
            "fw-context-pools-dc1",
            "lb-context-pools-dc1",
            "firewall-ha-group-pool",
            "ha-domain-fw-dc1101-fw-dc1102",
            "ha-domain-lb-dc1101-lb-dc1102",
        } <= set(gen.locked_keys)

    async def test_a_rerun_writes_the_same_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = _generator(monkeypatch, _with_bootstrap_groups(HfidClient()))
        await gen.generate(_with_services("internal-firewall", "load-balancer"))
        first = _snapshot(client)
        created = list(client.created_ids)

        await gen.generate(_with_services("internal-firewall", "load-balancer"))

        assert client.created_ids == created and _snapshot(client) == first
