"""Seeded firewall-terminated DC segments exercise the same derivations as live configuration queries."""

from __future__ import annotations

import json
from copy import deepcopy
from ipaddress import ip_interface, ip_network
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

from checks.segment_placement import CheckSegmentPlacement
from tests.integration.demo_data import BOOTSTRAP, declared, human_friendly_id, identity, name, object_templates
from tests.smoke.fixture_builders.vrf import routing_vrf
from transforms.helpers.firewall_contexts import get_firewall_contexts
from transforms.helpers.firewall_terminated import terminated_interfaces
from transforms.helpers.ha import get_ha
from transforms.helpers.segments import _get_segment_gateways, routed_activations
from transforms.helpers.vrf import vrf_requirements
from transforms.helpers.vxlan import get_vxlan_config
from utils.data_cleaning import clean_data
from utils.exchange_transit import is_routed_tenant_segment
from utils.segment_addressing import routed_segment_namespace

_DATA = Path(__file__).parents[2] / "data/demos/00_full_topology"
_SITES = {"ktw1": "DC-KTW1", "waw1": "DC-WAW1", "gdn1": "DC-GDN1"}
_EXPECTED = {
    "ktw1": ("10.90.2.0/24", 150, 10150),
    "waw1": ("10.92.2.0/24", 250, 10250),
    "gdn1": ("10.93.2.0/24", 350, 10350),
}
_WAW_CONTEXT = "c900-waw1-fw-01-waw1-fw-02-prod"


def _site(site: str) -> Path:
    """The customer-owned part of the existing DC, not a renamed facility."""
    return _DATA / "20_C900" / _SITES[site]


def _items(kind: str, folder: Path) -> list[dict[str, Any]]:
    """Top-level declarations in load order, including later keyed upserts."""
    relative = folder.relative_to(_DATA)
    scopes = [folder]
    if relative.parts and relative.parts[0] == "20_C900":
        scopes += [
            _DATA / "10_topology" / Path(*relative.parts[1:]),
            _DATA / "90_bindings/10_dc" / Path(*relative.parts[1:]),
        ]
    return [
        item
        for path in sorted(_DATA.rglob("*.yml"))
        if any(path.is_relative_to(scope) for scope in scopes)
        for doc in yaml.safe_load_all(path.read_text())
        if doc and doc["spec"]["kind"] == kind
        for item in doc["spec"]["data"]
    ]


def _segment(site: str) -> dict[str, Any]:
    """The new segment as authored, without synthesizing its computed name."""
    return _items("ManagedVxlanSegment", _site(site) / "05_firewall_tenant")[0]


def _segment_name(site: str) -> str:
    """Use the actual schema's computed-name implementation shared by other inventory tests."""
    return name(next(iter(declared("ManagedVxlanSegment", _site(site) / "05_firewall_tenant"))))


def _one(node: dict[str, Any]) -> dict[str, Any]:
    """A GraphQL cardinality-one relationship."""
    return {"node": node}


def _many(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """A GraphQL cardinality-many relationship."""
    return {"edges": [_one(node) for node in nodes]}


def _raw_segment(site: str) -> dict[str, Any]:
    """Project actual segment, footprint, context and row declarations into the registered query's raw shape."""
    segment = _segment(site)
    context_name = segment["terminated_on"]
    footprints = [
        item
        for item in _items("TopologyCustomerDC", _site(site))
        if item.get("serving_firewall_context") == context_name
    ]
    assert footprints
    footprint = footprints[-1]
    footprint_name = segment["customer_deployments"][0]
    assert footprint["parent"] == f"DC-{site.upper()}"
    gateway = segment["gateway"]["data"]
    prefix = gateway["ip_prefix"]["data"]
    zone = next(obj for obj in declared("SecurityZone", BOOTSTRAP) if name(obj) == segment["security_zone"])
    realization = _items("ManagedSegmentDeployment", _site(site) / "05_firewall_tenant")[0]
    assert realization["segment"] == _segment_name(site)
    footprint_node = {
        "id": footprint_name,
        "__typename": "TopologyCustomerDC",
        "name": {"value": footprint_name},
        "environment": {"value": footprint["environment"]},
    }
    return {
        "id": _segment_name(site),
        "__typename": "ManagedVxlanSegment",
        "name": {"value": _segment_name(site)},
        "environment": {"value": segment["environment"]},
        "security_zone": _one(
            {
                "name": {"value": segment["security_zone"]},
                "vrf_type": {"value": zone.value("vrf_type")},
                "l3_vni": {"value": zone.value("l3_vni")},
            }
        ),
        "customer_deployments": _many([footprint_node]),
        "terminated_on": _one(
            {
                "id": context_name,
                "name": {"value": context_name},
                "served_deployments": _many([footprint_node]),
            }
        ),
        "gateway": _one(
            {
                "address": {"value": gateway["address"]},
                "ip_prefix": _one(
                    {
                        "prefix": {"value": prefix["prefix"]},
                        "ip_namespace": {"node": {"name": {"value": "default"}}},
                    }
                ),
            }
        ),
        "segment_deployments": _many(
            [
                {
                    "vni": {"value": realization["vni"]},
                    "vlans": _many(
                        [
                            {"vlan_id": {"value": row["vlan_id"]}, "device": _one({"id": row["device"]})}
                            for row in realization["vlans"]["data"]
                        ]
                    ),
                }
            ]
        ),
    }


def render_terminated_from_query(raw_device: dict[str, Any]) -> list[dict[str, Any]]:
    """Reusable live proof: pass a raw ``firewall_config`` query's device node (not its root/edges wrapper).

    The registered query selects device capabilities/HA, interface cables, context legs and terminated-segment rows.
    No fixture facts are added: missing cables, rows or legs are handled by the production helper.
    """
    device = cast(dict[str, Any], clean_data(raw_device))
    interfaces = device.get("interfaces") or []
    ha = get_ha(device.get("capabilities"), interfaces, device.get("name"))
    contexts = get_firewall_contexts(interfaces, ha)
    return terminated_interfaces(contexts, interfaces, ha, device.get("id"), device["name"])


def _raw_member(site: str, member: str) -> dict[str, Any]:
    """Build raw query facts from that member's authored context leg, actual cable endpoints and HA membership."""
    segment = _raw_segment(site)
    context = next(
        obj for obj in _items("ManagedFirewallContext", _site(site)) if obj["name"] == _segment(site)["terminated_on"]
    )
    ha = next(obj for obj in _items("ManagedFirewallHA", _site(site)) if obj["name"] == context["cluster"])
    assert member in ha["capabilities"]
    leg_ref = next(
        cap["data"]
        for cap in context["capabilities"]
        if cap["kind"] == "DcimVirtualInterface" and cap["data"]["device"] == member
    )
    leg = next(
        obj
        for obj in _items("DcimVirtualInterface", _site(site))
        if obj["device"] == member and obj["name"] == leg_ref["name"]
    )
    trunk = leg["parent_interface"]
    physical_trunk = trunk
    if site == "waw1":
        device = next(obj for obj in _items("DcimVirtualDevice", _site(site)) if obj["name"] == member)
        nic = next(obj for obj in device["interfaces"]["data"] if obj["name"] == trunk[1])
        physical_trunk = nic["parent_interface"]
        assert physical_trunk == [device["hosting_device"], "ethernet1/1"]
    cable = next(
        obj
        for obj in _items("DcimCable", _site(site))
        if any(end["data"] == {"device": physical_trunk[0], "name": physical_trunk[1]} for end in obj["endpoints"])
    )
    assert {tuple(end["data"].values()) for end in cable["endpoints"]} == {
        tuple(physical_trunk),
        (physical_trunk[0].replace("-fw-", "-bl-"), "Ethernet1/25"),
    }
    address = leg["ip_address"]
    if isinstance(address, list):
        address = {"data": {"address": address[0], "ip_namespace": address[1]}}
    zone_name = leg["security_zone"]
    cap = {
        "id": context["name"],
        "__typename": "ManagedFirewallContext",
        "name": {"value": context["name"]},
        "vlan_id": {"value": context["vlan_id"]},
        "terminated_segments": _many([segment]),
        "served_deployments": segment["terminated_on"]["node"]["served_deployments"],
    }
    cabled = {
        "name": {"value": physical_trunk[1]},
        "device": _one({"id": physical_trunk[0]}),
        "cable": _one(
            {
                "endpoints": _many(
                    [
                        {
                            "name": {"value": end["data"]["name"]},
                            "device": _one({"id": end["data"]["device"]}),
                        }
                        for end in cable["endpoints"]
                    ]
                )
            }
        ),
    }
    uplink = {"name": {"value": trunk[1]}, "role": {"value": "uplink"}}
    if site == "waw1":
        uplink["parent_interface"] = _one(cabled)
    else:
        uplink["cable"] = cabled["cable"]
    return {
        "id": member,
        "name": {"value": member},
        "capabilities": _many(
            [
                {
                    "__typename": "ManagedFirewallHA",
                    "name": {"value": ha["name"]},
                    "group_id": {"value": ha["group_id"]},
                    "mode": {"value": ha.get("mode", "active-passive")},
                    "capabilities": _many([{"name": {"value": device}} for device in ha["capabilities"]]),
                }
            ]
        ),
        "interfaces": _many(
            [
                uplink,
                {
                    "vrf": _one(routing_vrf(zone_name)),
                    "name": {"value": leg["name"]},
                    "parent_interface": _one({"name": {"value": trunk[1]}}),
                    "ip_address": _one(
                        {
                            "address": {"value": address["data"]["address"]},
                            "ip_namespace": {"node": {"name": {"value": "default"}}},
                        }
                    ),
                    "capabilities": _many([cap]),
                },
            ]
        ),
    }


@pytest.mark.parametrize("site", _SITES)
def test_local_segment_and_placement_check_agree(site: str) -> None:
    """The approved prefix/VIP and one served PROD DC footprint pass the real termination validator."""
    authored = _segment(site)
    prefix, _, _ = _EXPECTED[site]
    segment = cast(dict[str, Any], clean_data(_raw_segment(site)))
    assert _segment_name(site) == f"c900-internal-fw-{site}-p"
    assert authored["owner"] == "C900" and authored["environment"] == "p"
    assert authored["stretch_scope"] == authored["vlan_scope"] == "local"
    assert authored["security_zone"] == "PROD"
    assert segment["gateway"]["ip_prefix"]["prefix"] == prefix
    assert ip_interface(segment["gateway"]["address"]).ip == ip_network(prefix)[1]
    assert authored["customer_deployments"] == [f"C900-P-DC-{site.upper()}"]
    assert authored["terminated_on"] == (_WAW_CONTEXT if site == "waw1" else f"shared-{site}-fw-ha-prod")
    check = cast(Any, CheckSegmentPlacement.__new__(CheckSegmentPlacement))
    check.log_error = MagicMock()
    check._validate_termination(segment)
    check.log_error.assert_not_called()
    invalid = deepcopy(segment)
    invalid["terminated_on"]["served_deployments"] = []
    check._validate_termination(invalid)
    assert "not served" in check.log_error.call_args.kwargs["message"]


@pytest.mark.parametrize("site", _SITES)
def test_both_cabled_firewall_members_render_the_vip_not_a_fabric_gateway(site: str) -> None:
    """The real query cleaning/context/HA/termination chain derives one identical VIP sub-interface per member."""
    prefix, vlan, _ = _EXPECTED[site]
    members = [f"{site}-fw-01", f"{site}-fw-02"]
    if site == "waw1":
        members = [f"c900-{member}-prod" for member in members]
    for member in members:
        raw = _raw_member(site, member)
        (interface,) = render_terminated_from_query(raw)
        assert interface["name"] == f"{'eth1/1' if site == 'waw1' else 'ethernet1/1'}.{vlan}"
        assert interface["virtual_ip"] == f"{ip_network(prefix)[1]}/24"
        assert interface["ip_address"]["address"] == interface["virtual_ip"]
        assert interface["security_zone"] == {"name": "PROD", "vrf_type": "prod", "l3_vni": 60000}
        no_cable = deepcopy(raw)
        uplink = no_cable["interfaces"]["edges"][0]["node"]
        if site == "waw1":
            assert "cable" not in uplink
            uplink["parent_interface"]["node"]["cable"] = None
        else:
            uplink["cable"] = None
        assert render_terminated_from_query(no_cable) == []


@pytest.mark.parametrize("site", _SITES)
def test_fabric_rows_are_l2_only_without_svi_gateway_l3_vni_or_new_vrf_demand(site: str) -> None:
    """All four rows retain their L2 VNI while the production fabric readers suppress every routed property."""
    _, vlan, vni = _EXPECTED[site]
    row = _items("ManagedSegmentDeployment", _site(site) / "05_firewall_tenant")[0]
    assert row["vni"] == vni and row["deployment"] == f"DC-{site.upper()}"
    assert {v["device"] for v in row["vlans"]["data"]} == {
        f"{site}-leaf-01",
        f"{site}-leaf-02",
        f"{site}-bl-01",
        f"{site}-bl-02",
    }
    assert all(v["vlan_id"] == vlan and "svi" not in v for v in row["vlans"]["data"])
    segment = cast(dict[str, Any], clean_data(_raw_segment(site)))
    assert _get_segment_gateways(segment) == (None, None, None, None)
    assert routed_segment_namespace(segment) == {} and not is_routed_tenant_segment(segment)
    activations = [{"vlan_id": vlan, "vni": vni, "segment": segment}]
    assert routed_activations(activations) == []
    for switch in row["vlans"]["data"]:
        device = {
            "name": switch["device"],
            "typename": "DcimPhysicalDevice",
            "role": "leaf",
            "interfaces": [],
            "capabilities": [],
            "segment_deployments": activations,
        }
        requirements = vrf_requirements(device)
        assert not requirements.namespaces and not requirements.svis
        config = get_vxlan_config(device, "cisco_nxos", device_role="leaf", activations=activations)
        assert config is not None
        assert [m["vni"] for m in config["l2_vni_mappings"]] == [vni]
        assert config["l3_vni_mappings"] == [] and config["anycast_gateway"]["enabled"] is False
    assert not any(item["name"] == f"Vlan{vlan}" for item in _items("DcimVirtualInterface", _site(site)))


@pytest.mark.parametrize("site", _SITES)
def test_vm_port_group_and_all_upstream_trunks_keep_the_physical_host_path(site: str) -> None:
    """The NIC belongs to the existing host/cluster/switch and its VLAN reaches compute and firewall border ports."""
    folder = _site(site) / "05_firewall_tenant"
    vm = _items("DcimVirtualDevice", folder)[0]
    switch = _items("ManagedVirtualSwitch", folder)[0]
    segment = _segment_name(site)
    prefix, vlan, _ = _EXPECTED[site]
    assert vm["name"] == f"{site}-vm-fw-01" and vm["object_template"] == "LINUX-SVC"
    assert vm["deployment"] == _segment(site)["customer_deployments"][0]
    cluster = next(obj for obj in _items("ManagedHypervisorCluster", _site(site)) if obj["name"] == switch["cluster"])
    assert vm["hosting_device"] in cluster["hosts"]
    (nic,) = vm["interfaces"]
    assert nic["kind"] == "DcimVirtualInterface"
    parent = nic["data"]["parent_interface"]
    assert parent == [vm["hosting_device"], "eno1"]
    assert nic["data"]["ip_address"]["data"]["address"] == f"{ip_network(prefix)[11]}/24"
    group = next(pg for pg in switch["port_groups"]["data"] if pg["segment"] == segment)
    assert group["name"] == f"c900-internal-fw-{site}"
    assert group["vlan_id"] == vlan and group["nics"] == [[vm["name"], "eth0"]]
    ports = {
        (item["device"], item["name"]): item
        for kind in ("DcimLAGInterface", "DcimPhysicalInterface")
        for item in _items(kind, folder)
    }
    for uplink in switch["uplinks"]:
        key = (uplink["data"]["device"], uplink["data"]["name"])
        assert segment in ports[key]["segments"]
    cable = next(
        item
        for item in _items("DcimCable", _site(site))
        if any(end["data"] == {"device": parent[0], "name": parent[1]} for end in item["endpoints"])
    )
    facing = next(end["data"] for end in cable["endpoints"] if end["data"]["device"] != parent[0])
    assert segment in ports[(facing["device"], facing["name"])]["segments"]
    for index in ("01", "02"):
        assert segment in ports[(f"{site}-leaf-{index}", "Port-Channel10")]["segments"]
        border = ports[(f"{site}-bl-{index}", "Ethernet1/25")]
        assert segment in border["segments"]
        assert not {"ip_address", "parent_interface"} & border.keys()


@pytest.mark.parametrize("site", _SITES)
def test_upserts_preserve_existing_switch_nics_uplinks_and_allowed_segments(site: str) -> None:
    """Adding a port group never changes switch type/MTU or overwrites existing group/NIC and trunk membership."""
    old_switch = _items("ManagedVirtualSwitch", _site(site) / "03_tenant")[0]
    folder = _site(site) / "05_firewall_tenant"
    new_switch = _items("ManagedVirtualSwitch", folder)[0]
    assert {k: v for k, v in old_switch.items() if k != "port_groups"} == {
        k: v for k, v in new_switch.items() if k != "port_groups"
    }
    for group in old_switch["port_groups"]["data"]:
        assert group in new_switch["port_groups"]["data"]
    for kind in ("DcimPhysicalInterface", "DcimLAGInterface"):
        earlier = {(obj["device"], obj["name"]): obj for obj in _items(kind, _site(site) / "03_tenant")}
        for obj in _items(kind, folder):
            previous = earlier.get((obj["device"], obj["name"]), {})
            assert set(previous.get("segments", [])) <= set(obj["segments"])
            for field in ("lag_id", "lacp_mode", "minimum_links", "member_interfaces"):
                if field in previous and field in obj:
                    assert previous[field] == obj[field]


def _port_group_load_rows() -> list[dict[str, Any]]:
    """Resolve nested and standalone groups to their actual composite HFID and mandatory parent in load order."""
    rows = []
    for obj in declared("ManagedPortGroup", _DATA):
        parent = obj.item.get("virtual_switch")
        if parent is None:
            assert obj.enclosing is not None and obj.enclosing.kind == "ManagedVirtualSwitch"
            parent = name(obj.enclosing)
        rows.append(
            {
                **deepcopy(obj.item),
                "virtual_switch": parent,
                "_hfid": identity(obj),
            }
        )
    return rows


def _upsert_port_groups(
    rows: list[dict[str, Any]], loaded: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Model the observed 1.11.11 inherited ``name.unique=True`` upsert, not just the composite HFID.

    Reloading a group updates its supplied fields, including its Parent and NICs; a later group with the same
    globally unique name therefore re-parents that object. Groups omitted from an upsert are not deleted.
    """
    loaded = deepcopy(loaded) if loaded is not None else {}
    for row in rows:
        loaded.setdefault(row["name"], {}).update(deepcopy(row))
    return loaded


def _assert_one_group_per_new_vm(loaded: dict[str, dict[str, Any]]) -> None:
    """Exactly one group owns each new NIC, on its intended existing vSwitch and site-local VLAN/segment."""
    for site in _SITES:
        switch = _items("ManagedVirtualSwitch", _site(site) / "05_firewall_tenant")[0]
        nic = [f"{site}-vm-fw-01", "eth0"]
        groups = [group for group in loaded.values() if nic in group.get("nics", [])]
        assert len(groups) == 1, f"{site}: expected exactly one port group for {nic}, got {len(groups)}"
        (group,) = groups
        assert group["name"] == f"c900-internal-fw-{site}"
        assert group["virtual_switch"] == switch["name"]
        assert group["segment"] == _segment_name(site) and group["vlan_id"] == _EXPECTED[site][1]
        assert group["nics"] == [nic]


def test_new_port_group_names_are_globally_unique_in_addition_to_composite_hfids() -> None:
    """A composite HFID does not exempt a group from the inherited global name uniqueness seen on the server."""
    assert human_friendly_id("ManagedPortGroup") == ("virtual_switch__name__value", "name__value")
    rows = _port_group_load_rows()
    expected_names = {f"c900-internal-fw-{site}" for site in _SITES}
    new_rows = [row for row in rows if row.get("segment") in {_segment_name(site) for site in _SITES}]
    assert len(new_rows) == 3
    assert {row["name"] for row in new_rows} == expected_names
    assert len({row["_hfid"] for row in new_rows}) == 3
    for group_name in expected_names:
        assert len([row for row in rows if row["name"] == group_name]) == 1
    assert not any(row["name"] == "c900-internal-fw" for row in rows)


def test_global_name_upserts_keep_each_vm_group_and_existing_groups_on_both_loads() -> None:
    """The entire sample's nested/standalone group upserts converge twice without losing old groups or NICs."""
    rows = _port_group_load_rows()
    baseline_rows = [row for row in rows if row.get("segment") not in {_segment_name(site) for site in _SITES}]
    baseline = _upsert_port_groups(baseline_rows)
    first = _upsert_port_groups(rows)
    second = _upsert_port_groups(rows, first)
    _assert_one_group_per_new_vm(first)
    _assert_one_group_per_new_vm(second)
    assert first == second
    for group_name, group in baseline.items():
        assert first[group_name] == second[group_name] == group


def test_legacy_shared_name_loses_vm_memberships_even_with_three_distinct_hfids() -> None:
    """Regression witness: the old three composite HFIDs still collapse to one globally named upserted group."""
    rows = _port_group_load_rows()
    new_rows = [row for row in rows if row.get("segment") in {_segment_name(site) for site in _SITES}]
    assert len({row["_hfid"] for row in new_rows}) == 3
    for row in new_rows:
        row["name"] = "c900-internal-fw"
        row["_hfid"] = (*row["_hfid"][:-1], json.dumps(row["name"]))
    assert len({row["_hfid"] for row in new_rows}) == 3
    loaded = _upsert_port_groups(new_rows)
    assert len(loaded) == 1 and loaded["c900-internal-fw"]["virtual_switch"] == "waw1-vmbr0"
    with pytest.raises(AssertionError, match="expected exactly one port group"):
        _assert_one_group_per_new_vm(loaded)


def test_new_names_prefixes_vnis_vlan_rows_and_load_order_have_no_collisions() -> None:
    """Realizations/rows have their actual schema keys, reference earlier parents, and share no occupied ID."""
    all_rows = _items("ManagedSegmentDeployment", _DATA)
    for site in _SITES:
        folder = _site(site) / "05_firewall_tenant"
        segment_name = _segment_name(site)
        prefix, vlan, vni = _EXPECTED[site]
        rows = [row for row in all_rows if row["segment"] == segment_name]
        assert len(rows) == 1
        assert not any(row["vni"] == vni and row["segment"] != segment_name for row in all_rows)
        for row in rows[0]["vlans"]["data"]:
            assert not any(
                other["device"] == row["device"] and other["vlan_id"] == vlan
                for other_row in all_rows
                if other_row["segment"] != segment_name
                for other in other_row["vlans"]["data"]
            )
        other_prefixes = [
            obj.value("prefix") for obj in declared("IpamPrefix", _DATA) if "05_firewall_tenant" not in obj.path.parts
        ]
        assert not any(
            ip_network(prefix).overlaps(ip_network(other)) for other in other_prefixes if ip_network(other).version == 4
        )
        assert human_friendly_id("ManagedSegmentDeployment") == (
            "segment__name__value",
            "deployment__name__value",
        )
        assert human_friendly_id("ManagedSegmentVlan") == ("device__name__value", "vlan_id__value")
        late_trunks = _DATA / "90_bindings/10_dc" / _SITES[site] / "05_firewall_tenant"
        paths = sorted([*folder.glob("*.yml"), *late_trunks.glob("*.yml")], key=lambda path: path.name)
        assert [p.name for p in paths] == [
            "00_segment.yml",
            "00a_realization.yml",
            "01_lag_trunks.yml",
            "01a_port_trunks.yml",
            "02_virtual_machine.yml",
            "03_virtual_switch.yml",
        ]
        for path in paths:
            docs = list(yaml.safe_load_all(path.read_text()))
            assert len(docs) == 1 and docs[0]
            assert "member_of_groups" not in path.read_text() and "segment_deployments:" not in path.read_text()
        context_path = _DATA / "10_topology" / _SITES[site] / "04_firewalls"
        load_order = sorted(_DATA.rglob("*.yml"))
        assert max(i for i, path in enumerate(load_order) if path.is_relative_to(context_path)) < min(
            i for i, path in enumerate(load_order) if path.is_relative_to(folder)
        )
        assert all(obj["segment"] == segment_name for obj in _items("ManagedSegmentDeployment", folder))


def test_new_gdn_pair_uses_free_template_ports_rack_units_and_shared_transit_prefix() -> None:
    """The minimum pair has two real uplinks/HA cables and derives the approved /29 transit VIP and anycast."""
    folder = _site("gdn1") / "04_firewalls"
    devices = _items("DcimPhysicalDevice", folder)
    assert {d["name"] for d in devices} == {"gdn1-fw-01", "gdn1-fw-02"}
    occupied = {
        obj["position"]
        for obj in _items("DcimPhysicalDevice", _site("gdn1") / "01_physical")
        if obj.get("rack") == "gdn1-rack-01"
    }
    height = next(obj.value("height") for obj in declared("DcimDeviceType", BOOTSTRAP) if name(obj) == "PA-5260")
    assert height == 2
    for device in devices:
        assert device["rack"] == "gdn1-rack-01" and device["position"] not in occupied
        assert device["deployment"] == "DC-GDN1" and device["object_template"] == "PA-5260_FIREWALL"
        units = set(range(device["position"], device["position"] + height))
        assert units.isdisjoint(occupied)
        occupied |= units
    template = object_templates()["PA-5260_FIREWALL"]
    assert template["role"] == "internal-firewall"
    assert {"ethernet1/1", "HA1", "HA2"} <= {port["name"] for port in template["interfaces"]["data"]}
    cables = _items("DcimCable", folder)
    all_cables = _items("DcimCable", _DATA)
    for cable in cables:
        for end in cable["endpoints"]:
            assert sum(candidate["data"] == end["data"] for item in all_cables for candidate in item["endpoints"]) == 1
    for port in ("HA1", "HA2"):
        cable = next(c for c in cables if c["name"] == f"CBL-GDN1-FW-{port}")
        assert {tuple(end["data"].values()) for end in cable["endpoints"]} == {
            ("gdn1-fw-01", port),
            ("gdn1-fw-02", port),
        }
    ha = _items("ManagedFirewallHA", folder)[0]
    assert ha["mode"] == "active-passive" and ha["group_id"] == 5
    domains = {obj["name"]: obj for obj in _items("ManagedFirewallHA", _DATA)}
    ids = [obj["group_id"] for obj in domains.values()]
    assert all(isinstance(value, int) and 1 <= value <= 63 for value in ids)
    assert len(ids) == len(set(ids))
    legs = _items("DcimVirtualInterface", folder)
    assert [leg["ip_address"]["data"]["address"] for leg in legs] == ["100.66.0.37/29", "100.66.0.38/29"]
    assert legs[0]["ip_address"]["data"]["ip_prefix"]["data"]["prefix"] == "100.66.0.32/29"
    assert legs[1]["ip_address"]["data"]["ip_prefix"] == ["100.66.0.32/29", "default"]
    assert {leg["security_zone"] for leg in legs} == {"PROD"}
    raw = _raw_member("gdn1", "gdn1-fw-01")
    device = cast(dict[str, Any], clean_data(raw))
    contexts = get_firewall_contexts(device["interfaces"], get_ha(device["capabilities"]))
    (context,) = contexts
    (leg,) = context["legs"]
    assert leg["virtual_ip"] == "100.66.0.36/29" and leg["anycast"] == "100.66.0.33"
