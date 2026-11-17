"""The plan of an office's fabric (generators/office/office_fabric.py): its pools, switches, cabling and OSPF underlay,
built from what exists before the run and written by the reconciler on the in-memory client; and what a re-run repeats.
The exit to the SD-WAN edge is in test_office_wan_exit_plan.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig
from generators.model.errors import GeneratorError
from generators.model.office_model import (
    OFFICE_LOOPBACK_PREFIX_LENGTH,
    OFFICE_MANAGEMENT_PREFIX_LENGTH,
    OFFICE_TECHNICAL_PREFIX_LENGTH,
    OFFICE_VALID_FABRIC_ROLES,
    OfficeFabricModel,
)
from tests.unit.office_fabric_generator_helpers import (
    _ACCESS_ENTRY,
    _ACCESS_TEMPLATE,
    _DISTRIBUTION_ENTRY,
    _DISTRIBUTION_PORTS,
    _DISTRIBUTION_TEMPLATE,
    _EDGE_ENTRY,
    _VIRTUAL_TEMPLATE,
    OfficeClient,
    _office,
    office_env,
)
from tests.unit.plan_graph_helpers import HfidNode
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES

_NO_EXIT = {"edge": False}


async def run(
    monkeypatch: pytest.MonkeyPatch, *offices: dict[str, Any], client: OfficeClient | None = None, edge: bool = False
) -> tuple[Any, OfficeClient]:
    """The offices (the default one has no SD-WAN edge entry: no exit) generated on the in-memory client."""
    gen, client = office_env(monkeypatch, client=client, edge=edge)
    offices = offices or (_office(**_NO_EXIT),)
    for office in offices:
        client.store.setdefault(
            office["id"], HfidNode(client, "TopologyCustomerOffice", {"id": office["id"], "name": office["name"]})
        )
    await gen.generate({"TopologyCustomerOffice": list(offices)})
    return gen, client


def of_kind(client: OfficeClient, kind: str) -> dict[str, dict[str, Any]]:
    return {node.data["name"]: node.data for node in client.store.values() if node.kind == kind}


def snapshot(client: OfficeClient) -> dict[str, dict[str, Any]]:
    return {node_id: {k: v for k, v in node.data.items() if k != "id"} for node_id, node in client.store.items()}


class TestRoles:
    def test_the_office_has_the_two_campus_roles_and_the_edge(self) -> None:
        assert OFFICE_VALID_FABRIC_ROLES == {"access-switch", "distribution-switch", "sdwan-edge"}
        assert CAMPUS_FABRIC_ROLES == {"access-switch"}
        assert LISP_BORDER_ROLES == {"distribution-switch"}

    @pytest.mark.parametrize(
        ("role", "expected"),
        [("distribution-switch", "ds-c005-office01"), ("access-switch", "as-c005-office01")],
    )
    def test_standard_names_carry_the_role_code_and_the_office(self, role: str, expected: str) -> None:
        """A flat office device is named "{role code}-{office}{NN}"."""
        name = DeviceNamingConfig(strategy="standard").format_device_name(
            DeviceNameContext.from_indexes(fabric_name="c005-office", device_role=role, role_index=1, indexes=[])
        )
        assert name == expected


class TestFabricTemplateValidation:
    @staticmethod
    def _gen(entries: list[dict[str, Any]]) -> Any:
        """The compiled model, standing where the generator reads its usable entries and their warnings."""
        model = OfficeFabricModel.compile(_office(fabric_templates=entries))
        return SimpleNamespace(
            _valid_fabric_templates=lambda: list(model.usable_templates),
            logger=MagicMock(warning=MagicMock(side_effect=lambda *_: None), error=MagicMock()),
            model=model,
        )

    def test_valid_entries_are_kept(self) -> None:
        gen = self._gen([_DISTRIBUTION_ENTRY, _ACCESS_ENTRY])

        assert gen._valid_fabric_templates() == [_DISTRIBUTION_ENTRY, _ACCESS_ENTRY]
        assert gen.model.warnings == ()

    @pytest.mark.parametrize(
        "entry",
        [
            # not a campus role: an office has no leaf/spine/edge tier of its own
            {"role": "leaf", "quantity": 2, "template": _ACCESS_TEMPLATE},
            {"role": "border-leaf", "quantity": 2, "template": _DISTRIBUTION_TEMPLATE},
            {"role": None, "quantity": 2, "template": _ACCESS_TEMPLATE},
            {"role": "access-switch", "quantity": 0, "template": _ACCESS_TEMPLATE},
            {"role": "access-switch", "quantity": 2, "template": None},
            # a campus switch is hardware
            {"role": "access-switch", "quantity": 2, "template": _VIRTUAL_TEMPLATE},
        ],
    )
    def test_unusable_entry_warns_and_is_dropped(self, entry: dict[str, Any]) -> None:
        gen = self._gen([entry])

        assert gen._valid_fabric_templates() == []
        assert len(gen.model.warnings) == 1

    def test_one_bad_entry_does_not_drop_the_good_ones(self) -> None:
        gen = self._gen([{"role": "leaf", "quantity": 2, "template": _ACCESS_TEMPLATE}, _DISTRIBUTION_ENTRY])

        assert gen._valid_fabric_templates() == [_DISTRIBUTION_ENTRY]

    def test_second_entry_for_a_role_is_dropped(self) -> None:
        """Device names derive from the role alone, so a second entry would rename the first one's devices."""
        second = {"role": "access-switch", "quantity": 4, "template": _ACCESS_TEMPLATE}
        gen = self._gen([_ACCESS_ENTRY, second])

        assert gen._valid_fabric_templates() == [_ACCESS_ENTRY]
        assert len(gen.model.warnings) == 1

    def test_no_fabric_templates_is_no_fabric(self) -> None:
        gen = self._gen([])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_not_called()


@pytest.mark.asyncio
class TestGenerate:
    async def test_the_fabric_names_derive_from_the_office_and_the_root_is_tracked_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = await run(monkeypatch)

        assert (gen.fabric_name, gen.pod_name, gen.deployment_id) == ("c005-office", None, "office-1")
        assert client.related_at_first_write == ["office-1"]
        gen.logger.error.assert_not_called()

    async def test_no_distribution_switch_means_no_wan_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An access-only office has nothing to exit from, even with an edge."""
        _, client = await run(monkeypatch, _office(fabric_templates=[_ACCESS_ENTRY, _EDGE_ENTRY]))

        assert not [name for name in of_kind(client, "ManagedBGP") if "wan-exit" in name]
        assert "C005-OFFICE-EDGE1-GE3__" not in "".join(of_kind(client, "DcimCable"))

    async def test_office_without_fabric_templates_creates_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The opt-in: the created-trigger fires for every office, so most runs end here."""
        gen, client = await run(monkeypatch, _office(fabric_templates=[]))

        assert client.saves == [] and gen.locked_keys == []
        gen.logger.error.assert_not_called()

    async def test_no_office_in_the_response_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = office_env(monkeypatch)

        await gen.generate({"TopologyCustomerOffice": {"edges": []}})

        gen.logger.error.assert_called_once()
        assert client.saves == []

    async def test_the_raw_graphql_response_is_cleaned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = office_env(monkeypatch, edge=False)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})
        entries = [
            {
                "node": {
                    "role": {"value": entry["role"]},
                    "quantity": {"value": entry["quantity"]},
                    "template": {"node": entry["template"]},
                }
            }
            for entry in (_DISTRIBUTION_ENTRY, _ACCESS_ENTRY)
        ]
        raw = {
            "TopologyCustomerOffice": {
                "edges": [
                    {
                        "node": {
                            "id": "office-1",
                            "name": {"value": "C005-OFFICE"},
                            "naming_convention": {"value": "standard"},
                            "sdwan_gateways": {"edges": []},
                            "fabric_templates": {"edges": entries},
                        }
                    }
                ]
            }
        }

        await gen.generate(raw)

        assert sorted(of_kind(client, "DcimPhysicalDevice")) == [
            "as-c005-office01",
            "as-c005-office02",
            "ds-c005-office01",
            "ds-c005-office02",
        ]

    async def test_each_office_of_the_response_is_generated_with_its_own_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = _office(id="office-2", name="C002-OFFICE", **_NO_EXIT)

        gen, client = await run(monkeypatch, _office(**_NO_EXIT), other)

        assert gen.fabric_name == "c002-office"
        assert "ds-c002-office01" in of_kind(client, "DcimPhysicalDevice")
        assert {"c005-office-vni-pool", "c002-office-vni-pool"} <= set(of_kind(client, "CoreNumberPool"))
        assert {key for key in client.store["office-1"].data} >= {"vni_pool", "lisp_site_key"}
        assert client.store["office-1"].data["vni_pool"] != client.store["office-2"].data["vni_pool"]


@pytest.mark.asyncio
class TestPools:
    async def test_slices_each_bootstrap_pool_under_the_office_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)

        pools = {
            name: pool
            for kind in ("CoreIPAddressPool", "CoreIPPrefixPool")
            for name, pool in of_kind(client, kind).items()
            if name.startswith("c005-office-")
        }
        assert {name: pool["default_prefix_length"] for name, pool in pools.items()} == {
            "c005-office-loopback-pool": OFFICE_LOOPBACK_PREFIX_LENGTH,
            "c005-office-management-pool": OFFICE_MANAGEMENT_PREFIX_LENGTH,
            "c005-office-technical-pool": OFFICE_TECHNICAL_PREFIX_LENGTH,
        }
        parents = {
            identifier: client.store[pool_id].data["name"]
            for pool_id, identifier in client.allocations
            if identifier.startswith("c005-office-")
        }
        assert parents == {
            "c005-office-loopback-pool": "Loopback-IPv4",
            "c005-office-management-pool": "Management-IPv4",
            "c005-office-technical-pool": "Technical-IPv4",
        }
        assert pools["c005-office-technical-pool"]["default_prefix_type"] == "IpamPrefix"
        assert pools["c005-office-loopback-pool"]["default_address_type"] == "IpamIPAddress"

    async def test_the_vni_pool_is_the_offices_own_and_attached_to_it_with_the_site_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch)

        pool = of_kind(client, "CoreNumberPool")["c005-office-vni-pool"]
        assert (pool["node"], pool["node_attribute"]) == ("ManagedSegmentDeployment", "vni")
        office = client.store["office-1"].data
        names = {node_id: node.data.get("name") for node_id, node in client.store.items()}
        assert names[office["vni_pool"]] == "c005-office-vni-pool"
        assert names[office["lisp_site_key"]] == "c005-office-lisp-site-key"
        (kwargs,) = [kwargs for _, node_id, _, kwargs in client.saves if node_id == "office-1"]
        assert kwargs == {"allow_upsert": True, "update_group_context": False}

    async def test_the_site_key_is_created_once_with_a_secret_and_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch)
        key_id, key = next(
            (n.id, n.data) for n in client.store.values() if n.data.get("name") == "c005-office-lisp-site-key"
        )
        secret = key["password"]
        assert secret
        assert [kwargs for _, node_id, _, kwargs in client.saves if node_id == key_id] == [{"allow_upsert": True}]

        await run(monkeypatch, client=client)

        assert client.store[key_id].data["password"] == secret

    async def test_the_pools_and_the_key_are_created_under_the_locks_of_their_find_or_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, _ = await run(monkeypatch)

        assert gen.locked_keys == ["office-pools-office-1", "routing-password-c005-office-lisp-site-key"]


@pytest.mark.asyncio
class TestDevices:
    async def test_every_entry_becomes_devices_of_its_role_with_loopback_and_management_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch)

        devices = of_kind(client, "DcimPhysicalDevice")
        assert {name: data["role"] for name, data in devices.items()} == {
            "as-c005-office01": "access-switch",
            "as-c005-office02": "access-switch",
            "ds-c005-office01": "distribution-switch",
            "ds-c005-office02": "distribution-switch",
        }
        # The office is the deployment of its own kit, and every device is a campus switch of its template
        assert {data["deployment"]["id"] for data in devices.values()} == {"office-1"}
        assert {data["object_template"]["id"] for name, data in devices.items() if name.startswith("as-")} == {
            "tpl-access"
        }
        loopbacks = [node.data for node in client.store.values() if node.kind == "DcimVirtualInterface"]
        assert len(loopbacks) == 4 and {data["name"] for data in loopbacks} == {"Loopback0"}
        descriptions = sorted(
            node.data["description"]
            for node in client.store.values()
            if node.kind == "IpamIPAddress" and "identifier" in node.data
        )
        assert descriptions == sorted(
            f"{kind} IP for {name}" for kind in ("Loopback", "Management") for name in devices
        )

    async def test_each_role_joins_its_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)

        assert {"access-switches", "distribution-switches"} <= set(of_kind(client, "CoreStandardGroup"))

    async def test_naming_convention_comes_from_the_office(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _office(naming_convention="Standard", **_NO_EXIT))

        assert "ds-c005-office01" in of_kind(client, "DcimPhysicalDevice")


@pytest.mark.asyncio
class TestCabling:
    async def test_access_uplinks_cable_to_distribution_downlinks_over_a_p2p_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch)

        cables = sorted(name for name in of_kind(client, "DcimCable") if name.startswith("as-"))
        assert cables == [
            "as-c005-office01-HundredGigE1/1/1__ds-c005-office01-HundredGigE1/0/1",
            "as-c005-office01-HundredGigE1/1/2__ds-c005-office02-HundredGigE1/0/1",
            "as-c005-office02-HundredGigE1/1/1__ds-c005-office01-HundredGigE1/0/2",
            "as-c005-office02-HundredGigE1/1/2__ds-c005-office02-HundredGigE1/0/2",
        ]
        links = {
            identifier: client.store[pool_id].data["name"]
            for pool_id, identifier in client.allocations
            if identifier.startswith("DcimPhysicalInterface")
        }
        # one /31 per link, five links (four uplinks and the interlink), all from the office's technical pool
        assert len(links) == 5 and set(links.values()) == {"c005-office-technical-pool"}
        prefixes = [n for n in client.store.values() if n.kind == "IpamPrefix" and str(n.prefix.value).endswith("/31")]
        assert len(prefixes) == 5

    async def test_the_distribution_pair_is_linked_on_its_interlink_ports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client = await run(monkeypatch)

        assert [name for name in of_kind(client, "DcimCable") if name.startswith("ds-")] == [
            "ds-c005-office01-HundredGigE1/0/25__ds-c005-office02-HundredGigE1/0/25"
        ]

    async def test_no_interlink_unless_exactly_two_distribution_switches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(
            monkeypatch,
            _office(fabric_templates=[{**_DISTRIBUTION_ENTRY, "quantity": 3}], **_NO_EXIT),
        )

        assert not [name for name in of_kind(client, "DcimCable") if name.startswith("ds-")]

    async def test_a_distribution_pair_without_interlink_ports_fails_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen, client = office_env(
            monkeypatch, ports={"tpl-dist": [port for port in _DISTRIBUTION_PORTS if port.role != "interlink"]}
        )
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})

        with pytest.raises(GeneratorError, match="cannot cable"):
            await gen.generate({"TopologyCustomerOffice": [_office(**_NO_EXIT)]})

        assert client.saves == []

    @pytest.mark.parametrize(("template", "role"), [("tpl-access", "uplink"), ("tpl-dist", "downlink")])
    async def test_a_template_without_the_needed_ports_is_an_error_and_cables_nothing(
        self, monkeypatch: pytest.MonkeyPatch, template: str, role: str
    ) -> None:
        office = _office(**_NO_EXIT)
        office["fabric_templates"] = [
            {
                **entry,
                "template": {
                    **entry["template"],
                    "interfaces": [i for i in entry["template"]["interfaces"] if i["role"] != role],
                },
            }
            if entry["template"]["id"] == template
            else entry
            for entry in office["fabric_templates"]
        ]
        gen, client = office_env(monkeypatch)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})

        with pytest.raises(GeneratorError, match="cannot cable"):
            await gen.generate({"TopologyCustomerOffice": [office]})

        assert client.saves == []

    async def test_a_distribution_only_office_is_not_cabled_to_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch, _office(fabric_templates=[_DISTRIBUTION_ENTRY], **_NO_EXIT))

        assert [name for name in of_kind(client, "DcimCable") if name.startswith("as-")] == []
        assert len(of_kind(client, "DcimCable")) == 1


@pytest.mark.asyncio
class TestOspfUnderlay:
    async def test_the_underlay_is_ospf_in_one_area_with_no_overlay_bgp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)

        assert list(of_kind(client, "RoutingOSPFArea")) == ["c005-office-ospf-area-0"]
        assert sorted(of_kind(client, "ManagedOSPF")) == [
            f"{name}-ospf-underlay"
            for name in ("as-c005-office01", "as-c005-office02", "ds-c005-office01", "ds-c005-office02")
        ]
        assert not of_kind(client, "ManagedBGP") and not of_kind(client, "ManagedBGPPeering")
        # a peering per cabled pair (four uplinks, one interlink) and an interface config per cabled port
        assert len(of_kind(client, "ManagedOSPFPeering")) == 5
        assert len(of_kind(client, "RoutingOSPFInterface")) == 10

    async def test_every_process_uses_the_loopback_address_as_router_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)

        by_name = {node.id: node.data["name"] for node in client.store.values() if node.kind == "DcimPhysicalDevice"}
        loopback = {
            by_name[node.data["device"]["id"]]: node.data["ip_address"]
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface"
        }
        for name, process in of_kind(client, "ManagedOSPF").items():
            assert process["router_id"] == {"id": loopback[name.removesuffix("-ospf-underlay")]}

    async def test_the_area_is_the_runs_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)

        area_id = next(n.id for n in client.store.values() if n.kind == "RoutingOSPFArea")
        assert [kwargs for _, node_id, _, kwargs in client.saves if node_id == area_id] == [{"allow_upsert": True}]


@pytest.mark.asyncio
class TestRerun:
    async def test_a_rerun_writes_the_same_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)
        first = snapshot(client)

        await run(monkeypatch, client=client)

        assert snapshot(client) == first

    async def test_a_rerun_creates_no_device_pool_cable_or_session_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client = await run(monkeypatch)
        before = {
            kind: sum(1 for n in client.store.values() if n.kind == kind)
            for kind in {n.kind for n in client.store.values()}
        }

        await run(monkeypatch, client=client)

        assert {kind: sum(1 for n in client.store.values() if n.kind == kind) for kind in before} == before
