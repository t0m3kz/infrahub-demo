"""Unit tests for transforms/colocation/virtual_colocation.py: the query result of a TopologyVirtualColocation
becomes the renderer's input (normalise_virtual_colocation) and the transform prints the Equinix Terraform for it.

The devices are CloudNetworkAppliances; the ordering parameters are read from the appliances themselves. An edge is
always a single device. Only a firewall pair is wired, from the ManagedFirewallHA domain (mode active-passive) that
lists the two firewalls as members through their `capabilities` edge.
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
    "service_code": "C8000V",
    "package_code": "network-essentials",
    "software_version": "17.12.01a",
    "cpu_count": 2,
    "term_length": 12,
}
_PA_VM = {
    "service_code": "PA-VM",
    "package_code": "VM100",
    "software_version": "11.1.3",
    "cpu_count": 2,
    "term_length": 12,
}
_FG_VM = {**_PA_VM, "service_code": "FG-VM"}


_BGP = {"id": "bgp-1", "typename": "ManagedBGP", "name": "AM-NE-overlay"}


def _domain_name(first: str, second: str) -> str:
    """The generator's HA domain name: the two member names sorted, then -ha."""
    return "-".join([*sorted([first, second]), "ha"])


def _domain(first: str, second: str, mode: str | None, *, members: list[str] | None = None) -> dict[str, Any]:
    """A cleaned HA domain capability: it carries a `mode`, and its own `capabilities` hold the members."""
    names = members if members is not None else [first, second]
    return {
        "id": f"ha-{_domain_name(first, second)}",
        "typename": "ManagedFirewallHA",
        "name": _domain_name(first, second),
        "mode": mode,
        "capabilities": [{"id": f"d-{name}", "name": name} for name in names],
    }


def _appliance(
    name: str,
    role: str,
    ordering: dict[str, Any],
    *,
    domains: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One cleaned CloudNetworkAppliance of the query result: an overlay ManagedBGP plus its HA domains, if any."""
    return {
        "id": f"d-{name}",
        "typename": "CloudNetworkAppliance",
        "name": name,
        "role": role,
        **ordering,
        "capabilities": [_BGP, *(domains or [])],
    }


def _pair(
    first: str, second: str, role: str, ordering: dict[str, Any], mode: str | None = "active-passive"
) -> list[dict[str, Any]]:
    """Two appliances that are the two members of one HA domain, listed as the query returns them."""
    domain = _domain(first, second, mode)
    return [_appliance(first, role, ordering, domains=[domain]), _appliance(second, role, ordering, domains=[domain])]


def _entry(role: str, quantity: int) -> dict[str, Any]:
    return {"role": role, "quantity": quantity}


def _vc(
    entries: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    *,
    operator: str = "EQUINIX",
) -> dict[str, Any]:
    """The cleaned query result (clean_data output) for virtual colocation AM-NE in metro AM."""
    return {
        "TopologyVirtualColocation": [
            {
                "id": "vc-1",
                "name": "AM-NE",
                "infrastructure_orchestrator": "terraform",
                "account": {
                    "id": "acc-1",
                    "display_label": "AM-NE account",
                    "account_id": "1000001",
                    "provider": {"id": "p-1", "name": operator},
                    "owner": {
                        "id": "o-1",
                        "display_label": "Internal",
                        "notification_emails": ["noc@example.com", "network-ops@example.com"],
                    },
                },
                "parent": {"id": "m-1", "name": "AM", "parent": {"id": "c-1", "name": operator}},
                "fabric_templates": entries,
                "devices": devices,
            }
        ]
    }


def _two_edges() -> dict[str, Any]:
    """Two edge routers, each carrying only its ManagedBGP overlay: two independent single devices."""
    return _vc(
        [_entry("edge", 2)],
        [_appliance("eg-am-ne01", "edge", _C8000V), _appliance("eg-am-ne02", "edge", _C8000V)],
    )


def _firewall_pair() -> dict[str, Any]:
    """Two firewalls that are the two members of one active-passive ManagedFirewallHA."""
    return _vc([_entry("firewall", 2)], _pair("fw-am-ne01", "fw-am-ne02", "firewall", _PA_VM))


def _edges_and_firewall_cluster() -> dict[str, Any]:
    return _vc(
        [_entry("edge", 2), _entry("firewall", 2)],
        [
            _appliance("eg-am-ne01", "edge", _C8000V),
            _appliance("eg-am-ne02", "edge", _C8000V),
            *_pair("fw-am-ne01", "fw-am-ne02", "firewall", _PA_VM),
        ],
    )


def _devices(cleaned: dict[str, Any]) -> list[dict[str, Any]]:
    return cleaned["TopologyVirtualColocation"][0]["devices"]


def _raw(cleaned: dict[str, Any]) -> dict[str, Any]:
    """The GraphQL response shape (value / node / edges wrappers) of a cleaned query result."""

    def value(item: Any) -> dict[str, Any]:
        return {"value": item}

    def node(item: Any) -> dict[str, Any]:
        return {"node": item}

    def capability(item: dict[str, Any]) -> dict[str, Any]:
        raw = {"id": item["id"], "__typename": item["typename"], "name": value(item["name"])}
        if "mode" in item:
            raw["mode"] = value(item["mode"])
            raw["capabilities"] = {
                "edges": [node({"id": member["id"], "name": value(member["name"])}) for member in item["capabilities"]]
            }
        return raw

    def device(item: dict[str, Any]) -> dict[str, Any]:
        raw = {
            "id": item["id"],
            "__typename": item["typename"],
            "name": value(item["name"]),
            "role": value(item["role"]),
        }
        if item["typename"] == "CloudNetworkAppliance":
            raw.update({key: value(item.get(key)) for key in _C8000V})
            raw["capabilities"] = {"edges": [node(capability(entry)) for entry in item["capabilities"]]}
        return raw

    vc = cleaned["TopologyVirtualColocation"][0]
    metro = vc["parent"]
    account = vc["account"]
    return {
        "TopologyVirtualColocation": {
            "edges": [
                node(
                    {
                        "id": vc["id"],
                        "name": value(vc["name"]),
                        "infrastructure_orchestrator": value(vc["infrastructure_orchestrator"]),
                        "account": node(
                            {
                                "id": account["id"],
                                "display_label": account["display_label"],
                                "account_id": value(account["account_id"]),
                                "provider": node(
                                    {"id": account["provider"]["id"], "name": value(account["provider"]["name"])}
                                ),
                                "owner": node(
                                    {
                                        "id": account["owner"]["id"],
                                        "display_label": account["owner"]["display_label"],
                                        "notification_emails": value(account["owner"]["notification_emails"]),
                                    }
                                )
                                if account.get("owner")
                                else {"node": None},
                            }
                        )
                        if account
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
                                node({"role": value(entry["role"]), "quantity": value(entry["quantity"])})
                                for entry in vc["fabric_templates"]
                            ]
                        },
                        "devices": {"edges": [node(device(item)) for item in vc["devices"]]},
                    }
                )
            ]
        }
    }


def _run(raw: dict[str, Any]) -> str:
    transform = VirtualColocationTerraform(client=MagicMock(), infrahub_node=MagicMock(), root_directory=".")
    return asyncio.run(transform.transform(raw))


# ---------------------------------------------------------------- normaliser


def test_two_edges_are_two_independent_single_devices() -> None:
    """Edges have no HA domain: each is its own device, with no peer and no placement relation to the other."""
    vc = normalise_virtual_colocation(_two_edges())

    assert vc["name"] == "AM-NE"
    assert vc["provider"] == "equinix"
    first, second = vc["devices"]
    assert first == {
        "name": "eg-am-ne01",
        "metro_code": "AM",
        "type_code": "C8000V",
        "package_code": "network-essentials",
        "version": "17.12.01a",
        "core_count": 2,
        "term_length": 12,
    }
    assert second == {**first, "name": "eg-am-ne02"}


def test_query_order_does_not_change_the_result() -> None:
    """The output is sorted by device name, not by the order of the query."""
    cleaned = _edges_and_firewall_cluster()
    expected = normalise_virtual_colocation(copy.deepcopy(cleaned))
    _devices(cleaned).reverse()

    assert normalise_virtual_colocation(cleaned) == expected


def test_single_edge_with_only_a_bgp_capability_is_a_plain_device() -> None:
    """An appliance whose only capability is the ManagedBGP overlay (no mode) is a single device."""
    cleaned = _vc([_entry("edge", 1)], [_appliance("eg-am-ne01", "edge", _C8000V)])

    vc = normalise_virtual_colocation(cleaned)

    assert len(vc["devices"]) == 1
    assert "cluster" not in vc["devices"][0]


def test_single_appliance_without_any_capability_is_a_plain_device() -> None:
    """No capabilities at all (an empty edge list) is a single device too."""
    device = _appliance("fw-am-ne01", "firewall", _PA_VM)
    device["capabilities"] = []

    assert "cluster" not in normalise_virtual_colocation(_vc([_entry("firewall", 1)], [device]))["devices"][0]


def test_firewall_pair_is_a_cluster_named_after_the_ha_domain() -> None:
    """An active-passive domain is a cluster: its name is the domain's, node 0/1 the index by sorted member name."""
    vc = normalise_virtual_colocation(_firewall_pair())

    clusters = {device["name"]: device["cluster"] for device in vc["devices"]}

    assert clusters == {
        "fw-am-ne01": {"name": "fw-am-ne01-fw-am-ne02-ha", "node": 0},
        "fw-am-ne02": {"name": "fw-am-ne01-fw-am-ne02-ha", "node": 1},
    }
    assert {device["type_code"] for device in vc["devices"]} == {"PA-VM"}


def test_cluster_node_numbering_follows_sorted_names_when_the_query_lists_the_second_member_first() -> None:
    """The primary (node 0) is the first member by name, whatever order the query returned the devices in."""
    cleaned = _firewall_pair()
    _devices(cleaned).reverse()

    nodes = {device["name"]: device["cluster"]["node"] for device in normalise_virtual_colocation(cleaned)["devices"]}

    assert nodes == {"fw-am-ne01": 0, "fw-am-ne02": 1}


def test_mixed_presence_has_two_single_edges_and_one_cluster() -> None:
    """The edges are separate devices, the firewalls the two nodes of a cluster."""
    vc = normalise_virtual_colocation(_edges_and_firewall_cluster())

    edges = [device for device in vc["devices"] if device["name"].startswith("eg-")]
    firewalls = [device for device in vc["devices"] if device["name"].startswith("fw-")]
    assert all("cluster" not in device for device in edges)
    assert [device["cluster"] for device in firewalls] == [
        {"name": "fw-am-ne01-fw-am-ne02-ha", "node": 0},
        {"name": "fw-am-ne01-fw-am-ne02-ha", "node": 1},
    ]
    assert not any("ha_peer" in device or "diverse_from" in device for device in vc["devices"])


def test_firewall_cluster_of_another_vendor_is_refused_by_the_renderer() -> None:
    """The normaliser maps the domain; only PA-VM clusters are rendered, any other vendor is an error."""
    cleaned = _vc([_entry("firewall", 2)], _pair("fw-am-ne01", "fw-am-ne02", "firewall", _FG_VM))

    with pytest.raises(ValueError, match=r"only .*PA-VM"):
        render_equinix_network_devices(normalise_virtual_colocation(cleaned))


def test_any_role_is_rendered_when_it_is_an_appliance() -> None:
    """The ordering data lives on each appliance, so there is no role restriction."""
    cleaned = _vc(
        [_entry("edge", 1), _entry("load-balancer", 1)],
        [_appliance("eg-am-ne01", "edge", _C8000V), _appliance("lb-am-ne01", "load-balancer", _C8000V)],
    )

    assert [device["name"] for device in normalise_virtual_colocation(cleaned)["devices"]] == [
        "eg-am-ne01",
        "lb-am-ne01",
    ]


def test_account_and_notifications_come_from_the_model() -> None:
    """account_number is the account's account_id, notifications those of the account's owner."""
    vc = normalise_virtual_colocation(_two_edges())

    assert vc["account_number"] == "1000001"
    assert vc["notifications"] == ["noc@example.com", "network-ops@example.com"]


def test_missing_ordering_parameter_is_reported_by_the_renderer_per_device() -> None:
    """An appliance without a package code leaves it empty; the renderer names the device and the field."""
    ordering = {key: item for key, item in _C8000V.items() if key != "package_code"}
    cleaned = _vc([_entry("edge", 1)], [_appliance("eg-am-ne01", "edge", ordering)])

    with pytest.raises(ValueError, match="eg-am-ne01.*package_code"):
        render_equinix_network_devices(normalise_virtual_colocation(cleaned))


def test_non_equinix_operator_is_refused_by_the_renderer() -> None:
    """The provider is the colocation operator above the metro; Megaport is not rendered."""
    cleaned = _two_edges()
    cleaned["TopologyVirtualColocation"][0]["parent"]["parent"]["name"] = "MEGAPORT"

    vc = normalise_virtual_colocation(cleaned)

    assert vc["provider"] == "megaport"
    with pytest.raises(ValueError, match="only Equinix"):
        render_equinix_network_devices(vc)


def test_presence_without_a_metro_or_operator_is_refused() -> None:
    """The metro code and the provider both come from the parents; without them nothing can be ordered."""
    cleaned = _two_edges()
    cleaned["TopologyVirtualColocation"][0]["parent"] = {"name": "AM", "parent": None}

    with pytest.raises(ValueError, match="no parent metro"):
        normalise_virtual_colocation(cleaned)


def test_empty_query_result_is_refused() -> None:
    """An instance that matched nothing is an error, not an empty artifact."""
    with pytest.raises(ValueError, match="no TopologyVirtualColocation"):
        normalise_virtual_colocation({"TopologyVirtualColocation": []})


def test_normaliser_does_not_modify_its_input() -> None:
    """Pure: the cleaned result is the same after the call."""
    cleaned = _edges_and_firewall_cluster()
    snapshot = copy.deepcopy(cleaned)

    normalise_virtual_colocation(cleaned)

    assert cleaned == snapshot


# ---------------------------------------------------------------- refusals


def test_device_that_is_not_an_appliance_is_refused() -> None:
    """A DcimVirtualDevice (or anything else) is not provider-hosted and carries no order data."""
    cleaned = _vc(
        [_entry("edge", 1)], [{"id": "d-1", "typename": "DcimVirtualDevice", "name": "eg-am-ne01", "role": "edge"}]
    )

    with pytest.raises(ValueError, match="eg-am-ne01 is a DcimVirtualDevice, not a CloudNetworkAppliance"):
        normalise_virtual_colocation(cleaned)


def test_device_in_two_ha_domains_is_refused() -> None:
    """A device belongs to at most one HA domain."""
    cleaned = _firewall_pair()
    other = _domain("fw-am-ne01", "fw-am-ne03", "active-passive")
    _devices(cleaned)[0]["capabilities"].append(other)

    with pytest.raises(ValueError, match="fw-am-ne01 is a member of several HA domains"):
        normalise_virtual_colocation(cleaned)


@pytest.mark.parametrize(
    "members",
    [["fw-am-ne01"], ["fw-am-ne01", "fw-am-ne02", "fw-am-ne03"], []],
    ids=["one-member", "three-members", "no-members"],
)
def test_domain_without_exactly_two_members_is_refused(members: list[str]) -> None:
    """An HA pair has two members: one, three or none cannot be ordered."""
    cleaned = _firewall_pair()
    for device in _devices(cleaned):
        device["capabilities"][1] = _domain("fw-am-ne01", "fw-am-ne02", "active-passive", members=members)

    with pytest.raises(ValueError, match=r"HA domain fw-am-ne01-fw-am-ne02-ha of fw-am-ne0\d has members"):
        normalise_virtual_colocation(cleaned)


def test_domain_that_does_not_list_the_device_is_refused() -> None:
    """Two members, but the device itself is not one of them."""
    cleaned = _firewall_pair()
    _devices(cleaned)[0]["capabilities"][1] = _domain("fw-am-ne02", "fw-am-ne03", "active-passive")

    with pytest.raises(ValueError, match="of fw-am-ne01 has members"):
        normalise_virtual_colocation(cleaned)


def test_member_outside_the_presence_is_refused() -> None:
    """The partner named by the domain must be a device of this presence."""
    cleaned = _firewall_pair()
    vc = cleaned["TopologyVirtualColocation"][0]
    vc["devices"] = vc["devices"][:1]
    vc["fabric_templates"] = [_entry("edge", 1)]

    with pytest.raises(ValueError, match="fw-am-ne01 is in HA domain .* with fw-am-ne02, which is not a device"):
        normalise_virtual_colocation(cleaned)


def test_partner_not_listing_the_same_domain_is_refused() -> None:
    """The partner is in the presence but carries no such domain: the pair is inconsistent."""
    cleaned = _firewall_pair()
    _devices(cleaned)[1]["capabilities"] = [_BGP]

    with pytest.raises(
        ValueError, match="fw-am-ne01 is in HA domain fw-am-ne01-fw-am-ne02-ha but its partner fw-am-ne02 is not"
    ):
        normalise_virtual_colocation(cleaned)


def test_partner_in_another_domain_is_refused() -> None:
    """The partner has an HA domain, but a different one."""
    cleaned = _firewall_pair()
    _devices(cleaned)[1]["capabilities"][1] = _domain("fw-am-ne02", "fw-am-ne09", "active-passive")

    with pytest.raises(ValueError, match="its partner fw-am-ne02 is not"):
        normalise_virtual_colocation(cleaned)


@pytest.mark.parametrize("mode", ["active-active", "cloud_managed", "active-standby", None])
def test_mode_other_than_active_passive_is_refused(mode: str | None) -> None:
    """Only a cluster (active-passive) is rendered; any other mode is refused naming device, domain and mode."""
    cleaned = _vc([_entry("firewall", 2)], _pair("fw-am-ne01", "fw-am-ne02", "firewall", _PA_VM, mode))

    with pytest.raises(
        ValueError,
        match=rf"fw-am-ne01 is in HA domain fw-am-ne01-fw-am-ne02-ha with mode {mode!r}; only a cluster \('active-passive'\)",
    ):
        normalise_virtual_colocation(cleaned)


def test_edge_in_an_active_active_domain_is_refused() -> None:
    """A router has no redundancy representation: an active-active domain on an edge is an error, not a pair."""
    cleaned = _vc([_entry("edge", 2)], _pair("eg-am-ne01", "eg-am-ne02", "edge", _C8000V, "active-active"))

    with pytest.raises(
        ValueError, match=r"eg-am-ne01 is in HA domain eg-am-ne01-eg-am-ne02-ha with mode 'active-active'"
    ):
        normalise_virtual_colocation(cleaned)


def test_devices_fewer_than_the_declared_quantity_are_refused() -> None:
    """The generator stopped midway (one of two edges exists): rendering one edge would order half a pair."""
    cleaned = _vc([_entry("edge", 2)], [_appliance("eg-am-ne01", "edge", _C8000V)])

    with pytest.raises(ValueError, match=r"do not match the fabric_templates quantities.*'edge': \(1, 2\)"):
        normalise_virtual_colocation(cleaned)


def test_quantities_of_two_entries_of_one_role_add_up() -> None:
    """Two entries of one role (1 + 1) declare two edges, not a mismatch."""
    cleaned = _vc([_entry("edge", 1), _entry("edge", 1)], _two_edges()["TopologyVirtualColocation"][0]["devices"])

    assert len(normalise_virtual_colocation(cleaned)["devices"]) == 2


# ---------------------------------------------------------------- transform


def test_transform_renders_two_edges_as_two_resources_from_a_graphql_result() -> None:
    """End to end from the raw query response: one independent resource per edge, no redundancy blocks."""
    hcl = _run(_raw(_two_edges()))

    assert hcl.count('resource "equinix_network_device"') == 2
    assert 'resource "equinix_network_device" "am_ne_eg_am_ne01" {' in hcl
    assert 'resource "equinix_network_device" "am_ne_eg_am_ne02" {' in hcl
    assert 'name           = "eg-am-ne01"' in hcl
    assert 'name           = "eg-am-ne02"' in hcl
    assert 'metro_code     = "AM"' in hcl
    assert 'type_code      = "C8000V"' in hcl
    assert 'package_code   = "network-essentials"' in hcl
    assert 'version        = "17.12.01a"' in hcl
    assert "core_count     = 2" in hcl
    assert "term_length    = 12" in hcl
    assert "secondary_device" not in hcl
    assert "diverse_device_id" not in hcl


def test_transform_renders_edges_and_firewall_cluster() -> None:
    """The mixed presence gives three resources (two edges, one cluster) and the cluster's licence variables."""
    hcl = _run(_raw(_edges_and_firewall_cluster()))

    assert hcl.count('resource "equinix_network_device"') == 3
    assert 'cluster_name = "fw-am-ne01-fw-am-ne02-ha-cluster"' in hcl
    assert "license_token = var.am_ne_fw_am_ne01_fw_am_ne02_ha_node0_license_token" in hcl
    assert "license_token = var.am_ne_fw_am_ne01_fw_am_ne02_ha_node1_license_token" in hcl


def test_transform_numbers_cluster_nodes_by_sorted_name_when_the_query_lists_the_second_firewall_first() -> None:
    """The raw result lists fw-am-ne02 first: node0 is still fw-am-ne01."""
    cleaned = _firewall_pair()
    _devices(cleaned).reverse()

    hcl = _run(_raw(cleaned))

    node0 = hcl.split("node0 {")[1].split("node1 {")[0]
    node1 = hcl.split("node1 {")[1]
    assert 'hostname = "fw-am-ne01"' in node0
    assert 'hostname = "fw-am-ne02"' in node1


def test_transform_prints_the_order_data_of_the_account_and_no_secret_values() -> None:
    """The account number and the owner's notification list are model data; licences stay Terraform variables."""
    hcl = _run(_raw(_edges_and_firewall_cluster()))

    assert 'account_number = "1000001"' in hcl
    assert 'notifications  = ["noc@example.com", "network-ops@example.com"]' in hcl
    assert "var.equinix_account_number" not in hcl
    assert hcl.count("sensitive = true") == 2
    lowered = hcl.lower()
    assert "password" not in lowered
    assert "ssh_key" not in lowered
    assert '= "' not in "".join(line for line in hcl.splitlines() if "license_token" in line)


def test_account_without_notification_emails_is_refused_by_the_renderer() -> None:
    """An owner with no e-mail addresses leaves the order without a notification list."""
    cleaned = _two_edges()
    cleaned["TopologyVirtualColocation"][0]["account"]["owner"]["notification_emails"] = []

    with pytest.raises(
        ValueError, match=r"account_id of the presence's account.*notification_emails of that account's owner"
    ):
        _run(_raw(cleaned))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda account: account.update(account_id=None),
        lambda account: account.update(owner=None),
    ],
    ids=["no-account-id", "no-owner"],
)
def test_account_without_order_data_is_refused_by_the_transform(mutation: Any) -> None:
    """A presence routed to terraform without an account number or an owner renders no file."""
    cleaned = _two_edges()
    mutation(cleaned["TopologyVirtualColocation"][0]["account"])

    with pytest.raises(ValueError, match=r"account_number .* and at\s+least one notification"):
        _run(_raw(cleaned))


def test_presence_without_an_account_is_refused_by_the_transform() -> None:
    """No account relationship: no account number and no notification list."""
    cleaned = _two_edges()
    cleaned["TopologyVirtualColocation"][0]["account"] = None
    raw = _raw(cleaned)
    raw["TopologyVirtualColocation"]["edges"][0]["node"]["account"] = {"node": None}

    with pytest.raises(ValueError, match=r"account_number .* and at\s+least one notification"):
        _run(raw)


def test_transform_is_idempotent() -> None:
    """The same query result renders the same bytes."""
    raw = _raw(_edges_and_firewall_cluster())

    assert _run(raw) == _run(copy.deepcopy(raw))


@pytest.mark.parametrize("orchestrator", ["manual", "ansible_automation_platform", "argocd", "github_actions"])
def test_transform_refuses_a_presence_not_routed_to_terraform(orchestrator: str) -> None:
    """Only a presence whose orchestrator is terraform gets a Terraform file."""
    cleaned = _two_edges()
    cleaned["TopologyVirtualColocation"][0]["infrastructure_orchestrator"] = orchestrator

    with pytest.raises(ValueError, match=f"{orchestrator!r}.*not 'terraform'"):
        _run(_raw(cleaned))
