"""Unit tests for transforms/colocation/virtual_colocation.py: the query result of a TopologyVirtualColocation
becomes the renderer's input (normalise_virtual_colocation) and the transform prints the Equinix Terraform for it.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import MagicMock

import pytest

from transforms.colocation.virtual_colocation import VirtualColocationTerraform, normalise_virtual_colocation
from transforms.colocation.virtual_terraform import render_equinix_network_devices

_C8000V = {
    "template_name": "C8000V_EDGE",
    "cpu": 2,
    "provider_type_code": "C8000V",
    "provider_package_code": "network-essentials",
    "provider_version": "17.12.01a",
    "provider_term_length": 12,
}
_PA_VM = {
    "template_name": "PA-VM_EDGE_CUSTOMER_S",
    "cpu": 2,
    "provider_type_code": "PA-VM",
    "provider_package_code": "VM100",
    "provider_version": "11.1.3",
    "provider_term_length": 12,
}
_FG_VM = {**_PA_VM, "template_name": "FG-VM_EDGE", "provider_type_code": "FG-VM"}


def _entry(role: str, quantity: int, template: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "quantity": quantity, "template": template}


def _devices(prefix: str, role: str, count: int) -> list[dict[str, Any]]:
    return [{"name": f"{prefix}{index:02d}", "role": role} for index in range(1, count + 1)]


def _vc(entries: list[dict[str, Any]], devices: list[dict[str, Any]], *, operator: str = "EQUINIX") -> dict[str, Any]:
    """The cleaned query result (clean_data output) for virtual colocation AM-NE in metro AM."""
    return {
        "TopologyVirtualColocation": [
            {
                "id": "vc-1",
                "name": "AM-NE",
                "infrastructure_orchestrator": "terraform",
                "provider_account_number": "1000001",
                "owner": {
                    "id": "o-1",
                    "name": "Internal",
                    "notification_emails": ["noc@example.com", "network-ops@example.com"],
                },
                "parent": {"id": "m-1", "name": "AM", "parent": {"id": "c-1", "name": operator}},
                "fabric_templates": entries,
                "devices": devices,
            }
        ]
    }


def _edge_pair() -> dict[str, Any]:
    return _vc([_entry("edge", 2, _C8000V)], _devices("eg-am-ne", "edge", 2))


def _edge_pair_and_firewall_cluster() -> dict[str, Any]:
    return _vc(
        [_entry("edge", 2, _C8000V), _entry("firewall", 2, _PA_VM)],
        [*_devices("eg-am-ne", "edge", 2), *_devices("fw-am-ne", "firewall", 2)],
    )


def _raw(cleaned: dict[str, Any]) -> dict[str, Any]:
    """The GraphQL response shape (value / node / edges wrappers) of a cleaned query result."""

    def value(item: Any) -> dict[str, Any]:
        return {"value": item}

    def node(item: Any) -> dict[str, Any]:
        return {"node": item}

    vc = cleaned["TopologyVirtualColocation"][0]
    metro = vc["parent"]
    return {
        "TopologyVirtualColocation": {
            "edges": [
                node(
                    {
                        "id": vc["id"],
                        "name": value(vc["name"]),
                        "infrastructure_orchestrator": value(vc["infrastructure_orchestrator"]),
                        "provider_account_number": value(vc["provider_account_number"]),
                        "owner": node(
                            {
                                "id": vc["owner"]["id"],
                                "name": value(vc["owner"]["name"]),
                                "notification_emails": value(vc["owner"]["notification_emails"]),
                            }
                        )
                        if vc["owner"]
                        else {"node": None},
                        "parent": node(
                            {
                                "id": metro["id"],
                                "name": value(metro["name"]),
                                "parent": node({"id": metro["parent"]["id"], "name": value(metro["parent"]["name"])}),
                            }
                        ),
                        "fabric_templates": {
                            "edges": [
                                node(
                                    {
                                        "role": value(entry["role"]),
                                        "quantity": value(entry["quantity"]),
                                        "template": node(
                                            {
                                                "id": f"t-{entry['template']['template_name']}",
                                                **{
                                                    key: value(item)
                                                    for key, item in entry["template"].items()
                                                    if item is not None
                                                },
                                            }
                                        ),
                                    }
                                )
                                for entry in vc["fabric_templates"]
                            ]
                        },
                        "devices": {
                            "edges": [
                                node(
                                    {
                                        "id": f"d-{device['name']}",
                                        "name": value(device["name"]),
                                        "role": value(device["role"]),
                                    }
                                )
                                for device in vc["devices"]
                            ]
                        },
                    }
                )
            ]
        }
    }


def _run(raw: dict[str, Any]) -> str:
    transform = VirtualColocationTerraform(client=MagicMock(), infrahub_node=MagicMock(), root_directory=".")
    return asyncio.run(transform.transform(raw))


# ---------------------------------------------------------------- normaliser


def test_edge_pair_is_an_ha_pair_first_device_primary() -> None:
    """Two edges: the first by name is the primary, the second its ha_peer target; metro code is the metro name."""
    vc = normalise_virtual_colocation(_edge_pair())

    assert vc["name"] == "AM-NE"
    assert vc["provider"] == "equinix"
    primary, secondary = vc["devices"]
    assert primary == {
        "name": "eg-am-ne01",
        "metro_code": "AM",
        "type_code": "C8000V",
        "package_code": "network-essentials",
        "version": "17.12.01a",
        "core_count": 2,
        "term_length": 12,
        "ha_peer": "eg-am-ne02",
    }
    assert secondary["name"] == "eg-am-ne02"
    assert "ha_peer" not in secondary


def test_pair_order_follows_names_not_query_order() -> None:
    """The primary is the lowest name whatever order the query returned."""
    cleaned = _edge_pair()
    cleaned["TopologyVirtualColocation"][0]["devices"].reverse()

    assert normalise_virtual_colocation(cleaned)["devices"][0]["ha_peer"] == "eg-am-ne02"


def test_single_edge_has_no_peer() -> None:
    """One edge is a plain device."""
    vc = normalise_virtual_colocation(_vc([_entry("edge", 1, _C8000V)], _devices("eg-am-ne", "edge", 1)))

    assert len(vc["devices"]) == 1
    assert "ha_peer" not in vc["devices"][0]


def test_three_edges_are_refused() -> None:
    """Equinix knows one device or one pair; a third edge is not guessed at."""
    cleaned = _vc([_entry("edge", 3, _C8000V)], _devices("eg-am-ne", "edge", 3))

    with pytest.raises(ValueError, match="3 edge devices"):
        normalise_virtual_colocation(cleaned)


def test_pa_vm_firewall_pair_is_one_cluster_named_after_the_nodes() -> None:
    """Two PA-VM firewalls: node 0 and 1 of the cluster fw-am-ne."""
    vc = normalise_virtual_colocation(_edge_pair_and_firewall_cluster())

    firewalls = [device for device in vc["devices"] if device["name"].startswith("fw-")]
    assert [device["cluster"] for device in firewalls] == [
        {"name": "fw-am-ne", "node": 0},
        {"name": "fw-am-ne", "node": 1},
    ]
    assert {device["type_code"] for device in firewalls} == {"PA-VM"}


def test_single_firewall_is_a_plain_device() -> None:
    """One firewall: no cluster."""
    cleaned = _vc([_entry("firewall", 1, _PA_VM)], _devices("fw-am-ne", "firewall", 1))

    assert "cluster" not in normalise_virtual_colocation(cleaned)["devices"][0]


def test_firewall_pair_of_another_vendor_is_refused_by_the_renderer() -> None:
    """The normaliser pairs the nodes; only PA-VM clusters are rendered, any other vendor is an error."""
    cleaned = _vc([_entry("firewall", 2, _FG_VM)], _devices("fw-am-ne", "firewall", 2))

    with pytest.raises(ValueError, match=r"only .*PA-VM"):
        render_equinix_network_devices(normalise_virtual_colocation(cleaned))


def test_three_firewalls_are_refused() -> None:
    """A cluster has two nodes."""
    cleaned = _vc([_entry("firewall", 3, _PA_VM)], _devices("fw-am-ne", "firewall", 3))

    with pytest.raises(ValueError, match="3 firewalls"):
        normalise_virtual_colocation(cleaned)


def test_load_balancer_is_refused() -> None:
    """Load balancers are not ordered through Network Edge here; refuse instead of omitting them silently."""
    cleaned = _vc(
        [_entry("edge", 2, _C8000V), _entry("load-balancer", 1, _C8000V)],
        [*_devices("eg-am-ne", "edge", 2), *_devices("lb-am-ne", "load-balancer", 1)],
    )

    with pytest.raises(ValueError, match="load-balancer"):
        normalise_virtual_colocation(cleaned)


def test_two_templates_for_one_role_are_ambiguous() -> None:
    """Devices do not link to their template, so two templates of one role cannot be told apart."""
    cleaned = _vc(
        [_entry("edge", 1, _C8000V), _entry("edge", 1, {**_C8000V, "template_name": "C8000V_EDGE_BIG"})],
        _devices("eg-am-ne", "edge", 2),
    )

    with pytest.raises(ValueError, match=r"ambiguous.*C8000V_EDGE.*C8000V_EDGE_BIG"):
        normalise_virtual_colocation(cleaned)


def test_two_entries_of_the_same_template_are_not_ambiguous() -> None:
    """The same template listed twice (two quantities) is one template."""
    cleaned = _vc([_entry("edge", 1, _C8000V), _entry("edge", 1, _C8000V)], _devices("eg-am-ne", "edge", 2))

    assert len(normalise_virtual_colocation(cleaned)["devices"]) == 2


def test_devices_of_a_role_without_a_template_entry_are_refused() -> None:
    """Without an entry there is nothing to take the order from."""
    cleaned = _vc([], _devices("eg-am-ne", "edge", 2))

    with pytest.raises(ValueError, match="no fabric_templates entry"):
        normalise_virtual_colocation(cleaned)


def test_entry_naming_no_virtual_template_is_refused() -> None:
    """A physical template yields no template_name through the virtual fragment; name the role."""
    cleaned = _vc([_entry("edge", 2, {"id": "t-physical"})], _devices("eg-am-ne", "edge", 2))

    with pytest.raises(ValueError, match="'edge' entry of fabric_templates"):
        normalise_virtual_colocation(cleaned)


def test_missing_ordering_parameter_is_reported_by_the_renderer_per_device() -> None:
    """A template without a package code leaves it empty; the renderer names the device and the field."""
    template = {key: item for key, item in _C8000V.items() if key != "provider_package_code"}
    cleaned = _vc([_entry("edge", 1, template)], _devices("eg-am-ne", "edge", 1))

    with pytest.raises(ValueError, match="eg-am-ne01.*package_code"):
        render_equinix_network_devices(normalise_virtual_colocation(cleaned))


def test_non_equinix_operator_is_refused_by_the_renderer() -> None:
    """The provider is the colocation operator above the metro; Megaport is not rendered."""
    cleaned = _vc([_entry("edge", 2, _C8000V)], _devices("eg-am-ne", "edge", 2), operator="MEGAPORT")

    vc = normalise_virtual_colocation(cleaned)

    assert vc["provider"] == "megaport"
    with pytest.raises(ValueError, match="only Equinix"):
        render_equinix_network_devices(vc)


def test_presence_without_a_metro_or_operator_is_refused() -> None:
    """The metro code and the provider both come from the parents; without them nothing can be ordered."""
    cleaned = _edge_pair()
    cleaned["TopologyVirtualColocation"][0]["parent"] = {"name": "AM", "parent": None}

    with pytest.raises(ValueError, match="no parent metro"):
        normalise_virtual_colocation(cleaned)


def test_empty_query_result_is_refused() -> None:
    """An instance that matched nothing is an error, not an empty artifact."""
    with pytest.raises(ValueError, match="no TopologyVirtualColocation"):
        normalise_virtual_colocation({"TopologyVirtualColocation": []})


def test_normaliser_does_not_modify_its_input() -> None:
    """Pure: the cleaned result is the same after the call."""
    cleaned = _edge_pair_and_firewall_cluster()
    snapshot = copy.deepcopy(cleaned)

    normalise_virtual_colocation(cleaned)

    assert cleaned == snapshot


# ---------------------------------------------------------------- transform


def test_transform_renders_the_ha_pair_from_a_graphql_result() -> None:
    """End to end from the raw query response: one resource, the second edge as its secondary device."""
    hcl = _run(_raw(_edge_pair()))

    assert hcl.count('resource "equinix_network_device"') == 1
    assert 'resource "equinix_network_device" "am_ne_eg_am_ne01" {' in hcl
    assert 'name           = "eg-am-ne01"' in hcl
    assert 'metro_code     = "AM"' in hcl
    assert 'type_code      = "C8000V"' in hcl
    assert 'package_code   = "network-essentials"' in hcl
    assert 'version        = "17.12.01a"' in hcl
    assert "core_count     = 2" in hcl
    assert "term_length    = 12" in hcl
    assert 'name           = "eg-am-ne02"' in hcl.split("secondary_device {")[1]


def test_transform_renders_edge_pair_and_firewall_cluster() -> None:
    """The mixed presence gives two resources and the cluster's licence variables."""
    hcl = _run(_raw(_edge_pair_and_firewall_cluster()))

    assert hcl.count('resource "equinix_network_device"') == 2
    assert 'cluster_name = "fw-am-ne-cluster"' in hcl
    assert "license_token = var.am_ne_fw_am_ne_node0_license_token" in hcl
    assert "license_token = var.am_ne_fw_am_ne_node1_license_token" in hcl


def test_transform_prints_the_order_data_of_the_presence_and_no_secret_values() -> None:
    """The account and the notification list are the presence's data; licences stay Terraform variables."""
    hcl = _run(_raw(_edge_pair_and_firewall_cluster()))

    assert 'account_number = "1000001"' in hcl
    assert 'notifications  = ["noc@example.com", "network-ops@example.com"]' in hcl
    assert "var.equinix_account_number" not in hcl
    assert hcl.count("sensitive = true") == 2
    lowered = hcl.lower()
    assert "password" not in lowered
    assert "ssh_key" not in lowered
    assert '= "' not in "".join(line for line in hcl.splitlines() if "license_token" in line)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda vc: vc.update(provider_account_number=None),
        lambda vc: vc.update(owner=None),
        lambda vc: vc["owner"].update(notification_emails=[]),
    ],
    ids=["no-account", "no-owner", "owner-without-emails"],
)
def test_presence_without_order_data_is_refused_by_the_transform(mutation: Any) -> None:
    """A virtual colocation routed to terraform without its account or an owner's addresses renders no file."""
    cleaned = _edge_pair()
    mutation(cleaned["TopologyVirtualColocation"][0])

    with pytest.raises(ValueError, match="account_number .* and at\\s+least one notification"):
        _run(_raw(cleaned))


def test_transform_is_idempotent() -> None:
    """The same query result renders the same bytes."""
    raw = _raw(_edge_pair_and_firewall_cluster())

    assert _run(raw) == _run(copy.deepcopy(raw))


@pytest.mark.parametrize("orchestrator", ["manual", "ansible_automation_platform", "argocd", "github_actions"])
def test_transform_refuses_a_presence_not_routed_to_terraform(orchestrator: str) -> None:
    """Only a presence whose orchestrator is terraform gets a Terraform file."""
    cleaned = _edge_pair()
    cleaned["TopologyVirtualColocation"][0]["infrastructure_orchestrator"] = orchestrator

    with pytest.raises(ValueError, match=f"{orchestrator!r}.*not 'terraform'"):
        _run(_raw(cleaned))


def test_devices_fewer_than_the_declared_quantity_are_refused() -> None:
    """The generator stopped midway (one of two edges exists): rendering one edge would order half a pair."""
    cleaned = _vc([_entry("edge", 2, _C8000V)], _devices("eg-am-ne", "edge", 1))

    with pytest.raises(ValueError, match=r"do not match the fabric_templates quantities.*'edge': \(1, 2\)"):
        normalise_virtual_colocation(cleaned)


def test_quantities_of_two_entries_of_one_role_add_up() -> None:
    """Two entries of one template (1 + 1) declare two edges: an HA pair, not a mismatch."""
    cleaned = _vc([_entry("edge", 1, _C8000V), _entry("edge", 1, _C8000V)], _devices("eg-am-ne", "edge", 2))

    assert normalise_virtual_colocation(cleaned)["devices"][0]["ha_peer"] == "eg-am-ne02"
