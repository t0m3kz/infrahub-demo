"""CheckRoutingInvariants and the tunnel's ``registered_prefixes``: rules 6-9 (PROD, owners, conflicts, offices)."""

from __future__ import annotations

from typing import Any

from tests.integration.demo_data import relationship, relationship_names
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths
from tests.unit.test_routing_invariants_check import (
    _CUSTOMER,
    _PROVIDER,
    _block,
    _gateway_office,
    _onprem,
    _owner,
    _rule_eight,
    _run,
    _v,
)

_OTHER_CUSTOMER = ("OrganizationCustomer", "C001", "Nordix (C001)")
_ACME = "198.18.0.0/24"
_GW1 = "EQX-FR2-SDWAN-GW1"
_GW2 = "EQX-AM5-SDWAN-GW1"


def _tunnel(
    name: str,
    owner: tuple[str, str, str] | None,
    *prefixes: dict[str, Any],
    ends: tuple[str, ...] = (_GW1, "EXT-ACME-EDGE1"),
) -> dict[str, Any]:
    """A TopologyTunnelCircuit in the routing_invariants shape: owner, end-port devices, registered prefixes."""
    return {
        "id": f"tunnel-{name}",
        "name": _v(name),
        "owner": _owner(owner),
        "capabilities": {"edges": [{"node": {"device": {"node": {"name": _v(device)}}}} for device in ends]},
        "registered_prefixes": {"edges": [{"node": prefix} for prefix in prefixes]},
    }


def _lan_office(name: str, gateway: str | None, owner: tuple[str, str, str] | None, lan: str) -> dict[str, Any]:
    """An office behind ``gateway`` registering one PROD LAN."""
    return _gateway_office(name, gateway, owner, _block(lan))


def _errors_with(check: Any, text: str) -> list[str]:
    """The captured errors that mention ``text``."""
    return [message for message in check._captured_errors if text in message]


class TestTunnelPrefixesAreProd:
    """Rule 6: a tunnel is production, its registered prefixes sit in PROD and overlap nothing routed there."""

    def test_a_prod_prefix_of_a_customer_tunnel_passes(self) -> None:
        """The partner range in PROD, owned by a customer, behind a gateway with an office: nothing to report."""
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block(_ACME))],
            TopologyCustomerOffice=[_lan_office("o", _GW1, _CUSTOMER, "10.210.2.0/24")],
        )
        assert check._captured_errors == []

    def test_a_non_prod_prefix_is_an_error(self) -> None:
        """A tunnel registers PROD space; a NON-PROD prefix would never be accepted by the PROD session."""
        check = _run(TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block(_ACME, "NON-PROD", "non_prod"))])
        errors = _errors_with(check, "tunnel 'T' registered prefix")
        assert len(errors) == 1
        assert "NON-PROD" in errors[0]
        assert "always production" in errors[0]

    def test_a_prefix_overlapping_a_vpc_block_is_an_error(self) -> None:
        """Inside one namespace a tunnel's range overlaps no VPC block."""
        vpc = {
            "id": "vpc-v",
            "name": _v("v"),
            "account": {"node": {"id": "a", "name": _v("a"), "environment": _v("p"), "owner": _owner(_CUSTOMER)}},
            "cidr_blocks": {"edges": [{"node": _block("198.18.0.0/16")}]},
        }
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block(_ACME))],
            CloudVirtualNetwork=[vpc],
        )
        assert len(_errors_with(check, "overlaps")) == 1

    def test_a_prefix_overlapping_an_office_lan_is_an_error(self) -> None:
        """A tunnel range and an office LAN of the same namespace may not overlap."""
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block("10.210.0.0/16"))],
            TopologyCustomerOffice=[_lan_office("o", _GW1, _CUSTOMER, "10.210.2.0/24")],
        )
        assert len(_errors_with(check, "overlaps")) == 1

    def test_a_prefix_overlapping_an_onprem_prefix_is_an_error(self) -> None:
        """A tunnel range inside the on-prem space of its namespace is reported."""
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block("10.0.0.0/24"))],
            ManagedNetworkSegment=[_onprem("seg", "10.0.0.0/24", pid="other")],
        )
        assert len(_errors_with(check, "overlaps on-prem prefix")) == 1


class TestTunnelOwner:
    """Rule 7: a tunnel that registers prefixes needs a customer owner."""

    def test_a_provider_owned_tunnel_is_an_error(self) -> None:
        """A provider-owned tunnel's prefixes are never accepted or announced."""
        check = _run(TopologyTunnelCircuit=[_tunnel("T", _PROVIDER, _block(_ACME))])
        errors = _errors_with(check, "tunnel 'T' registers prefixes")
        assert len(errors) == 1
        assert "never accepted or announced" in errors[0]

    def test_an_ownerless_tunnel_is_an_error(self) -> None:
        """No owner is no customer."""
        check = _run(TopologyTunnelCircuit=[_tunnel("T", None, _block(_ACME))])
        assert len(_errors_with(check, "tunnel 'T' registers prefixes")) == 1

    def test_a_tunnel_without_prefixes_needs_no_owner(self) -> None:
        """An inventory tunnel (the office's own SD-WAN tunnel) that registers nothing is not held to the rule."""
        check = _run(TopologyTunnelCircuit=[_tunnel("T", None)])
        assert check._captured_errors == []


class TestTunnelOwnershipConflicts:
    """Rule 8: a tunnel's prefixes are registered behind the gateway at one of its ends."""

    def test_the_same_owner_behind_one_gateway_is_not_a_conflict(self) -> None:
        """An office LAN and a tunnel range of one owner behind one gateway: one attachment point."""
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block(_ACME))],
            TopologyCustomerOffice=[_lan_office("o", _GW1, _CUSTOMER, "10.210.2.0/24")],
        )
        assert _rule_eight(check) == []

    def test_a_prefix_of_two_owners_is_a_conflict(self) -> None:
        """A tunnel range another customer's office also registers would be announced across owners."""
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block(_ACME))],
            TopologyCustomerOffice=[_lan_office("o", _GW1, _OTHER_CUSTOMER, _ACME)],
        )
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert _ACME in errors[0]
        assert "more than one owner" in errors[0]
        assert "SD-WAN gateway" in errors[0]

    def test_a_prefix_behind_two_gateways_is_a_conflict(self) -> None:
        """One owner's range registered by tunnels at two gateways is reachable through two sessions."""
        check = _run(
            TopologyTunnelCircuit=[
                _tunnel("T1", _CUSTOMER, _block(_ACME), ends=(_GW1, "EXT-A")),
                _tunnel("T2", _CUSTOMER, _block(_ACME), ends=(_GW2, "EXT-B")),
            ],
            TopologyCustomerOffice=[
                _lan_office("o1", _GW1, _CUSTOMER, "10.210.2.0/24"),
                _lan_office("o2", _GW2, _CUSTOMER, "10.210.3.0/24"),
            ],
        )
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "more than one session" in errors[0]

    def test_a_tunnel_at_a_gateway_no_office_names_registers_nothing(self) -> None:
        """Without an office behind the gateway there is no session: the tunnel's range is attached nowhere."""
        check = _run(
            TopologyTunnelCircuit=[
                _tunnel("T1", _CUSTOMER, _block(_ACME), ends=(_GW1, "EXT-A")),
                _tunnel("T2", _CUSTOMER, _block(_ACME), ends=(_GW2, "EXT-B")),
            ],
            TopologyCustomerOffice=[_lan_office("o1", _GW1, _CUSTOMER, "10.210.2.0/24")],
        )
        assert _rule_eight(check) == []


class TestOfficesRegisterSomething:
    """Rule 9: an office behind an SD-WAN gateway registers a LAN or hosts a segment."""

    def test_an_office_without_a_prefix_is_an_error(self) -> None:
        """No lan_prefixes and no hosted segment: the gateway session accepts nothing of it."""
        check = _run(TopologyCustomerOffice=[_gateway_office("C001-OFFICE", _GW1, _CUSTOMER)])
        errors = _errors_with(check, "registers no prefix")
        assert len(errors) == 1
        assert "C001-OFFICE" in errors[0]
        assert _GW1 in errors[0]

    def test_an_office_with_a_lan_passes(self) -> None:
        """One lan_prefixes entry is enough."""
        check = _run(TopologyCustomerOffice=[_lan_office("o", _GW1, _CUSTOMER, "10.210.2.0/24")])
        assert _errors_with(check, "registers no prefix") == []

    def test_an_office_hosting_a_segment_passes(self) -> None:
        """A segment the office hosts registers its prefix (the office has no typed LAN)."""
        office = _gateway_office("o", _GW1, _CUSTOMER, hosted=(("10.176.5.0/24", _CUSTOMER),))
        assert _errors_with(_run(TopologyCustomerOffice=[office]), "registers no prefix") == []

    def test_an_office_without_a_gateway_is_not_held_to_it(self) -> None:
        """No sdwan_gateway: no SD-WAN session to feed."""
        check = _run(TopologyCustomerOffice=[_gateway_office("o", None, _CUSTOMER)])
        assert _errors_with(check, "registers no prefix") == []

    def test_a_tunnel_does_not_register_for_the_office(self) -> None:
        """The tunnel's range reaches the gateway, but the office itself still registers nothing."""
        check = _run(
            TopologyTunnelCircuit=[_tunnel("T", _CUSTOMER, _block(_ACME))],
            TopologyCustomerOffice=[_gateway_office("o", _GW1, _CUSTOMER)],
        )
        assert len(_errors_with(check, "registers no prefix")) == 1


def test_the_query_selects_what_the_tunnel_rules_read() -> None:
    """Owner, end-port devices and registered prefixes with their namespace, on real schema fields."""
    selected = _selected_paths(_QUERIES / "validation" / "routing_invariants.gql", "TopologyTunnelCircuit")
    reads = [
        "name",
        "owner.org_id",
        "capabilities.device.name",
        "registered_prefixes.prefix",
        "registered_prefixes.ip_namespace.name",
        "registered_prefixes.ip_namespace.namespace_type",
    ]
    assert not [item for item in reads if item not in selected]
    assert {"owner", "registered_prefixes", "capabilities"} <= relationship_names("TopologyTunnelCircuit")
    definition = relationship("TopologyTunnelCircuit", "registered_prefixes")
    assert definition is not None
    assert definition["peer"] == "IpamPrefix"
    assert definition["cardinality"] == "many"
    assert definition["optional"] is True
    assert len(definition["description"]) < 128
