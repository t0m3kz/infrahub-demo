"""Unit tests for CheckInternetEgress (checks/internet_egress.py).

The pure functions run on cleaned query data (``clean_data`` output), built by the helpers below: a complete shared
context of a firewall pair (``_context``), a DC and a colocation footprint that name it, a rule from a segment on the
footprint, and the zones / virtual colocations of a metro. A test breaks one element of a complete setup and expects
exactly one finding that names the rule, the footprint, the context and the missing element. One test runs the check
class on a raw GraphQL payload shaped like queries/validation/internet_egress.gql, so the wiring through
``clean_data`` is covered too.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.internet_egress import (
    CheckInternetEgress,
    context_gaps,
    egress_findings,
    internet_rules,
    virtual_pair_gaps,
)
from utils.data_cleaning import clean_data
from utils.exchange_transit import INTERNET_TYPE, INTERNET_ZONE, transit_vlan

VLAN = 3000
INTERNET_VLAN = transit_vlan(VLAN, INTERNET_TYPE)
SEGMENT = {"id": "seg-1", "name": "c006-nordix-prod-p"}
METRO = {"id": "metro-fr", "name": "FR"}


def _device(device_id: str, role: str = "firewall", kind: str = "DcimPhysicalDevice", **extra: Any) -> dict[str, Any]:
    """A cluster member / zone device as the query returns it, cleaned."""
    return {"id": device_id, "typename": kind, "name": device_id, "role": role, **extra}


def _leg(
    device: str,
    port: str = "eth1",
    vlan: int = INTERNET_VLAN,
    namespace: tuple[str, str] | None = ("INTERNET", INTERNET_TYPE),
    exchange: str | None = "ex-1",
) -> dict[str, Any]:
    """The sub-interface ``<port>.<vlan>`` of a member, with an address in ``namespace`` (None: no address)."""
    address = (
        {
            "id": f"ip-{device}-{vlan}",
            "address": "192.0.2.5/29",
            "ip_namespace": {"name": namespace[0], "namespace_type": namespace[1]},
        }
        if namespace
        else None
    )
    return {
        "id": f"leg-{device}-{vlan}",
        "typename": "DcimVirtualInterface",
        "name": f"{port}.{vlan}",
        "role": "firewall",
        "device": {"id": device, "name": device, "role": "firewall"},
        "ip_address": address,
        "capabilities": [{"typename": "TopologyRoutedExchange", "id": exchange, "name": "ex"}] if exchange else [],
    }


def _port(port_id: str, device_role: str = "border-leaf", role: str = "firewall") -> dict[str, Any]:
    """A physical port of a border device, as tagged with the context."""
    return {
        "id": port_id,
        "typename": "DcimPhysicalInterface",
        "name": port_id,
        "role": role,
        "device": {"id": f"dev-{port_id}", "name": f"dev-{port_id}", "role": device_role},
    }


def _context(prefix: str = "dc", members: tuple[str, str] = ("fw-1", "fw-2")) -> dict[str, Any]:
    """A complete shared context: pair, INTERNET legs, border ports, SNAT. Ids are prefixed per facility."""
    ids = [f"{prefix}-{member}" for member in members]
    return {
        "id": f"ctx-{prefix}",
        "name": f"shared-{prefix}-prod",
        "vlan_id": VLAN,
        "snat_address": {"id": f"snat-{prefix}", "address": "198.51.100.1/32"},
        "cluster": {"id": f"ha-{prefix}", "name": f"ha-{prefix}", "capabilities": [_device(i) for i in ids]},
        "capabilities": [
            *(_leg(i, exchange=f"ex-{prefix}") for i in ids),
            *(_leg(i, vlan=VLAN, namespace=("PROD", "prod"), exchange=None) for i in ids),
            _port(f"{prefix}-bl-1"),
            _port(f"{prefix}-bl-2"),
        ],
    }


def _exchange(prefix: str = "dc") -> dict[str, Any]:
    """The context's exchange between the tenant VRF and INTERNET."""
    return {
        "id": f"ex-{prefix}",
        "name": f"shared-{prefix}-prod-PROD-INTERNET",
        "gateway": {"id": f"ctx-{prefix}", "name": f"shared-{prefix}-prod"},
        "namespace_a": {"id": "ns-prod", "name": "PROD", "namespace_type": "prod"},
        "namespace_z": {"id": "ns-inet", "name": "INTERNET", "namespace_type": INTERNET_TYPE},
    }


def _rule(
    name: str = "to-internet",
    zone: str | None = INTERNET_ZONE,
    segment: dict[str, Any] | None = SEGMENT,
    disabled: bool = False,
    policy_enabled: bool = True,
) -> dict[str, Any]:
    """A cleaned SecurityPolicyRule."""
    return {
        "id": f"rule-{name}",
        "name": name,
        "disabled": disabled,
        "policy": {"id": "pol-1", "name": "pol", "enabled": policy_enabled},
        "source_segment": segment,
        "destination_zone": {"id": "zone", "name": zone} if zone else None,
    }


def _dc(contexts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A DC footprint carrying the segment. It has ONE serving context (none for ``[]``)."""
    contexts = [_context()] if contexts is None else contexts
    assert len(contexts) <= 1
    return {
        "id": "fp-dc",
        "name": "C006-P-DC11",
        "network_segments": [SEGMENT],
        "serving_firewall_context": contexts[0] if contexts else None,
    }


def _colocation(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """A colocation footprint of the FR metro carrying the segment."""
    return {
        "id": "fp-colo",
        "name": "C001-P-FR",
        "parent": METRO,
        "network_segments": [SEGMENT],
        "serving_firewall_contexts": contexts,
    }


def _zone(prefix: str, with_firewalls: bool = True) -> dict[str, Any]:
    """A physical zone of the FR metro; its firewall devices are the members of ``_context(prefix)``."""
    devices = [_device(f"{prefix}-fw-1"), _device(f"{prefix}-fw-2")] if with_firewalls else []
    return {"id": f"zone-{prefix}", "name": prefix.upper(), "parent": METRO, "devices": devices}


def _virtual_colocation(members: list[dict[str, Any]] | None = None, name: str = "AM-NE") -> dict[str, Any]:
    """A virtual colocation of the FR metro whose appliances share one firewall HA domain of ``members``."""
    if members is None:
        members = [_device(f"ne-fw-{n}", kind="CloudNetworkAppliance", status="active") for n in (1, 2)]
    domain = {"id": "ha-ne", "typename": "ManagedFirewallHA", "name": "ha-ne", "capabilities": members}
    appliances = [{"id": member["id"], "name": member["id"], "capabilities": [domain]} for member in members]
    return {"id": f"vc-{name}", "name": name, "parent": METRO, "appliances": appliances}


def _cleaned(
    footprints: list[dict[str, Any]],
    kind: str = "TopologyCustomerDC",
    rules: list[dict[str, Any]] | None = None,
    exchanges: list[dict[str, Any]] | None = None,
    zones: list[dict[str, Any]] | None = None,
    virtual: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The cleaned query result."""
    return {
        "SecurityPolicyRule": [_rule()] if rules is None else rules,
        kind: footprints,
        "TopologyRoutedExchange": [_exchange()] if exchanges is None else exchanges,
        "TopologyColocationZone": zones or [],
        "TopologyVirtualColocation": virtual or [],
    }


def _colocation_cleaned(
    contexts: list[dict[str, Any]],
    zones: list[dict[str, Any]],
    virtual: list[dict[str, Any]] | None = None,
    prefixes: tuple[str, ...] = ("fr2",),
) -> dict[str, Any]:
    """A colocation footprint with a complete exchange per zone prefix."""
    return _cleaned(
        [_colocation(contexts)],
        kind="TopologyCustomerColocation",
        exchanges=[_exchange(prefix) for prefix in prefixes],
        zones=zones,
        virtual=virtual,
    )


class TestDataCenter:
    """A DC footprint: the serving context must be hardware that enforces the rule."""

    def test_complete_context_passes(self) -> None:
        """A pair with legs, exchange, ports and SNAT raises nothing."""
        assert egress_findings(_cleaned([_dc()])) == []

    def test_no_serving_context_is_an_error(self) -> None:
        """A footprint with no serving context has no firewall at all."""
        (finding,) = egress_findings(_cleaned([_dc([])]))
        assert "rule 'to-internet'" in finding
        assert "TopologyCustomerDC 'C006-P-DC11'" in finding
        assert "no serving firewall context" in finding

    def test_empty_cluster(self) -> None:
        """A cluster without members lacks the pair (and with it every leg)."""
        context = _context()
        context["cluster"]["capabilities"] = []
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "context 'shared-dc-prod'" in finding
        assert "cluster 'ha-dc' has 0 member device(s)" in finding

    def test_no_cluster(self) -> None:
        """A context with no cluster link is reported as such."""
        context = _context()
        context["cluster"] = None
        findings = egress_findings(_cleaned([_dc([context])]))
        assert any("no cluster (ManagedFirewallHA)" in finding for finding in findings)

    def test_one_member_only(self) -> None:
        """One member of a pair is not a pair."""
        context = _context()
        context["cluster"]["capabilities"].pop()
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "has 1 member device(s), 1 with role firewall" in finding

    def test_member_with_another_role(self) -> None:
        """Two members of which one is no firewall are not a firewall pair."""
        context = _context()
        context["cluster"]["capabilities"][1]["role"] = "load-balancer"
        findings = egress_findings(_cleaned([_dc([context])]))
        assert any("2 member device(s), 1 with role firewall" in finding for finding in findings)

    def test_member_without_internet_leg(self) -> None:
        """A member with no sub-interface in INTERNET leaves half of the pair without a path."""
        context = _context()
        context["capabilities"] = [
            leg
            for leg in context["capabilities"]
            if leg.get("name") != f"eth1.{INTERNET_VLAN}" or "fw-2" not in leg["id"]
        ]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "member 'dc-fw-2' has no INTERNET leg" in finding
        assert f"<port>.{INTERNET_VLAN}" in finding

    def test_leg_without_address(self) -> None:
        """A leg of the INTERNET VLAN with no address is reported, not skipped."""
        context = _context()
        context["capabilities"] = [
            _leg("dc-fw-1", namespace=None, exchange="ex-dc") if leg["id"] == f"leg-dc-fw-1-{INTERNET_VLAN}" else leg
            for leg in context["capabilities"]
        ]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "INTERNET leg 'dc-fw-1:eth1." in finding
        assert "has no address" in finding

    def test_leg_addressed_in_another_namespace(self) -> None:
        """An address outside the INTERNET namespace is no INTERNET leg."""
        context = _context()
        context["capabilities"] = [
            _leg("dc-fw-2", namespace=("PROD", "prod"), exchange="ex-dc")
            if leg["id"] == f"leg-dc-fw-2-{INTERNET_VLAN}"
            else leg
            for leg in context["capabilities"]
        ]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "addressed in namespace 'PROD', not in the INTERNET namespace" in finding

    def test_no_exchange(self) -> None:
        """Without the exchange the tenant VRF has no hop to INTERNET."""
        (finding,) = egress_findings(_cleaned([_dc()], exchanges=[]))
        assert "no TopologyRoutedExchange between the tenant VRF and INTERNET" in finding

    def test_exchange_of_another_context_does_not_count(self) -> None:
        """The exchange must belong to the context, not to another one."""
        (finding,) = egress_findings(_cleaned([_dc()], exchanges=[_exchange("other")]))
        assert "no TopologyRoutedExchange" in finding

    def test_exchange_between_two_tenant_vrfs_does_not_count(self) -> None:
        """An exchange that does not reach INTERNET does not count."""
        exchange = _exchange()
        exchange["namespace_z"] = {"id": "ns-mgmt", "name": "MANAGEMENT", "namespace_type": "management"}
        (finding,) = egress_findings(_cleaned([_dc()], exchanges=[exchange]))
        assert "no TopologyRoutedExchange" in finding

    def test_leg_not_linked_to_the_exchange(self) -> None:
        """A leg that is not in the exchange is not on its path."""
        context = _context()
        context["capabilities"] = [
            _leg("dc-fw-1", exchange=None) if leg["id"] == f"leg-dc-fw-1-{INTERNET_VLAN}" else leg
            for leg in context["capabilities"]
        ]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "is not linked to the exchange" in finding

    def test_untagged_service_port(self) -> None:
        """One border service port tagged for a pair of members leaves one member without its port."""
        context = _context()
        context["capabilities"] = [cap for cap in context["capabilities"] if cap["id"] != "dc-bl-2"]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "1 border service port(s) tagged with the context" in finding

    def test_port_of_a_non_border_device_or_another_role_is_no_service_port(self) -> None:
        """Only a firewall-role port of a border or edge device counts."""
        context = _context()
        context["capabilities"] = [
            *(cap for cap in context["capabilities"] if cap["typename"] != "DcimPhysicalInterface"),
            _port("p1", device_role="leaf"),
            _port("p2", role="uplink"),
        ]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "0 border service port(s)" in finding

    def test_edge_port_counts(self) -> None:
        """A colocation edge carries the legs like a border leaf."""
        context = _context()
        context["capabilities"] = [
            _port(cap["id"], device_role="edge") if cap["typename"] == "DcimPhysicalInterface" else cap
            for cap in context["capabilities"]
        ]
        assert egress_findings(_cleaned([_dc([context])])) == []

    def test_missing_snat(self) -> None:
        """Without a SNAT address the tenant traffic leaves with a private source."""
        context = _context()
        context["snat_address"] = None
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "no snat_address" in finding

    def test_missing_vlan_id(self) -> None:
        """A context without its transit VLAN cannot name its legs; the legs are still found by their namespace."""
        context = _context()
        context["vlan_id"] = None
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "no vlan_id" in finding

    def test_one_finding_per_missing_element(self) -> None:
        """Several missing elements are as many findings, each naming the rule, footprint and context."""
        context = _context()
        context["snat_address"] = None
        findings = egress_findings(_cleaned([_dc([context])], exchanges=[]))
        assert len(findings) == 2
        assert all("rule 'to-internet'" in f and "C006-P-DC11" in f and "shared-dc-prod" in f for f in findings)


class TestColocation:
    """A colocation footprint: its metro's zones with firewalls, or a complete virtual pair."""

    def test_two_zones_one_broken_is_an_error_for_the_broken_one(self) -> None:
        """Each zone with firewalls must pass: the zone that does not is named."""
        broken = _context("fr6")
        broken["snat_address"] = None
        cleaned = _colocation_cleaned(
            [_context("fr2"), broken], zones=[_zone("fr2"), _zone("fr6")], prefixes=("fr2", "fr6")
        )
        (finding,) = egress_findings(cleaned)
        assert "TopologyCustomerColocation 'C001-P-FR'" in finding
        assert "context 'shared-fr6-prod'" in finding
        assert "zone 'FR6'" in finding
        assert "no snat_address" in finding

    def test_two_complete_zones_pass(self) -> None:
        """Every zone with a pair serves."""
        cleaned = _colocation_cleaned(
            [_context("fr2"), _context("fr6")], zones=[_zone("fr2"), _zone("fr6")], prefixes=("fr2", "fr6")
        )
        assert egress_findings(cleaned) == []

    def test_zone_without_firewalls_is_ignored(self) -> None:
        """A cage with no firewall device is neither served nor an error."""
        cleaned = _colocation_cleaned([_context("fr2")], zones=[_zone("fr2"), _zone("fr6", with_firewalls=False)])
        assert egress_findings(cleaned) == []

    def test_zone_with_firewalls_but_no_serving_context(self) -> None:
        """A zone whose pair has no context of the footprint is an error naming the zone."""
        cleaned = _colocation_cleaned([_context("fr2")], zones=[_zone("fr2"), _zone("fr6")])
        (finding,) = egress_findings(cleaned)
        assert "zone 'FR6' has a firewall pair but no serving context" in finding

    def test_only_a_complete_virtual_pair_passes(self) -> None:
        """A metro with no physical zone with firewalls is served by a complete provider-hosted pair."""
        cleaned = _colocation_cleaned([], zones=[_zone("fr6", with_firewalls=False)], virtual=[_virtual_colocation()])
        assert egress_findings(cleaned) == []

    def test_incomplete_virtual_pair_is_an_error(self) -> None:
        """A single appliance in the HA domain is no pair, and then nothing serves the footprint."""
        members = [_device("ne-fw-1", kind="CloudNetworkAppliance", status="active")]
        cleaned = _colocation_cleaned([], zones=[], virtual=[_virtual_colocation(members)])
        (finding,) = egress_findings(cleaned)
        assert "no firewall serves the footprint" in finding
        assert "virtual colocation 'AM-NE'" in finding
        assert "has 1 member(s)" in finding

    @pytest.mark.parametrize(
        "change",
        [{"role": "edge"}, {"status": "provisioning"}, {"typename": "DcimPhysicalDevice"}],
        ids=["not-firewall", "not-active", "not-an-appliance"],
    )
    def test_virtual_pair_members_must_be_active_firewall_appliances(self, change: dict[str, Any]) -> None:
        """Role, status and kind of both members decide the pair."""
        members = [_device(f"ne-fw-{n}", kind="CloudNetworkAppliance", status="active") for n in (1, 2)]
        members[1].update(change)
        assert virtual_pair_gaps(_virtual_colocation(members)) != []

    def test_virtual_colocation_without_firewall_domain(self) -> None:
        """Routers only: no firewall HA domain on the appliances."""
        colocation = _virtual_colocation()
        for appliance in colocation["appliances"]:
            appliance["capabilities"] = []
        assert virtual_pair_gaps(colocation) == ["it hosts no firewall HA domain (ManagedFirewallHA)"]

    def test_metro_with_neither_is_an_error(self) -> None:
        """No zone with a firewall pair and no virtual colocation: nothing serves the footprint."""
        cleaned = _colocation_cleaned([], zones=[_zone("fr6", with_firewalls=False)])
        (finding,) = egress_findings(cleaned)
        assert "no firewall serves the footprint" in finding
        assert "metro 'FR'" in finding

    def test_virtual_pair_of_another_metro_does_not_serve(self) -> None:
        """Only the virtual colocations of the footprint's own metro count."""
        other = _virtual_colocation()
        other["parent"] = {"id": "metro-am", "name": "AM"}
        cleaned = _colocation_cleaned([], zones=[], virtual=[other])
        (finding,) = egress_findings(cleaned)
        assert "no firewall serves the footprint" in finding

    def test_virtual_pair_does_not_excuse_a_broken_zone(self) -> None:
        """A zone with firewalls must pass even where a virtual pair also serves the metro."""
        broken = _context("fr2")
        broken["snat_address"] = None
        cleaned = _colocation_cleaned([broken], zones=[_zone("fr2")], virtual=[_virtual_colocation()])
        (finding,) = egress_findings(cleaned)
        assert "no snat_address" in finding


class TestWhichRulesAndFootprints:
    """What is checked at all."""

    def test_disabled_rule_is_ignored(self) -> None:
        """A disabled rule enforces nothing, so nothing has to enforce it."""
        assert egress_findings(_cleaned([_dc([])], rules=[_rule(disabled=True)])) == []

    def test_rule_of_a_disabled_policy_is_ignored(self) -> None:
        """A rule in a switched-off policy is not enabled either."""
        assert egress_findings(_cleaned([_dc([])], rules=[_rule(policy_enabled=False)])) == []

    def test_non_internet_rule_is_ignored(self) -> None:
        """A rule to a segment, or to another zone, is not internet egress."""
        rules = [_rule(zone=None), _rule(name="to-prod", zone="PROD-ZONE")]
        assert egress_findings(_cleaned([_dc([])], rules=rules)) == []

    @pytest.mark.parametrize("kind", ["TopologyCustomerCloud", "TopologyCustomerOffice"])
    def test_cloud_and_office_footprints_are_ignored(self, kind: str) -> None:
        """Cloud and office egress is a deliberate gap: such footprints are not read at all."""
        cleaned = _cleaned([], rules=[_rule()])
        cleaned[kind] = [_dc([])]
        assert egress_findings(cleaned) == []

    def test_segment_on_no_on_prem_footprint_is_ignored(self) -> None:
        """A rule whose source segment sits on no DC or colocation footprint has nothing to check."""
        footprint = _dc([])
        footprint["network_segments"] = [{"id": "other", "name": "other"}]
        assert egress_findings(_cleaned([footprint])) == []

    def test_rule_without_source_segment_is_ignored(self) -> None:
        """No source segment, no footprint."""
        assert egress_findings(_cleaned([_dc([])], rules=[_rule(segment=None)])) == []

    def test_every_rule_is_named_once_per_footprint(self) -> None:
        """Two rules from the segment give a finding each, sorted by rule name."""
        findings = egress_findings(_cleaned([_dc([])], rules=[_rule("b-rule"), _rule("a-rule")]))
        assert [f.split("'")[1] for f in findings] == ["a-rule", "b-rule"]

    def test_internet_rules_keeps_only_enabled_internet_rules_with_a_segment(self) -> None:
        """The selection the check starts from."""
        rules = [
            _rule("keep"),
            _rule("off", disabled=True),
            _rule("other", zone="PROD-ZONE"),
            _rule("none", segment=None),
        ]
        assert [rule["name"] for rule in internet_rules(rules)] == ["keep"]

    def test_context_gaps_of_a_complete_context_is_empty(self) -> None:
        """The pure per-context function, without a footprint."""
        assert context_gaps(_context(), [_exchange()]) == []


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _raw_payload(snat: bool) -> dict[str, Any]:
    """A raw GraphQL response (queries/validation/internet_egress.gql) of a complete DC context."""
    value = lambda v: {"value": v}  # noqa: E731
    members = [
        {"id": f"fw-{n}", "__typename": "DcimPhysicalDevice", "name": value(f"fw-{n}"), "role": value("firewall")}
        for n in (1, 2)
    ]
    legs = [
        {
            "id": f"leg-{n}",
            "__typename": "DcimVirtualInterface",
            "name": value(f"eth1.{INTERNET_VLAN}"),
            "role": value("firewall"),
            "device": {"node": {"id": f"fw-{n}", "name": value(f"fw-{n}"), "role": value("firewall")}},
            "ip_address": {
                "node": {
                    "id": f"ip-{n}",
                    "address": value("192.0.2.5/29"),
                    "ip_namespace": {"node": {"name": value("INTERNET"), "namespace_type": value(INTERNET_TYPE)}},
                }
            },
            "capabilities": _edges([{"__typename": "TopologyRoutedExchange", "id": "ex-1", "name": value("ex")}]),
        }
        for n in (1, 2)
    ]
    ports = [
        {
            "id": f"bl-{n}",
            "__typename": "DcimPhysicalInterface",
            "name": value(f"Ethernet1/{n}"),
            "role": value("firewall"),
            "device": {"node": {"id": f"border-{n}", "name": value(f"border-{n}"), "role": value("border-leaf")}},
        }
        for n in (1, 2)
    ]
    context = {
        "id": "ctx-1",
        "name": value("shared-dc10-prod"),
        "vlan_id": value(VLAN),
        "snat_address": {"node": {"id": "snat", "address": value("198.51.100.1/32")}} if snat else {"node": None},
        "cluster": {"node": {"id": "ha-1", "name": value("ha-1"), "capabilities": _edges(members)}},
        "capabilities": _edges([*legs, *ports]),
    }
    return {
        "SecurityPolicyRule": _edges(
            [
                {
                    "id": "rule-1",
                    "name": value("to-internet"),
                    "disabled": value(False),
                    "policy": {"node": {"id": "pol", "name": value("pol"), "enabled": value(True)}},
                    "source_segment": {"node": {"id": "seg-1", "name": value("seg")}},
                    "destination_zone": {"node": {"id": "zone", "name": value(INTERNET_ZONE)}},
                }
            ]
        ),
        "TopologyCustomerDC": _edges(
            [
                {
                    "id": "fp-1",
                    "name": value("C006-P-DC11"),
                    "network_segments": _edges([{"id": "seg-1", "name": value("seg")}]),
                    "serving_firewall_context": {"node": context},
                }
            ]
        ),
        "TopologyCustomerColocation": _edges([]),
        "TopologyRoutedExchange": _edges(
            [
                {
                    "id": "ex-1",
                    "name": value("ex"),
                    "gateway": {"node": {"id": "ctx-1", "name": value("shared-dc10-prod")}},
                    "namespace_a": {"node": {"id": "a", "name": value("PROD"), "namespace_type": value("prod")}},
                    "namespace_z": {
                        "node": {"id": "z", "name": value("INTERNET"), "namespace_type": value(INTERNET_TYPE)}
                    },
                }
            ]
        ),
        "TopologyColocationZone": _edges([]),
        "TopologyVirtualColocation": _edges([]),
    }


class TestCheckClass:
    """The check on raw GraphQL data."""

    @staticmethod
    def _run(data: dict[str, Any]) -> list[str]:
        check = cast(Any, CheckInternetEgress.__new__(CheckInternetEgress))
        errors: list[str] = []
        check.log_error = lambda message: errors.append(message)
        check.validate(data)
        return errors

    def test_complete_raw_payload_passes(self) -> None:
        """clean_data keeps every shape the check reads."""
        assert self._run(_raw_payload(snat=True)) == []
        assert (
            clean_data(_raw_payload(snat=True))["TopologyCustomerDC"][0]["serving_firewall_context"]["vlan_id"] == VLAN
        )

    def test_raw_payload_without_snat_logs_an_error(self) -> None:
        """A missing element is logged as an error."""
        (error,) = self._run(_raw_payload(snat=False))
        assert "no snat_address" in error
