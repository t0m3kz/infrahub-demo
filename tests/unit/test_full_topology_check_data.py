"""data/demos/00_full_topology is authored, no generator runs over it, so the checks of a Proposed Change find it as it is.

The sample is loaded onto main and the next branch's Proposed Change runs every check on it. Each test here states one
rule of such a check against the YAML (no server): the panels hold every port of their type, every routed segment and
its owner has a routing tag, a footprint stays within its design, a provider interface rides on its own provider
connect, and an owner's cloud prefix is registered behind one attachment point.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from generators.helpers.patch_panel import audit
from tests.integration.demo_data import Declared, declared, object_templates
from transforms.helpers.bgp_cloud import attachment_point
from utils.exchange_transit import is_exchange_zone
from utils.interface_speed import InterfaceSpeedMatcher
from utils.limits import ROUTING_TAG_MAX, ROUTING_TAG_MIN

_ROOT = Path(__file__).parents[2]
_FULL = _ROOT / "data" / "demos" / "00_full_topology"
_BOOTSTRAP = _ROOT / "data" / "bootstrap"


# ---------------------------------------------------------------------------
# validate_patch_panel_pairing
# ---------------------------------------------------------------------------


def _panel_key(obj: Declared) -> tuple[str, str]:
    """(rack, panel name) of a port: its own ``panel`` reference, else the panel it is nested under."""
    panel = obj.item.get("panel")
    if panel:
        return panel[0], panel[1]
    assert obj.enclosing is not None
    return obj.enclosing.item["rack"], obj.enclosing.item["name"]


def _port_id(rack: str, panel: str, name: str, rear: bool) -> str:
    """The computed id of a port, which is its HFID: ``<rack>:<panel>:<rear>`` or ``<rack>:<panel>:F<front>``."""
    return f"{rack}:{panel}:{name}" if rear else f"{rack}:{panel}:F{name}"


def test_every_panel_of_the_sample_holds_every_port_of_its_type() -> None:
    """validate_patch_panel_pairing: add_patch_panel does not run here, so the ports are data, front on its rear."""
    panels = declared("DcimPatchPanel", _FULL)
    rears: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    fronts: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for obj in declared("DcimRearPatchPanelPort", _FULL):
        rack, panel = _panel_key(obj)
        port = obj.item["name"]
        rears[(rack, panel)][port] = {
            "id": _port_id(rack, panel, port, rear=True),
            "name": port,
            "connector": obj.item["connector"],
        }
    for obj in declared("DcimFrontPatchPanelPort", _FULL):
        rack, panel = _panel_key(obj)
        port = obj.item["name"]
        target = obj.item.get("rear_port")
        if target is not None:
            rear_id = target if isinstance(target, str) else _port_id(rack, panel, target["data"]["name"], rear=True)
        else:  # a cable end names the port only: the declaration that pairs it is another one
            rear_id = fronts[(rack, panel)].get(port, {}).get("rear_port", {}).get("id")
        fronts[(rack, panel)][port] = {
            "id": _port_id(rack, panel, port, rear=False),
            "name": port,
            "connector": obj.item["connector"],
            "rear_port": {"id": rear_id},
        }

    assert panels
    problems = []
    for obj in panels:
        key = (obj.item["rack"], obj.item["name"])
        result = audit(obj.item["panel_type"], list(rears[key].values()), list(fronts[key].values()))
        found = (result.problem, result.missing_rears, result.missing_fronts, result.misplaced, result.extra)
        if result.problem or any(found[1:]) or result.to_pair or result.wrong_connector:
            problems.append(f"{key}: {found}")
    assert problems == []


# ---------------------------------------------------------------------------
# validate_routing_tags
# ---------------------------------------------------------------------------


def test_every_routed_segment_and_its_owner_has_a_unique_routing_tag() -> None:
    """validate_routing_tags: no generator draws the tags here, so they are authored (1..65534, unique)."""
    segments = declared("ManagedVxlanSegment", _FULL)
    routed = [
        segment
        for segment in segments
        if segment.item.get("gateway")
        and not segment.item.get("terminated_on")
        and not is_exchange_zone(segment.item.get("security_zone"))
    ]
    customers = {customer.item["org_id"]: customer for customer in declared("OrganizationCustomer", _FULL)}
    assert routed

    untagged = [f"{s.item['owner']}-{s.item['customer_name']}" for s in routed if s.item.get("routing_tag") is None]
    untagged_owners = sorted(
        {s.item["owner"] for s in routed if customers[s.item["owner"]].item.get("routing_tag") is None}
    )
    assert untagged == [] and untagged_owners == []

    for tags in (
        [s.item["routing_tag"] for s in segments if s.item.get("routing_tag") is not None],
        [c.item["routing_tag"] for c in customers.values() if c.item.get("routing_tag") is not None],
    ):
        assert len(tags) == len(set(tags))
        assert all(ROUTING_TAG_MIN <= tag <= ROUTING_TAG_MAX for tag in tags)


# ---------------------------------------------------------------------------
# validate_customer_quota
# ---------------------------------------------------------------------------


def _expand(pattern: str) -> list[str]:
    """``Ethernet1/[1-24]`` -> its names (the template's expand_range)."""
    match = re.search(r"\[(\d+)-(\d+)\]", pattern)
    if not match:
        return [pattern]
    low, high = int(match.group(1)), int(match.group(2))
    return [pattern[: match.start()] + str(n) + pattern[match.end() :] for n in range(low, high + 1)]


def _port_speeds() -> dict[tuple[str, str], int]:
    """(device, port) -> Gbps for the ports a device template gives its devices (the leaf customer ports are 100G)."""
    speeds: dict[tuple[str, str], int] = {}
    for device in declared("DcimPhysicalDevice", _FULL):
        template = object_templates().get(str(device.item.get("object_template")))
        for entry in ((template or {}).get("interfaces") or {}).get("data") or []:
            speed = InterfaceSpeedMatcher.extract_speed(entry.get("interface_type")) or 0
            for port in _expand(entry["name"]) if speed else []:
                speeds[(device.item["name"], port)] = speed // 1000
    return speeds


def _members() -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(device, port-channel) -> the (device, port) members, from the LAGs the sample declares."""
    return {
        (lag.item.get("device") or (lag.enclosing.item["name"] if lag.enclosing else ""), lag.item["name"]): [
            tuple(member) for member in lag.item["member_interfaces"] if isinstance(member, list)
        ]
        for lag in declared("DcimLAGInterface", _FULL)
        if lag.item.get("member_interfaces")
    }


def _tagged_ports(segment: str, deployment: str) -> set[tuple[str, str]]:
    """The host-facing physical ports tagged with ``segment`` in ``deployment``: its rows and its requests.

    A port-channel counts its members, the border-leaf service ports (``-bl-``) facing the firewall pair are no host
    port (customer_quota._host_facing_ports).
    """
    members = _members()
    ports: set[tuple[str, str]] = set()
    for kind, holder in (("ManagedSegmentVlan", "segment_deployment"), ("ManagedSegmentDeployment", None)):
        for obj in declared(kind, _FULL):
            pair = obj.item.get(holder) if holder else [obj.item.get("segment"), obj.item.get("deployment")]
            if list(pair or []) != [segment, deployment]:
                continue
            for capability in obj.item.get("capabilities") or []:
                data = capability["data"]
                key = (data["device"], data["name"])
                ports.update(members.get(key, [key]))
    return {port for port in ports if "-bl-" not in port[0]}


def test_every_dc_footprint_stays_within_its_design() -> None:
    """validate_customer_quota: segments and host-facing 25G/100G ports against the design (zero means none)."""
    designs = {
        design.item["name"]: design.item
        for design in declared("TopologyCustomerTemplateDC", _BOOTSTRAP, _FULL / "00_common")
    }
    speeds = _port_speeds()
    footprints = {
        f"{fp.item['owner']}-{fp.item['environment'].upper()}-{fp.item['parent']}": fp
        for fp in declared("TopologyCustomerDC", _FULL)
    }
    segments = [s for s in declared("ManagedVxlanSegment", _FULL) if not is_exchange_zone(s.item.get("security_zone"))]
    assert footprints

    over = []
    for name, footprint in footprints.items():
        design = designs[footprint.item["design"]]
        named = [s.item["customer_name"] for s in segments if name in (s.item.get("customer_deployments") or [])]
        ports = {
            port
            for segment in segments
            if name in (segment.item.get("customer_deployments") or [])
            for port in _tagged_ports(
                f"{segment.item['owner'].lower()}-{segment.item['customer_name']}-p", footprint.item["parent"]
            )
        }
        used: dict[int, int] = defaultdict(int)
        for port in ports:
            used[speeds[port]] += 1
        if len(named) > design["max_segments"]:
            over.append(f"{name}: {len(named)} segments over {design['max_segments']}")
        for speed, count in used.items():
            if count > design.get(f"ports_{speed}g", count):
                over.append(f"{name}: {count} x {speed}G over {design[f'ports_{speed}g']} of '{design['name']}'")
    assert over == []


def test_the_c900_footprints_that_tag_100g_ports_name_a_tier_that_allows_them() -> None:
    """GDN1 tags three 100G leaf ports, KTW1 five (the bootstrap S and M allow none): the sample's own tiers."""
    designs = {
        design.item["name"]: design.item for design in declared("TopologyCustomerTemplateDC", _FULL / "00_common")
    }
    by_footprint = {fp.item["parent"]: fp.item["design"] for fp in declared("TopologyCustomerDC", _FULL)}
    assert by_footprint["DC-GDN1"] == "S_100G" and by_footprint["DC-KTW1"] == "M_100G"
    assert designs["S_100G"]["ports_100g"] >= 3 and designs["M_100G"]["ports_100g"] >= 5
    for name in ("S_100G", "M_100G"):
        assert designs[name]["dedicated_firewall"] is False and designs[name]["dedicated_loadbalancer"] is False


# ---------------------------------------------------------------------------
# validate_interface_parents
# ---------------------------------------------------------------------------


def test_a_provider_interface_rides_on_a_port_of_its_own_provider_connect() -> None:
    """validate_interface_parents rule 1: the connection of a Fabric port rides on that port, same device."""
    interfaces = declared("TopologyProviderInterface", _FULL)
    assert interfaces
    assert [i.item["name"] for i in interfaces if i.item["parent_interface"][0] != i.item["device"]] == []


# ---------------------------------------------------------------------------
# validate_routing_invariants rule 8 (cloud legs)
# ---------------------------------------------------------------------------


def _cloud_attachment_points() -> dict[str, set[str]]:
    """Prefix -> the attachment points (hub chains) of the BGP legs that reach it, without the route-table filter."""
    items = [a.item for a in declared("CloudGatewayAttachment", _FULL)]
    attachments = [{"gateway": _ref(i["gateway"]), "peer_gateway": _ref(i.get("peer_gateway"))} for i in items]
    networks = {n.item["name"]: n.item for n in declared("CloudVirtualNetwork", _FULL)}
    landing = {
        gateway
        for peering in declared("CloudPeeringInterface", _FULL)
        if peering.item.get("routing") == "bgp" and peering.item.get("scope") != "public"
        for gateway in [peering.item.get("gateway") or _vpn_gateway(peering)]
        if gateway
    }
    reached: dict[str, set[str]] = defaultdict(set)
    for gateway in sorted(landing):
        point = attachment_point(_ref(gateway), attachments) or gateway
        chain = set(point.split("+"))
        for attachment in (item for item in items if item["gateway"] in chain):
            allowed = [entry[0] for entry in attachment.get("allowed_prefixes") or []]
            if network := attachment.get("virtual_network"):
                allowed = allowed or _block_prefixes(networks[network])
            for prefix in allowed:
                reached[prefix].add(point)
    return reached


def _ref(gateway: str | None) -> dict[str, str] | None:
    """A cleaned gateway reference."""
    return {"id": gateway, "name": gateway} if gateway else None


def _block_prefixes(network: dict[str, Any]) -> list[str]:
    """The prefixes of a virtual network's ``cidr_blocks``, referenced ([prefix, namespace]) or nested."""
    blocks = network["cidr_blocks"]
    entries = blocks["data"] if isinstance(blocks, dict) else blocks
    return [entry["prefix"] if isinstance(entry, dict) else entry[0] for entry in entries]


def _vpn_gateway(peering: Declared) -> str | None:
    """The gateway of the CloudVPNConnection that holds a tunnel (nested under it in the sample)."""
    transport = peering.enclosing
    return transport.item.get("gateway") if transport is not None else None


def test_a_registered_cloud_prefix_is_behind_one_attachment_point() -> None:
    """validate_routing_invariants rule 8: the transit VIF and the VPN reach the hub chain, the VGW has a VPC of its own."""
    reached = _cloud_attachment_points()
    assert reached
    for prefix in reached:
        ipaddress.ip_network(prefix)
    assert {prefix: sorted(points) for prefix, points in reached.items() if len(points) > 1} == {}
