"""Unit tests for checks/common.py's validate_routing_password(), validate_exchange_gateways()
and checks/border_leaf.py's validate_transit_vlans()."""

from checks.border_leaf import validate_transit_vlans
from checks.common import validate_exchange_gateways, validate_routing_password


def _bgp_capability(peerings: list[dict]) -> dict:
    return {"typename": "ManagedBGP", "peerings": peerings}


def _ospf_interface_capability(password: dict | None) -> dict:
    entry = {"typename": "RoutingOSPFInterface"}
    if password is not None:
        entry["password"] = password
    return entry


class TestValidateRoutingPasswordBgp:
    def test_peering_without_password_reports_error(self):
        data = {
            "capabilities": [_bgp_capability([{"name": "underlay--leaf-01--spine-01", "password": None}])],
        }
        errors = validate_routing_password(data)
        assert len(errors) == 1
        assert "underlay--leaf-01--spine-01" in errors[0]

    def test_peering_with_password_passes(self):
        data = {
            "capabilities": [
                _bgp_capability([{"name": "underlay--leaf-01--spine-01", "password": {"password": "s3cr3t"}}])
            ],
        }
        assert validate_routing_password(data) == []

    def test_missing_password_key_treated_as_unset(self):
        data = {
            "capabilities": [_bgp_capability([{"name": "overlay-evpn--leaf-01--spine-01"}])],
        }
        errors = validate_routing_password(data)
        assert len(errors) == 1

    def test_multiple_peerings_each_checked_independently(self):
        data = {
            "capabilities": [
                _bgp_capability(
                    [
                        {"name": "underlay-a", "password": {"password": "s3cr3t"}},
                        {"name": "underlay-b", "password": None},
                    ]
                )
            ],
        }
        errors = validate_routing_password(data)
        assert len(errors) == 1
        assert "underlay-b" in errors[0]

    def test_non_bgp_capability_ignored(self):
        data = {
            "capabilities": [{"typename": "ManagedNTP", "servers": []}],
        }
        assert validate_routing_password(data) == []

    def test_no_capabilities_key_returns_no_errors(self):
        assert validate_routing_password({}) == []


class TestValidateRoutingPasswordOspf:
    def test_interface_without_password_reports_error(self):
        data = {
            "interfaces": [
                {"name": "Ethernet1", "interface_capabilities": [_ospf_interface_capability(None)]},
            ],
        }
        errors = validate_routing_password(data)
        assert len(errors) == 1
        assert "Ethernet1" in errors[0]

    def test_interface_with_password_passes(self):
        data = {
            "interfaces": [
                {
                    "name": "Ethernet1",
                    "interface_capabilities": [_ospf_interface_capability({"password": "s3cr3t"})],
                },
            ],
        }
        assert validate_routing_password(data) == []

    def test_non_ospf_interface_capability_ignored(self):
        data = {
            "interfaces": [
                {"name": "Ethernet1", "interface_capabilities": [{"typename": "ManagedVlanSegment"}]},
            ],
        }
        assert validate_routing_password(data) == []

    def test_no_interfaces_key_returns_no_errors(self):
        assert validate_routing_password({}) == []


class TestValidateRoutingPasswordCombined:
    def test_bgp_and_ospf_errors_both_reported(self):
        data = {
            "capabilities": [_bgp_capability([{"name": "underlay-a", "password": None}])],
            "interfaces": [
                {"name": "Ethernet1", "interface_capabilities": [_ospf_interface_capability(None)]},
            ],
        }
        errors = validate_routing_password(data)
        assert len(errors) == 2


GATEWAY_ID = "ctx-1"
CONTEXT_VLAN = 3005


def _exchange(exchange_id: str = "xchg-1") -> dict:
    return {
        "typename": "TopologyRoutedExchange",
        "id": exchange_id,
        "name": "ctx-1-PROD-INTERNET",
        "gateway": {"id": GATEWAY_ID},
    }


def _context(context_id: str = GATEWAY_ID, vlan: int | None = CONTEXT_VLAN) -> dict:
    return {"typename": "ManagedFirewallContext", "id": context_id, "name": "ctx-1", "vlan_id": vlan}


_NS_TYPE = {"PROD": "prod", "NON-PROD": "non_prod", "INTERNET": "internet"}


def _fw_leg(
    name: str = "eth1.3005",
    address: str | None = "100.66.0.5/29",
    namespace: str | None = "PROD",
    ns_type: str | None = None,
    *,
    caps: list[dict] | None = None,
) -> dict:
    ip_address = None
    if address and namespace:
        ip_address = {
            "address": address,
            "ip_namespace": {"name": namespace, "namespace_type": ns_type or _NS_TYPE.get(namespace, "prod")},
        }
    return {
        "name": name,
        "ip_address": ip_address,
        "interface_capabilities": [_context(), _exchange()] if caps is None else caps,
    }


def _good_legs() -> list[dict]:
    # transit VLAN: PROD slot 0 -> 3005, INTERNET slot 2 -> 3405
    return [
        _fw_leg("eth1.3005", "100.66.0.5/29", "PROD"),
        _fw_leg("eth1.3405", "100.66.32.6/29", "INTERNET"),
    ]


def _validate(legs: list[dict]) -> list[str]:
    return validate_exchange_gateways({"name": "dc1-fw-01", "interfaces": legs})


class TestValidateExchangeGateways:
    def test_no_interfaces_key_returns_no_errors(self):
        assert validate_exchange_gateways({}) == []

    def test_non_exchange_capability_ignored(self):
        data = {"interfaces": [{"name": "Vlan10", "interface_capabilities": [{"typename": "ManagedVxlanSegment"}]}]}
        assert validate_exchange_gateways(data) == []

    def test_one_leg_per_namespace_passes(self):
        assert _validate(_good_legs()) == []

    def test_missing_namespace_z_leg_reports_error(self):
        errors = _validate(_good_legs()[:1])
        assert len(errors) == 1
        assert "legs in 1 namespace(s) ['PROD'] — exactly 2" in errors[0]

    def test_two_legs_in_one_namespace_reports_error(self):
        legs = [*_good_legs(), _fw_leg("eth2.3005", "100.66.0.6/29", "PROD")]
        errors = _validate(legs)
        assert len(errors) == 1
        assert "has 2 legs in namespace 'PROD'" in errors[0]

    def test_leg_without_address_reports_error(self):
        errors = _validate([*_good_legs()[:1], _fw_leg("eth1.3405", None, None)])
        assert any("no IP address / namespace" in e for e in errors)

    def test_prod_and_non_prod_are_never_exchanged(self):
        errors = _validate([*_good_legs()[:1], _fw_leg("eth1.3205", "100.66.16.6/29", "NON-PROD")])
        assert any("not an allowed pair" in e for e in errors)

    def test_three_namespaces_in_one_exchange_report_error(self):
        errors = _validate([*_good_legs(), _fw_leg("eth1.3205", "100.66.16.6/29", "NON-PROD")])
        assert any("exactly 2" in e for e in errors)

    def test_address_not_a_slash_29_reports_error(self):
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.5/30", "PROD")
        errors = _validate(legs)
        assert len(errors) == 1
        assert "not inside a /29" in errors[0]

    def test_address_not_a_member_offset_reports_error(self):
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.4/29", "PROD")  # the VIP, not a member
        errors = _validate(legs)
        assert len(errors) == 1
        assert "not a firewall member address" in errors[0]

    def test_wrong_vlan_reports_error(self):
        legs = _good_legs()
        legs[1] = _fw_leg("eth1.3005", "100.66.32.6/29", "INTERNET")  # context VLAN, not the INTERNET transit
        errors = _validate(legs)
        assert len(errors) == 1
        assert "expected 3405" in errors[0]

    def test_non_numeric_vlan_suffix_reports_error(self):
        legs = _good_legs()
        legs[0] = _fw_leg("eth1", "100.66.0.5/29", "PROD")
        assert any("expected 3005" in e for e in _validate(legs))

    def test_leg_without_gateway_context_reports_error(self):
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.5/29", "PROD", caps=[_exchange()])
        errors = _validate(legs)
        assert len(errors) == 1
        assert "not tagged with the exchange's gateway context" in errors[0]

    def test_leg_tagged_with_other_context_reports_error(self):
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.5/29", "PROD", caps=[_context("ctx-other"), _exchange()])
        assert any("gateway context" in e for e in _validate(legs))

    def test_context_without_vlan_reports_error(self):
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.5/29", "PROD", caps=[_context(vlan=None), _exchange()])
        assert any("cannot derive its transit VLAN" in e for e in _validate(legs))

    def test_two_exchanges_validated_independently(self):
        other = _exchange("xchg-2")
        other["name"] = "ctx-1-NON-PROD-INTERNET"
        legs = [
            *_good_legs(),
            _fw_leg("eth1.3205", "100.66.16.5/29", "NON-PROD", caps=[_context(), other]),
        ]
        errors = _validate(legs)
        assert len(errors) == 1
        assert "ctx-1-NON-PROD-INTERNET" in errors[0]
        assert "legs in 1 namespace(s) ['NON-PROD']" in errors[0]

    def test_device_name_included_in_error_message(self):
        assert "dc1-fw-01" in _validate(_good_legs()[:1])[0]


def _segment_port(vlan: int | None, seg_id: str = "seg-1", name: str = "web") -> dict:
    return {
        "name": "Ethernet1",
        "role": "customer",
        "interface_capabilities": [
            {"typename": "ManagedVlanSegment", "id": seg_id, "name": name, "status": "active", "vlan_id": vlan}
        ],
    }


def _service_port() -> dict:
    context = {
        **_context(),
        "interface_capabilities": [
            {
                "device": {"role": "firewall"},
                "ip_address": {
                    "address": "100.66.0.5/29",
                    "ip_namespace": {"name": "PROD", "namespace_type": "prod", "l3_vni": 50001},
                },
            }
        ],
    }
    return {"name": "Ethernet49", "role": "firewall", "interface_capabilities": [context]}


class TestValidateTransitVlans:
    def test_no_transits_returns_no_errors(self):
        assert validate_transit_vlans({"name": "bl-1", "interfaces": [_segment_port(3005)]}) == []

    def test_distinct_vlans_pass(self):
        device = {"name": "bl-1", "interfaces": [_segment_port(100), _service_port()]}
        assert validate_transit_vlans(device) == []

    def test_collision_with_segment_vlan_reports_error(self):
        device = {"name": "bl-1", "interfaces": [_segment_port(3005), _service_port()]}
        errors = validate_transit_vlans(device)
        assert len(errors) == 1
        assert "VLAN 3005" in errors[0]
        assert "'web'" in errors[0]
        assert "bl-1" in errors[0]
