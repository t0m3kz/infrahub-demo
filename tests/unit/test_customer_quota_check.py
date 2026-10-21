"""Unit tests for CheckCustomerQuota (segments and host-facing ports of a DC footprint against its design).

The payloads are raw GraphQL responses shaped like queries/validation/customer_quota.gql; ``validate`` runs them
through ``clean_data``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from checks.customer_quota import CheckCustomerQuota
from tests.integration.demo_data import declared, distinct, name

_ROOT = Path(__file__).parents[2]


def _v(value: Any) -> dict[str, Any]:
    """A GraphQL attribute."""
    return {"value": value}


def _check() -> Any:
    """CheckCustomerQuota with logging captured instead of sent to Infrahub."""
    check = cast(Any, CheckCustomerQuota.__new__(CheckCustomerQuota))
    findings: list[dict[str, Any]] = []
    check._captured = findings
    check.log_error = lambda **kwargs: findings.append(kwargs)
    return check


def _footprint(
    name: str = "C005-P-DC10",
    dc: str = "DC10",
    design: str | None = "M_DC",
    max_segments: int = 2,
    ports_25g: int = 4,
    ports_100g: int = 0,
) -> dict[str, Any]:
    """One TopologyCustomerDC row, with its design's numbers (design=None: no design)."""
    return {
        "node": {
            "id": f"fp-{name}",
            "name": _v(name),
            "parent": {"node": {"id": f"dc-{dc}", "name": _v(dc)}},
            "design": {
                "node": {
                    "id": f"design-{design}",
                    "name": _v(design),
                    "max_segments": _v(max_segments),
                    "ports_25g": _v(ports_25g),
                    "ports_100g": _v(ports_100g),
                }
            }
            if design
            else {"node": None},
        }
    }


def _device(dc: str | None) -> dict[str, Any]:
    """The tagged port's device: deployed into a pod of ``dc`` (None: no deployment selected)."""
    if dc is None:
        return {"node": {}}
    return {
        "node": {
            "deployment": {
                "node": {
                    "id": f"pod-{dc}",
                    "__typename": "TopologyPod",
                    "parent": {"node": {"id": f"dc-{dc}", "name": _v(dc)}},
                }
            }
        }
    }


def _port(
    port_id: str,
    interface_type: str = "25gbase-x-sfp28",
    dc: str | None = "DC10",
    role: str = "customer",
) -> dict[str, Any]:
    """A tagged physical switch port."""
    return {
        "node": {
            "id": port_id,
            "__typename": "DcimPhysicalInterface",
            "role": _v(role),
            "device": _device(dc),
            "interface_type": _v(interface_type),
        }
    }


def _lag(port_id: str, *members: tuple[str, str], dc: str | None = "DC10") -> dict[str, Any]:
    """A tagged port-channel with its (member id, interface_type) ports."""
    return {
        "node": {
            "id": port_id,
            "__typename": "DcimLAGInterface",
            "role": _v("lag"),
            "device": _device(dc),
            "member_interfaces": {"edges": [{"node": {"id": mid, "interface_type": _v(t)}} for mid, t in members]},
        }
    }


def _segment(
    seg_id: str, footprints: list[str], *ports: dict[str, Any], zone: str | None = None
) -> dict[str, dict[str, Any]]:
    """A segment (the raw row; ``footprints`` are the footprint names it names) with its tagged ports."""
    node = {
        "id": seg_id,
        "name": _v(seg_id),
        "security_zone": {"node": {"name": _v(zone)}} if zone else {"node": None},
        "capabilities": {"edges": list(ports)},
    }
    return {"node": {**node, "_footprints": footprints}}


def _vxlan(segment: dict[str, Any]) -> dict[str, Any]:
    """The segment as a ManagedVxlanSegment row naming its footprints."""
    node = {k: v for k, v in segment["node"].items() if k != "_footprints"}
    named = [{"node": {"id": f"fp-{name}", "name": _v(name)}} for name in segment["node"]["_footprints"]]
    return {"node": {**node, "customer_deployments": {"edges": named}}}


def _vlan(segment: dict[str, Any]) -> dict[str, Any]:
    """The segment as a ManagedVlanSegment row naming its (single) footprint."""
    node = {k: v for k, v in segment["node"].items() if k != "_footprints"}
    name = segment["node"]["_footprints"][0]
    return {"node": {**node, "customer_deployment": {"node": {"id": f"fp-{name}", "name": _v(name)}}}}


def _run(
    footprints: list[dict[str, Any]],
    vxlan: list[dict[str, Any]] | None = None,
    vlan: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate and return the logged findings."""
    check = _check()
    check.validate(
        {
            "TopologyCustomerDC": {"edges": footprints},
            "ManagedVxlanSegment": {"edges": [_vxlan(s) for s in vxlan or []]},
            "ManagedVlanSegment": {"edges": [_vlan(s) for s in vlan or []]},
        }
    )
    return check._captured


def _messages(findings: list[dict[str, Any]]) -> list[str]:
    return [finding["message"] for finding in findings]


class TestSegmentQuota:
    """Segments naming the footprint against max_segments."""

    def test_at_the_limit_passes(self) -> None:
        """Exactly max_segments segments is within the quota."""
        segments = [_segment("s1", ["C005-P-DC10"]), _segment("s2", ["C005-P-DC10"])]
        assert _run([_footprint(max_segments=2)], segments) == []

    def test_over_the_limit_is_an_error(self) -> None:
        """One segment more than max_segments is an error naming footprint, design and both numbers."""
        segments = [_segment(f"s{i}", ["C005-P-DC10"]) for i in range(3)]
        findings = _run([_footprint(max_segments=2)], segments)

        assert len(findings) == 1
        message = findings[0]["message"]
        assert "'C005-P-DC10'" in message and "3 segments" in message and "over the 2" in message
        assert "'M_DC'" in message and "max_segments" in message
        assert findings[0]["object_type"] == "TopologyCustomerDC"
        assert findings[0]["object_id"] == "fp-C005-P-DC10"

    def test_vlan_and_vxlan_segments_both_count(self) -> None:
        """A VLAN segment on the footprint is a segment of the quota too."""
        findings = _run(
            [_footprint(max_segments=1)], [_segment("s1", ["C005-P-DC10"])], [_segment("s2", ["C005-P-DC10"])]
        )
        assert len(findings) == 1 and "2 segments" in findings[0]["message"]

    def test_stretched_segment_counts_for_each_footprint(self) -> None:
        """A segment naming two footprints is one segment of each."""
        footprints = [
            _footprint("C005-P-DC10", "DC10", max_segments=1),
            _footprint("C005-P-DC12", "DC12", max_segments=1),
        ]
        assert _run(footprints, [_segment("s1", ["C005-P-DC10", "C005-P-DC12"])]) == []
        over = _run(footprints, [_segment("s1", ["C005-P-DC10", "C005-P-DC12"]), _segment("s2", ["C005-P-DC12"])])
        assert len(over) == 1 and "'C005-P-DC12'" in over[0]["message"]

    def test_segment_of_another_footprint_is_not_counted(self) -> None:
        """Only segments naming the footprint count."""
        segments = [_segment("s1", ["C008-P-DC10"]), _segment("s2", ["C008-P-DC10"]), _segment("s3", ["C008-P-DC10"])]
        assert (
            _run([_footprint("C005-P-DC10", max_segments=1), _footprint("C008-P-DC10", max_segments=3)], segments) == []
        )

    def test_exchange_zone_segment_is_not_counted(self) -> None:
        """The operator's shared DC-local service segment is no part of the customer's quota."""
        segments = [_segment("s1", ["C005-P-DC10"]), _segment("infra", ["C005-P-DC10"], zone="MANAGEMENT-ZONE")]
        assert _run([_footprint(max_segments=1)], segments) == []

    def test_tenant_zone_segment_is_counted(self) -> None:
        """Only an exchange zone exempts: a PROD-ZONE segment counts."""
        segments = [_segment("s1", ["C005-P-DC10"]), _segment("s2", ["C005-P-DC10"], zone="PROD-ZONE")]
        assert len(_run([_footprint(max_segments=1)], segments)) == 1


class TestPortQuota:
    """Host-facing ports tagged with the footprint's segments, by speed, against ports_<speed>g."""

    def test_at_the_limit_passes(self) -> None:
        """Exactly ports_25g ports is within the quota."""
        ports = [_port(f"p{i}") for i in range(4)]
        assert _run([_footprint(ports_25g=4)], [_segment("s1", ["C005-P-DC10"], *ports)]) == []

    def test_over_the_limit_is_an_error(self) -> None:
        """A fifth 25G port against ports_25g=4 is an error naming speed and numbers."""
        ports = [_port(f"p{i}") for i in range(5)]
        findings = _run([_footprint(ports_25g=4)], [_segment("s1", ["C005-P-DC10"], *ports)])

        assert len(findings) == 1
        message = findings[0]["message"]
        assert "5 host-facing 25G ports" in message and "over the 4" in message and "ports_25g" in message

    def test_speeds_are_metered_separately(self) -> None:
        """25G and 100G have their own numbers: 100G over its limit does not use the 25G budget."""
        ports = [_port("a"), _port("b", "100gbase-x-qsfp28"), _port("c", "100gbase-x-qsfp28")]
        findings = _run([_footprint(ports_25g=4, ports_100g=1)], [_segment("s1", ["C005-P-DC10"], *ports)])

        assert len(findings) == 1
        assert "2 host-facing 100G ports" in findings[0]["message"] and "ports_100g" in findings[0]["message"]

    def test_zero_means_none_allowed(self) -> None:
        """ports_100g: 0 refuses the first 100G port."""
        findings = _run([_footprint(ports_100g=0)], [_segment("s1", ["C005-P-DC10"], _port("a", "100gbase-x-qsfp28"))])
        assert len(findings) == 1
        assert "1 host-facing 100G ports" in findings[0]["message"] and "over the 0" in findings[0]["message"]

    def test_port_tagged_by_two_segments_counts_once(self) -> None:
        """A host port carrying two segments of the footprint is one port."""
        ports = [_port("a"), _port("b")]
        segments = [_segment("s1", ["C005-P-DC10"], *ports), _segment("s2", ["C005-P-DC10"], *ports)]
        assert _run([_footprint(max_segments=2, ports_25g=2)], segments) == []

    def test_port_shared_by_two_footprints_counts_for_each(self) -> None:
        """A host serving two customers uses a port of each one's entitlement."""
        footprints = [_footprint("C005-P-DC10", ports_25g=1), _footprint("C008-P-DC10", ports_25g=1)]
        segments = [_segment("s1", ["C005-P-DC10"], _port("a")), _segment("s2", ["C008-P-DC10"], _port("a"))]
        assert _run(footprints, segments) == []

        tight = [_footprint("C005-P-DC10", ports_25g=0), _footprint("C008-P-DC10", ports_25g=1)]
        findings = _run(tight, segments)
        assert len(findings) == 1 and "'C005-P-DC10'" in findings[0]["message"]

    def test_port_channel_counts_its_members(self) -> None:
        """A port-channel is its member ports, at the member's speed."""
        segments = [
            _segment(
                "s1", ["C005-P-DC10"], _lag("pc1", ("m1", "25gbase-x-sfp28")), _lag("pc2", ("m2", "25gbase-x-sfp28"))
            )
        ]
        assert _run([_footprint(ports_25g=2)], segments) == []
        assert len(_run([_footprint(ports_25g=1)], segments)) == 1

    def test_port_channel_with_two_members_counts_both(self) -> None:
        """Two bonded NICs on one port-channel are two ports."""
        segments = [_segment("s1", ["C005-P-DC10"], _lag("pc1", ("m1", "25gbase-x-sfp28"), ("m2", "25gbase-x-sfp28")))]
        assert len(_run([_footprint(ports_25g=1)], segments)) == 1

    def test_service_ports_are_not_host_facing(self) -> None:
        """The border-leaf ports facing a firewall or load balancer (inline termination) are not counted."""
        ports = [_port("fw", role="firewall"), _port("lb", role="load-balancer"), _port("host")]
        assert _run([_footprint(ports_25g=1)], [_segment("s1", ["C005-P-DC10"], *ports)]) == []

    def test_other_interface_kinds_are_not_ports(self) -> None:
        """An inline sub-interface (DcimVirtualInterface) tagged with the segment is no switch port."""
        virtual = {
            "node": {"id": "v", "__typename": "DcimVirtualInterface", "role": _v("firewall"), "device": _device("DC10")}
        }
        assert _run([_footprint(ports_25g=0)], [_segment("s1", ["C005-P-DC10"], virtual)]) == []

    def test_unknown_speed_is_not_metered(self) -> None:
        """No ports_10g attribute, a sub-1G port and a speed that cannot be read all count against nothing."""
        ports = [
            _port("a", "10gbase-x-sfpp"),
            _port("b", "400gbase-x-qsfpdd"),
            _port("c", "virtual"),
            _port("d", "100base-tx"),
        ]
        assert _run([_footprint(ports_25g=0, ports_100g=0)], [_segment("s1", ["C005-P-DC10"], *ports)]) == []

    def test_stretched_segment_ports_count_in_their_own_dc(self) -> None:
        """A segment stretched over DC10 and DC12 is tagged in both: each footprint meters its own DC's ports."""
        footprints = [_footprint("C005-P-DC10", "DC10", ports_25g=2), _footprint("C005-P-DC12", "DC12", ports_25g=1)]
        ports = [_port("a", dc="DC10"), _port("b", dc="DC10"), _port("c", dc="DC12")]
        segments = [_segment("s1", ["C005-P-DC10", "C005-P-DC12"], *ports)]
        assert _run(footprints, segments) == []

        findings = _run([_footprint("C005-P-DC10", "DC10", ports_25g=1), footprints[1]], segments)
        assert (
            len(findings) == 1
            and "'C005-P-DC10'" in findings[0]["message"]
            and "2 host-facing" in findings[0]["message"]
        )

    def test_port_without_a_deployment_counts_for_the_segments_footprints(self) -> None:
        """A device not in a deployment cannot be placed in a DC: counted, never silently unmetered."""
        segments = [_segment("s1", ["C005-P-DC10"], _port("a", dc=None), _port("b", dc=None))]
        assert len(_run([_footprint(ports_25g=1)], segments)) == 1

    def test_vlan_segment_ports_count(self) -> None:
        """A VLAN segment's tagged ports are the footprint's too."""
        findings = _run([_footprint(ports_25g=1)], None, [_segment("s1", ["C005-P-DC10"], _port("a"), _port("b"))])
        assert len(findings) == 1

    def test_exchange_zone_segment_ports_are_not_counted(self) -> None:
        """The operator's infra servers' ports are not the customer's."""
        segments = [_segment("infra", ["C005-P-DC10"], _port("a"), _port("b"), zone="MANAGEMENT-ZONE")]
        assert _run([_footprint(ports_25g=0)], segments) == []

    def test_segment_and_port_errors_are_both_reported(self) -> None:
        """Each exceeded number is its own error."""
        segments = [_segment("s1", ["C005-P-DC10"], _port("a")), _segment("s2", ["C005-P-DC10"])]
        findings = _run([_footprint(max_segments=1, ports_25g=0)], segments)
        assert len(findings) == 2


class TestNotMetered:
    """What is out of scope produces nothing."""

    def test_footprint_without_design_is_skipped(self) -> None:
        """No design, no numbers: any usage passes."""
        ports = [_port(f"p{i}") for i in range(20)]
        segments = [_segment(f"s{i}", ["C005-P-DC10"], *ports) for i in range(20)]
        assert _run([_footprint(design=None)], segments) == []

    def test_segment_naming_an_unmetered_footprint_is_ignored(self) -> None:
        """A colocation/cloud/office footprint is not in the DC list: its segments touch no DC's count."""
        segments = [_segment("s1", ["C011-P-NY"], _port("a"))]
        assert _run([_footprint(max_segments=1, ports_25g=0)], segments) == []

    def test_empty_response_is_silent(self) -> None:
        """No footprints and no segments is nothing to meter."""
        check = _check()
        check.validate({})
        assert check._captured == []


def test_check_and_query_are_registered() -> None:
    """The check is registered globally with its query, and both smoke files list it."""
    config = yaml.safe_load((_ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert checks["validate_customer_quota"]["class_name"] == "CheckCustomerQuota"
    assert "targets" not in checks["validate_customer_quota"]
    assert queries[CheckCustomerQuota.query] == "queries/validation/customer_quota.gql"
    assert (_ROOT / queries[CheckCustomerQuota.query]).is_file()
    assert "resource_name: validate_customer_quota" in (_ROOT / "tests/smoke/test_checks.yml").read_text()
    assert "resource_name: customer_quota" in (_ROOT / "tests/smoke/test_graphql.yml").read_text()


@pytest.mark.parametrize("field", ["max_segments", "ports_25g", "ports_100g"])
def test_query_selects_each_number_the_design_carries(field: str) -> None:
    """The query reads every quota attribute of the design."""
    query = (_ROOT / "queries" / "validation" / "customer_quota.gql").read_text()
    assert f"{field} {{ value }}" in query


# ---------------------------------------------------------------------------
# 30_all: the bootstrap template rows hold every demo footprint
# ---------------------------------------------------------------------------

_ALL_DEMO = _ROOT / "data" / "demos" / "30_all"
_BOOTSTRAP = _ROOT / "data" / "bootstrap"


def _server_nic_types() -> list[str]:
    """interface_type of each uplink NIC of the demo server template (add_endpoint cables one switch port per NIC)."""
    template = next(
        t
        for doc in yaml.safe_load_all((_BOOTSTRAP / "10_physical_devices_templates_dell_server.yaml").read_text())
        for t in doc["spec"]["data"]
        if t["template_name"] == "PowerEdge-R640_SERVER"
    )
    return [i["interface_type"] for i in template["interfaces"]["data"] if i["role"] == "uplink"]


def _all_demo_payload() -> dict[str, Any]:
    """The 30_all footprints, segments and tagged ports in the query shape, from the object files alone.

    A segment is tagged on the ports of the physical hosts of its AppComponents' instances (a VM through its
    hosting_device): one port per NIC, in the DC the host's directory (03_dc/<dc>/) names.
    """
    templates = {t.item["name"]: t.item for t in declared("TopologyCustomerTemplateDC", _BOOTSTRAP)}
    footprint_rows = [
        _footprint(
            name(fp),
            str(fp.item["parent"]).upper(),
            fp.item.get("design"),
            **{k: templates[fp.item["design"]][k] for k in ("max_segments", "ports_25g", "ports_100g")},
        )
        for fp in distinct(declared("TopologyCustomerDC", _ALL_DEMO))
        if fp.item.get("design")
    ]
    host_dc = {
        h.item["name"]: h.path.parent.name.upper()
        for h in declared("DcimPhysicalDevice", _ALL_DEMO)
        if "03_dc" in h.path.parts
    }
    vm_host = {vm.item["name"]: vm.item["hosting_device"] for vm in declared("DcimVirtualDevice", _ALL_DEMO)}
    hosts_by_segment: dict[str, set[str]] = {}
    for component in declared("AppComponent", _ALL_DEMO):
        for instance in component.item.get("instances") or []:
            host = vm_host.get(instance, instance)
            if host in host_dc:
                hosts_by_segment.setdefault(component.item["network_segment"], set()).add(host)

    vxlan, vlan = [], []
    for kind, rows in (("ManagedVxlanSegment", vxlan), ("ManagedVlanSegment", vlan)):
        for segment in distinct(declared(kind, _ALL_DEMO)):
            seg_name = name(segment)
            deployments = segment.item.get("customer_deployments") or [segment.item.get("customer_deployment")]
            ports = [
                _port(f"{host}-{nic}", nic_type, host_dc[host])
                for host in sorted(hosts_by_segment.get(seg_name, ()))
                for nic, nic_type in enumerate(_server_nic_types())
            ]
            zone = segment.item.get("security_zone")
            row = _segment(seg_name, [str(d) for d in deployments if d], *ports, zone=zone)
            rows.append(row)
    return {
        "TopologyCustomerDC": {"edges": footprint_rows},
        "ManagedVxlanSegment": {"edges": [_vxlan(s) for s in vxlan]},
        "ManagedVlanSegment": {"edges": [_vlan(s) for s in vlan if s["node"]["_footprints"]]},
    }


def test_all_demo_footprints_are_metered() -> None:
    """Every 30_all DC footprint has a design, and the scan finds segments and 25G ports on them."""
    payload = _all_demo_payload()
    assert len(payload["TopologyCustomerDC"]["edges"]) == 10
    assert len(payload["ManagedVxlanSegment"]["edges"]) >= 10
    ports = [p for s in payload["ManagedVxlanSegment"]["edges"] for p in s["node"]["capabilities"]["edges"]]
    assert ports and all(p["node"]["interface_type"]["value"].startswith("25gbase") for p in ports)


def test_all_demo_usage_passes_the_check() -> None:
    """Each 30_all footprint is within the numbers of its own design row in data/bootstrap/21_customer_templates.yml."""
    check = _check()
    check.validate(_all_demo_payload())
    assert check._captured == []
