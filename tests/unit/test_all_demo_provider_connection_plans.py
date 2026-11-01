"""Data x generator gate: every provider connection of 30_all plans without error, offline.

The payload ``add_provider_connection`` would read (queries/topology/add/provider_connection.gql, cleaned) is built from
the YAML of ``08_interconnects/05_provider_connections`` and the objects it refers to (carriers, devices, deployments,
BGP processes, accounts, providers), then handed to the pure planner (generators/helpers/provider_connection.py). What
this proves, and the loader and the unit tests of the planner cannot: each connection of the data yields the session
circuits, ends and link prefixes the data comments document, with no planner refusal.

Devices the colocation generators create (the cage edges ``eg-*`` and the Network Edge routers) are not in the files: their
deployment is the zone they sit in, and a Network Edge router carries the one overlay ``ManagedBGP`` that
add_virtual_colocation generates. Everything else is read from the data.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from generators.helpers.provider_connection import (
    ConnectionSpec,
    DedicatedEnds,
    LinkPlan,
    parse_connection,
    plan_links,
    port_of,
    reused_ends,
    validate_spec,
)
from tasks import ALL_DEMO_DATA
from tests.integration.demo_data import ROOT, declared, distinct, name, object_templates
from utils.provider_services import ADDRESS_SOURCE_PROVIDER

_ALL = ROOT / ALL_DEMO_DATA
_BOOTSTRAP = ROOT / "data" / "bootstrap"

#: Devices the generators create: device -> the zone it is deployed in (a Network Edge router sits in AM-NE).
_GENERATED_DEPLOYMENT = {
    "eg-fr201": "FR2",
    "eg-fr202": "FR2",
    "eg-pa401": "PA4",
    "eg-pa402": "PA4",
    "eg-am-ne01": "AM-NE",
    "eg-am-ne02": "AM-NE",
}
#: The Network Edge routers run the overlay process add_virtual_colocation generates (no authored AS).
_GENERATED_PROCESS = {"eg-am-ne01", "eg-am-ne02"}


def _providers() -> dict[str, dict[str, Any]]:
    """org_id -> the OrganizationProvider row."""
    return {str(p.item["org_id"]): p.item for p in declared("OrganizationProvider", _BOOTSTRAP)}


def _provider_ref(org_id: str | None) -> dict[str, Any] | None:
    """A provider as the query returns it (id, name, connection_profile)."""
    row = _providers().get(str(org_id)) if org_id else None
    if row is None:
        return None
    return {"id": row["org_id"], "name": row["name"], "connection_profile": row.get("connection_profile")}


def _processes() -> dict[str, list[dict[str, Any]]]:
    """Device -> the ManagedBGP processes it carries, as the query lists them among its capabilities."""
    found: dict[str, list[dict[str, Any]]] = {}
    for process in declared("ManagedBGP", _ALL):
        for device in process.item["capabilities"]:
            found.setdefault(device, []).append(
                {
                    "id": name(process),
                    "typename": "ManagedBGP",
                    "name": name(process),
                    "local_as": process.item["local_as"],
                }
            )
    for device in _GENERATED_PROCESS:
        found.setdefault(device, []).append(
            {"id": f"{device}-bgp-overlay", "typename": "ManagedBGP", "name": f"{device}-bgp-overlay", "local_as": None}
        )
    return found


def _devices() -> dict[str, dict[str, Any]]:
    """Device name -> its row (physical devices and appliances), with the deployment of the generated ones."""
    rows = {
        name(d): d.item
        for kind in ("DcimPhysicalDevice", "CloudNetworkAppliance")
        for d in distinct(declared(kind, _ALL))
        if d.item.get("deployment")
    }
    rows |= {device: {"deployment": zone} for device, zone in _GENERATED_DEPLOYMENT.items()}
    return rows


def _device(device: str) -> dict[str, Any]:
    """A device as a carrier end or a far stub shows it: id, name, deployment, owner and processes."""
    row = _devices()[device]
    owner = _provider_ref(row.get("owner"))
    return {
        "id": device,
        "name": device,
        "deployment": {"id": row["deployment"], "typename": "TopologyDeployment"},
        "owner": {**owner, "typename": "OrganizationProvider"} if owner else None,
        "capabilities": _processes().get(device, []),
    }


def _carriers() -> dict[str, dict[str, Any]]:
    """Carrier circuit_id -> its row."""
    return {str(c.item["circuit_id"]): c.item for c in declared("TopologyCarrierCircuit", _ALL)}


def _carrier(carrier_name: str) -> dict[str, Any]:
    """A carrier as the query returns it: its ends are interfaces `<device>/<port>` with their device."""
    row = _carriers()[carrier_name]
    return {
        "id": carrier_name,
        "name": carrier_name,
        "typename": "TopologyCarrierCircuit",
        "circuit_type": row["circuit_type"],
        "status": row["status"],
        "capabilities": [
            {"id": f"{device}/{port}", "name": port, "device": _device(device)} for device, port in row["capabilities"]
        ],
    }


def _connections() -> list[dict[str, Any]]:
    """The connection rows of the data."""
    return [c.item for c in distinct(declared("TopologyProviderConnection", _ALL))]


def _far_provider(row: dict[str, Any]) -> dict[str, Any]:
    """The pieces of the payload that give the provider: the connection's account, the account a virtual colocation
    is billed to, or the SaaS region's parent (a cloud-neutral zone has none: the owner of its kit decides)."""
    provider: dict[str, Any] = {"account": None, "far_account": None, "far_parent": None}
    accounts = {name(a): a.item for a in declared("CloudAccount", _ALL)}
    if row.get("account"):
        provider["account"] = {"provider": _provider_ref(accounts[row["account"]]["provider"])}
    for colocation in declared("TopologyVirtualColocation", _ALL):
        if name(colocation) == row["far"] and colocation.item.get("account"):
            billed = accounts[colocation.item["account"]]
            provider["far_account"] = {"provider": _provider_ref(billed["provider"])}
    regions = {
        f"{region.enclosing.item['name']}-{region.item['region_name']}".upper(): region.enclosing.item["provider"]
        for region in declared("TopologySaasRegion", _ALL)
        if region.enclosing is not None
    }
    if row["far"] in regions:
        provider["far_parent"] = {"provider": _provider_ref(regions[row["far"]])}
    return provider


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    """The cleaned query payload of one connection row."""
    provider = _far_provider(row)
    near, far = row["near"], row["far"]
    internet = [
        _carrier(circuit_id)
        for circuit_id, carrier in _carriers().items()
        if carrier["circuit_type"] == "internet" and near in carrier.get("locations", [])
    ]
    stubs = [_device(device) for device, device_row in _devices().items() if device_row["deployment"] == far]
    # The planner reads `appliances` and `devices` alike; a cloud region has no `devices` key at all.
    far_node: dict[str, Any] = {"id": far, "typename": "TopologyFacility", "appliances": stubs}
    if provider["far_parent"]:
        far_node["parent"] = provider["far_parent"]
    if provider["far_account"]:
        far_node["account"] = provider["far_account"]
    return {
        "id": row["name"],
        "name": row["name"],
        "path_type": row["path_type"],
        "status": row["status"],
        "vlan_id": row.get("vlan_id"),
        "ip_namespace": row["ip_namespace"],
        "provider_as": row.get("provider_as"),
        "password": row.get("password"),
        "link_prefixes": [{"id": f"{prefix}|{ns}", "prefix": prefix} for prefix, ns in row.get("link_prefixes") or []],
        "account": provider["account"],
        "near_port": f"{row['near_port'][0]}/{row['near_port'][1]}" if row.get("near_port") else None,
        "near": {
            "id": near,
            "devices": [
                {"id": device, "name": device}
                for device, device_row in _devices().items()
                if device_row["deployment"] == near
            ],
            "near_connections": [],
            "circuits": [
                {"id": c["id"], "typename": "TopologyCarrierCircuit", **{k: v for k, v in c.items() if k != "typename"}}
                for c in internet
            ],
        },
        "far": far_node,
        "carrier_circuits": [],
        "session_circuits": [],
    }


def _adopted_ends(spec: ConnectionSpec, near_port: str) -> DedicatedEnds:
    """The ends of a dedicated link, as the generator decides them for an explicit near port: the existing carrier whose
    end is that port is adopted, its far end is the stub's port."""
    ((carrier_name, ends),) = [
        (circuit_id, row["capabilities"])
        for circuit_id, row in _carriers().items()
        if near_port in {f"{device}/{port}" for device, port in row["capabilities"]}
    ]
    ports = [port_of(spec, f"{device}/{port}", port, device, device) for device, port in ends]
    return reused_ends(spec, carrier_name, ports)


def _plan(row: dict[str, Any]) -> list[LinkPlan]:
    """The links the generator would build for one connection row."""
    spec = parse_connection(_payload(row))
    ends = _adopted_ends(spec, spec.near_port_id or "") if row["path_type"] == "dedicated" else None
    _, links = plan_links(spec, ends)
    return links


def _ends(link: LinkPlan) -> tuple[str, str]:
    """(near, far) as `<device>/<port>`: the carrier port a sub-interface rides, the uplink a tunnel rides on."""
    assert link.near.port is not None
    far = f"{link.far.device_name}/{link.far.port.name}" if link.far.port else link.far.device_name
    return link.near.port.label, far


#: connection -> the session circuits documented in the data comments: (name, near, far, link prefix or None)
_EXPECTED: dict[str, list[tuple[str, str, str, str | None]]] = {
    "PC-FR2-AWS-EUC1-PROD": [("PC-FR2-AWS-EUC1-PROD-L1", "eg-fr201/Ethernet1/13", "EXT-AWS-DX-FR2/vnic-fr2", None)],
    "PC-FR2-AMNE-PROD": [("PC-FR2-AMNE-PROD-L1", "eg-fr201/Ethernet1/1", "eg-am-ne01/Ethernet1/1", None)],
    "PC-AMNE-AWS-EUC1-PROD": [("PC-AMNE-AWS-EUC1-PROD-L1", "eg-am-ne02/Ethernet1/1", "EXT-AWS-DX-FR2/vnic-amne", None)],
    "PC-PA4-MEGAPORT-PROD": [("PC-PA4-MEGAPORT-PROD-L1", "eg-pa401/Ethernet1/1", "EXT-MEGAPORT-MCR1/et-0/0/0", None)],
    "PC-PA4-AZURE-WEU-PRI-PROD": [
        ("PC-PA4-AZURE-WEU-PRI-PROD-L1", "eg-pa401/Ethernet1/13", "EXT-AZURE-ER-PA4/vnic-pri", "10.255.72.0/30")
    ],
    "PC-PA4-AZURE-WEU-SEC-PROD": [
        ("PC-PA4-AZURE-WEU-SEC-PROD-L1", "eg-pa402/Ethernet1/13", "EXT-AZURE-ER-PA4/vnic-sec", "10.255.72.4/30")
    ],
    # the tunnels leave by eg-fr201's Lumen internet port, and end on the VPN gateway appliance / the Zscaler node
    "PC-FR2-AWS-EUC1-VPN-PROD": [
        ("PC-FR2-AWS-EUC1-VPN-PROD-L1", "eg-fr201/Ethernet1/14", "EXT-AWS-VPN-EUC1", "169.254.10.0/30"),
        ("PC-FR2-AWS-EUC1-VPN-PROD-L2", "eg-fr201/Ethernet1/14", "EXT-AWS-VPN-EUC1", "169.254.10.4/30"),
    ],
    "PC-FR2-ZSCALER-MUC-PROD": [
        ("PC-FR2-ZSCALER-MUC-PROD-L1", "eg-fr201/Ethernet1/14", "EXT-ZSCALER-MUC1", None),
        ("PC-FR2-ZSCALER-MUC-PROD-L2", "eg-fr201/Ethernet1/14", "EXT-ZSCALER-MUC1", None),
    ],
}


def test_the_expectations_name_every_connection_of_the_data() -> None:
    """A connection added to the data needs its documented plan here."""
    assert {row["name"] for row in _connections()} == set(_EXPECTED)


@pytest.mark.parametrize("connection_name", sorted(_EXPECTED))
def test_each_connection_plans_the_documented_sessions_ends_and_link_prefixes(connection_name: str) -> None:
    """The planner accepts the connection and yields exactly the sessions, ends and ranges documented."""
    (row,) = [r for r in _connections() if r["name"] == connection_name]

    links = _plan(row)

    prefix_text = {f"{prefix}|{ns}": prefix for prefix, ns in row.get("link_prefixes") or []}
    assert [(link.name, *_ends(link), prefix_text.get(link.link_prefix_id or "")) for link in links] == _EXPECTED[
        connection_name
    ]


@pytest.mark.parametrize("connection_name", sorted(_EXPECTED))
def test_the_planned_circuit_type_and_link_count_follow_the_path_type_and_flavour(connection_name: str) -> None:
    """handoff for a dedicated connection, vpn for an internet_vpn; the count is the flavour's, the ranges are the
    connection's own for a provider-assigned flavour."""
    (row,) = [r for r in _connections() if r["name"] == connection_name]

    flavour, _ = validate_spec(parse_connection(_payload(row)))
    links = _plan(row)

    assert {link.circuit_type for link in links} == {"handoff" if row["path_type"] == "dedicated" else "vpn"}
    assert len(links) == flavour.links
    if flavour.address_source == ADDRESS_SOURCE_PROVIDER:
        assert len(row["link_prefixes"]) == flavour.links
        assert all(link.link_prefix_id for link in links)
    else:
        assert not row.get("link_prefixes") and not any(link.link_prefix_id for link in links)


@pytest.mark.parametrize("connection_name", sorted(_EXPECTED))
def test_the_far_process_is_the_stubs_only_bgp_process(connection_name: str) -> None:
    """Every session ends on a device with exactly one process, the one that speaks the connection's provider_as; a
    stub with none authored is planned for the generator to build (`far_process_id` None, which needs `provider_as`)."""
    (row,) = [r for r in _connections() if r["name"] == connection_name]
    processes = _processes()

    for link in _plan(row):
        authored = processes.get(link.far.device_name, [])
        assert [p["id"] for p in authored] == ([link.far_process_id] if authored else [])
        if authored and row.get("provider_as"):
            assert authored[0]["local_as"] == row["provider_as"]
        if not authored:
            assert link.far_process_id is None and row["provider_as"]


def test_the_ports_the_planner_picks_on_authored_stubs_exist_on_their_template() -> None:
    """The far port of a dedicated link (et-0/0/0, et-0/0/1) is a port of the stub's object template."""
    rows = {name(d): d.item for d in declared("DcimPhysicalDevice", _ALL)}
    for connection in _connections():
        if connection["path_type"] != "dedicated":
            continue
        for link in _plan(connection):
            assert link.far.port is not None
            device = rows.get(link.far.device_name)
            if device is None:  # an appliance (a service edge has declared NICs, a Network Edge router is generated)
                declared_nics = {
                    str(i.item["name"])
                    for i in declared("DcimVirtualInterface", _ALL)
                    if i.item["device"] == link.far.device_name
                }
                assert not declared_nics or link.far.port.name in declared_nics, link.far.device_name
                continue
            template_ports = _template_ports(device["object_template"])
            assert link.far.port.name in template_ports, f"{link.far.device_name}: {link.far.port.name}"


def _template_ports(template: str) -> set[str]:
    """The expanded interface names of one object template."""
    names: set[str] = set()
    for row in object_templates()[template]["interfaces"]["data"]:
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", row["name"])
        if match:
            names |= {f"{match[1]}{n}{match[4]}" for n in range(int(match[2]), int(match[3]) + 1)}
        else:
            names.add(row["name"])
    return names


def test_no_two_links_of_the_demo_share_a_carrier_port_or_a_link_prefix() -> None:
    """One port, one handoff session: the planned dedicated links are on distinct (device, port) pairs per VLAN, and a
    link range is used by one session. (The AWS stub port is shared by two connections on two VLANs: 1000 and 1100.)"""
    seen_prefix: set[str] = set()
    ports: dict[tuple[str, int], str] = {}
    for connection in _connections():
        for link in _plan(connection):
            if link.link_prefix_id:
                assert link.link_prefix_id not in seen_prefix, link.name
                seen_prefix.add(link.link_prefix_id)
            if connection["path_type"] != "dedicated":
                continue
            for side in (link.near, link.far):
                assert side.port is not None
                key = (side.port.label, connection["vlan_id"])
                assert key not in ports, f"{link.name} and {ports[key]} share {key}"
                ports[key] = link.name
