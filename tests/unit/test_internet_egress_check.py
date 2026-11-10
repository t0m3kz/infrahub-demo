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
from tests.smoke.fixture_builders.vrf import interface_vrf_capability, routing_vrf
from tests.unit.routing_helpers import in_vrf, make_vrf
from utils.data_cleaning import clean_data
from utils.exchange_transit import INTERNET_TYPE, INTERNET_ZONE, transit_vlan

VLAN = 3000
INTERNET_VLAN = transit_vlan(VLAN, INTERNET_TYPE)
SEGMENT = {"id": "seg-1", "name": "c006-nordix-prod-p"}
METRO = {"id": "metro-fr", "name": "FR"}


def _device(
    device_id: str, role: str = "internal-firewall", kind: str = "DcimPhysicalDevice", **extra: Any
) -> dict[str, Any]:
    """A cluster member / zone device as the query returns it, cleaned."""
    return {"id": device_id, "typename": kind, "name": device_id, "role": role, **extra}


def _leg(
    device: str,
    port: str = "eth1",
    vlan: int = INTERNET_VLAN,
    namespace: tuple[str, str] | None = ("INTERNET", INTERNET_TYPE),
    name: str | None = None,
) -> dict[str, Any]:
    """The sub-interface of a member tagged ``vlan`` (named ``<port>.<vlan>`` unless ``name`` says otherwise), with
    an address in ``namespace`` (None: no address) and in its VRF instance: an exchange's legs are the context's legs in
    its two VRFs (derived), so the leg names no exchange."""
    address = (
        {
            "id": f"ip-{device}-{vlan}",
            "address": "192.0.2.5/29",
            "ip_namespace": {"node": {"name": {"value": "default"}}},
        }
        if namespace
        else None
    )
    return {
        "id": f"leg-{device}-{vlan}",
        "typename": "DcimVirtualInterface",
        "name": f"{port}.{vlan}" if name is None else name,
        "vlan_id": vlan,
        "parent_interface": {"name": port, "device": {"id": device}},
        "role": "firewall",
        "device": {"id": device, "name": device, "role": "internal-firewall"},
        "ip_address": address,
        **(in_vrf(make_vrf({"name": namespace[0], "vrf_type": namespace[1]})) if namespace else {}),
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


def _member(prefix: str, member_id: str, role: str, port_index: int) -> dict[str, Any]:
    """A member of the shared context with what its legs and border port derive from: the context it belongs to, its
    uplink ``eth1`` cabled to a border service port, and the legs `eth1.<transit VLAN>` of the PROD and INTERNET slots."""
    uplink_id = f"{member_id}:up"
    return {
        **_device(member_id, role),
        "shared_capabilities": [{"typename": "ManagedFirewallContext", "id": f"ctx-{prefix}", "vlan_id": VLAN}],
        "interfaces": [
            {
                "id": uplink_id,
                "typename": "DcimPhysicalInterface",
                "name": "eth1",
                "role": "uplink",
                "cable": {
                    "id": f"cable-{member_id}",
                    "endpoints": [{"id": uplink_id}, _port(f"{prefix}-bl-{port_index}")],
                },
            },
            _leg(member_id),
            _leg(member_id, vlan=VLAN, namespace=("PROD", "prod")),
        ],
    }


def _context(
    prefix: str = "dc", members: tuple[str, str] = ("fw-1", "fw-2"), role: str = "internal-firewall"
) -> dict[str, Any]:
    """A complete shared context: pair, INTERNET legs, border ports, SNAT. Ids are prefixed per facility."""
    return {
        "id": f"ctx-{prefix}",
        "name": f"shared-{prefix}-prod",
        "vlan_id": VLAN,
        "snat_address": {"id": f"snat-{prefix}", "address": "198.51.100.1/32"},
        "shared_devices": [_member(prefix, f"{prefix}-{m}", role, n) for n, m in enumerate(members, start=1)],
    }


def _member_of(context: dict[str, Any], member_id: str) -> dict[str, Any]:
    return next(m for m in context["shared_devices"] if m["id"] == member_id)


def _internet_leg(context: dict[str, Any], member_id: str) -> dict[str, Any]:
    return next(i for i in _member_of(context, member_id)["interfaces"] if i["name"] == f"eth1.{INTERNET_VLAN}")


def _far_end(context: dict[str, Any], member_id: str) -> dict[str, Any]:
    """The border service port the member's uplink is cabled to."""
    return _member_of(context, member_id)["interfaces"][0]["cable"]["endpoints"][1]


def _exchange(prefix: str = "dc") -> dict[str, Any]:
    """The context's exchange between the tenant VRF and INTERNET."""
    return {
        "id": f"ex-{prefix}",
        "name": f"shared-{prefix}-prod-PROD-INTERNET",
        "gateway": {"id": f"ctx-{prefix}", "name": f"shared-{prefix}-prod"},
        "vrf_a": make_vrf({"name": "PROD", "vrf_type": "prod"}),
        "vrf_z": make_vrf({"name": "INTERNET", "vrf_type": INTERNET_TYPE}),
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


def _dc(contexts: list[dict[str, Any]] | None = None, environment: str = "p") -> dict[str, Any]:
    """A DC footprint carrying the segment, listing ``contexts`` (one complete shared one by default, none for ``[]``)."""
    return {
        "id": "fp-dc",
        "name": "C006-P-DC11",
        "environment": environment,
        "network_segments": [SEGMENT],
        "serving_firewall_contexts": [_context()] if contexts is None else contexts,
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

    @pytest.mark.parametrize("members", [[], None])
    def test_no_member_firewalls(self, members: list[dict[str, Any]] | None) -> None:
        """A context with no member devices (empty or absent `shared_devices`) lacks the pair and every leg."""
        context = _context()
        context["shared_devices"] = members
        findings = egress_findings(_cleaned([_dc([context])]))
        assert any("no member firewalls (shared_devices)" in finding for finding in findings)

    def test_one_member_only(self) -> None:
        """One member of a pair is not a pair."""
        context = _context()
        context["shared_devices"].pop()
        findings = egress_findings(_cleaned([_dc([context])]))
        assert any("has 1 member device(s), 1 with role internal-firewall" in finding for finding in findings)

    def test_member_with_another_role(self) -> None:
        """Two members of which one is no firewall are not a firewall pair."""
        context = _context()
        context["shared_devices"][1]["role"] = "load-balancer"
        findings = egress_findings(_cleaned([_dc([context])]))
        assert any("2 member device(s), 1 with role internal-firewall" in finding for finding in findings)

    def test_member_without_internet_leg(self) -> None:
        """A member with no sub-interface in INTERNET leaves half of the pair without a path."""
        context = _context()
        member = _member_of(context, "dc-fw-2")
        member["interfaces"] = [i for i in member["interfaces"] if i["name"] != f"eth1.{INTERNET_VLAN}"]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "member 'dc-fw-2' has no INTERNET leg" in finding
        assert f"VLAN {INTERNET_VLAN}" in finding

    def test_the_leg_is_found_by_its_tag_not_its_name(self) -> None:
        """A leg named otherwise but tagged with the INTERNET VLAN is the leg; one named .3400 over another tag is not."""
        context = _context()
        member = _member_of(context, "dc-fw-2")
        leg_id = f"leg-dc-fw-2-{INTERNET_VLAN}"
        member["interfaces"] = [
            _leg("dc-fw-2", name="uplink-internet") if i["id"] == leg_id else i for i in member["interfaces"]
        ]
        assert egress_findings(_cleaned([_dc([context])])) == []
        member["interfaces"] = [
            {**i, "vlan_id": 3999, "ip_address": None, "zone_capabilities": []} if i["id"] == leg_id else i
            for i in member["interfaces"]
        ]
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "member 'dc-fw-2' has no INTERNET leg" in finding

    def test_leg_without_address(self) -> None:
        """A leg of the INTERNET VLAN with no address is reported, not skipped."""
        context = _context()
        leg = _internet_leg(context, "dc-fw-1")
        leg["ip_address"], leg["zone_capabilities"] = None, []
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "INTERNET leg 'dc-fw-1:eth1." in finding
        assert "has no address" in finding

    def test_leg_addressed_in_another_namespace(self) -> None:
        """An address outside the INTERNET namespace is no INTERNET leg."""
        context = _context()
        _internet_leg(context, "dc-fw-2").update(in_vrf(make_vrf({"name": "PROD", "vrf_type": "prod"})))
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
        exchange["vrf_z"] = make_vrf({"name": "MANAGEMENT", "vrf_type": "management"})
        (finding,) = egress_findings(_cleaned([_dc()], exchanges=[exchange]))
        assert "no TopologyRoutedExchange" in finding

    def test_leg_not_in_a_vrf_of_the_exchange(self) -> None:
        """A leg in another INTERNET instance than the exchange's is no leg of it, so it is not on its path."""
        context = _context()
        leg = _internet_leg(context, "dc-fw-1")
        leg.update(in_vrf(make_vrf({"name": "INTERNET", "vrf_type": INTERNET_TYPE}, deployment="other-dc")))
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "is not in a VRF of the exchange" in finding

    def test_untagged_service_port(self) -> None:
        """One border service port tagged for a pair of members leaves one member without its port."""
        context = _context()
        _member_of(context, "dc-fw-2")["interfaces"][0]["cable"] = None
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "1 border service port(s) tagged with the context" in finding

    def test_port_of_a_non_border_device_or_another_role_is_no_service_port(self) -> None:
        """Only a firewall-role port of a border or edge device counts."""
        context = _context()
        _member_of(context, "dc-fw-1")["interfaces"][0]["cable"]["endpoints"][1] = _port("p1", device_role="leaf")
        _member_of(context, "dc-fw-2")["interfaces"][0]["cable"]["endpoints"][1] = _port("p2", role="uplink")
        (finding,) = egress_findings(_cleaned([_dc([context])]))
        assert "0 border service port(s)" in finding

    def test_edge_port_counts(self) -> None:
        """A colocation edge carries the legs like a border leaf."""
        context = _context()
        for member_id in ("dc-fw-1", "dc-fw-2"):
            _far_end(context, member_id)["device"]["role"] = "edge"
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
        rules = [_rule(zone=None), _rule(name="to-prod", zone="PROD")]
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
            _rule("other", zone="PROD"),
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
        {
            "id": f"fw-{n}",
            "__typename": "DcimPhysicalDevice",
            "name": value(f"fw-{n}"),
            "role": value("internal-firewall"),
            "shared_capabilities": _edges(
                [{"__typename": "ManagedFirewallContext", "id": "ctx-1", "vlan_id": value(VLAN)}]
            ),
            "interfaces": _edges(
                [
                    {
                        "__typename": "DcimPhysicalInterface",
                        "id": f"up-{n}",
                        "name": value("eth1"),
                        "role": value("uplink"),
                        "cable": {
                            "node": {
                                "id": f"cable-{n}",
                                "endpoints": _edges(
                                    [
                                        {"__typename": "DcimPhysicalInterface", "id": f"up-{n}"},
                                        {
                                            "__typename": "DcimPhysicalInterface",
                                            "id": f"bl-{n}",
                                            "role": value("firewall"),
                                            "device": {"node": {"role": value("border-leaf")}},
                                        },
                                    ]
                                ),
                            }
                        },
                    },
                    {
                        **interface_vrf_capability(routing_vrf("INTERNET")),
                        "id": f"leg-{n}",
                        "__typename": "DcimVirtualInterface",
                        "name": value(f"eth1.{INTERNET_VLAN}"),
                        "vlan_id": value(INTERNET_VLAN),
                        "parent_interface": {"node": {"name": value("eth1"), "device": {"node": {"id": f"fw-{n}"}}}},
                        "role": value("firewall"),
                        "ip_address": {
                            "node": {
                                "id": f"ip-{n}",
                                "address": value("192.0.2.5/29"),
                                "ip_namespace": {"node": {"name": {"value": "default"}}},
                            }
                        },
                        "capabilities": _edges(
                            [{"__typename": "TopologyRoutedExchange", "id": "ex-1", "name": value("ex")}]
                        ),
                    },
                ]
            ),
        }
        for n in (1, 2)
    ]
    context = {
        "id": "ctx-1",
        "name": value("shared-dc10-prod"),
        "vlan_id": value(VLAN),
        "snat_address": {"node": {"id": "snat", "address": value("198.51.100.1/32")}} if snat else {"node": None},
        "shared_devices": _edges(members),
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
                    "serving_firewall_contexts": _edges([context]),
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
                    "vrf_a": {"node": routing_vrf("PROD")},
                    "vrf_z": {"node": routing_vrf("INTERNET")},
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
            clean_data(_raw_payload(snat=True))["TopologyCustomerDC"][0]["serving_firewall_contexts"][0]["vlan_id"]
            == VLAN
        )

    def test_raw_payload_without_snat_logs_an_error(self) -> None:
        """A missing element is logged as an error."""
        (error,) = self._run(_raw_payload(snat=False))
        assert "no snat_address" in error


def _dedicated(snat: bool = True) -> dict[str, Any]:
    """A dedicated internal context with no Internet leg: only its own public address, when it has one."""
    return {
        "id": "ctx-ded",
        "name": "c006-dedicated-prod",
        "tenant": {"id": "fp-dc"},
        "snat_address": {"id": "snat-ded", "address": "198.51.100.9/32"} if snat else None,
        "shared_devices": [{"id": "ded-fw-1", "name": "ded-fw-1", "role": "internal-firewall"}],
    }


def _edge(prefix: str = "eg") -> dict[str, Any]:
    """A complete Internet edge context on an external-firewall pair."""
    return _context(prefix, role="external-firewall")


class TestInternetEdge:
    """A footprint that lists an Internet edge context: it is checked against that context, not its internal one."""

    def test_dedicated_context_without_legs_passes_when_the_edge_is_complete(self) -> None:
        cleaned = _cleaned([_dc([_dedicated(), _edge()])], exchanges=[_exchange("eg")])
        assert egress_findings(cleaned) == []

    def test_a_shared_internal_context_needs_no_public_address(self) -> None:
        shared = _context()
        shared["snat_address"] = None
        cleaned = _cleaned([_dc([shared, _edge()])], exchanges=[_exchange("eg")])
        assert egress_findings(cleaned) == []

    def test_the_edge_context_is_held_to_every_element(self) -> None:
        edge = _edge()
        edge["snat_address"] = None
        findings = egress_findings(_cleaned([_dc([_dedicated(), edge])], exchanges=[_exchange("eg")]))
        assert len(findings) == 1
        assert "egress context: no snat_address" in findings[0] and "shared-eg-prod" in findings[0]

    def test_a_dedicated_context_needs_its_own_public_address(self) -> None:
        cleaned = _cleaned([_dc([_dedicated(snat=False), _edge()])], exchanges=[_exchange("eg")])
        findings = egress_findings(cleaned)
        assert len(findings) == 1 and "c006-dedicated-prod" in findings[0] and "no snat_address" in findings[0]

    def test_an_edge_only_footprint_is_checked_against_the_edge(self) -> None:
        """The edge is the footprint's only firewall (C900-PAR): it holds every rule and must be complete."""
        assert egress_findings(_cleaned([_dc([_edge()])], exchanges=[_exchange("eg")])) == []
        edge = _edge()
        edge["vlan_id"] = None
        findings = egress_findings(_cleaned([_dc([edge])], exchanges=[_exchange("eg")]))
        assert findings and all("egress context" in finding for finding in findings)

    def test_an_edge_only_colocation_footprint_needs_no_zone_firewall(self) -> None:
        cleaned = _colocation_cleaned([_edge()], zones=[], prefixes=("eg",))
        assert egress_findings(cleaned) == []

    def test_two_edge_contexts_are_an_error(self) -> None:
        exchanges = [_exchange("eg"), _exchange("eg2")]
        findings = egress_findings(_cleaned([_dc([_context(), _edge(), _edge("eg2")])], exchanges=exchanges))
        assert len(findings) == 1 and "2 Internet edge contexts" in findings[0]

    def test_an_edge_of_another_environment_is_an_error(self) -> None:
        non_prod = _exchange("eg")
        non_prod["vrf_a"] = make_vrf({"name": "NONPROD", "vrf_type": "non_prod"})
        findings = egress_findings(_cleaned([_dc([_context(), _edge()])], exchanges=[non_prod, _exchange()]))
        assert len(findings) == 1 and "is for non_prod, the footprint is prod" in findings[0]

    def test_an_edge_without_an_exchange_is_reported(self) -> None:
        findings = egress_findings(_cleaned([_dc([_context(), _edge()])], exchanges=[]))
        assert any("no TopologyRoutedExchange between the tenant VRF and INTERNET" in finding for finding in findings)
