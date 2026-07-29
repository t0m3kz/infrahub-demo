"""Unit tests for checks/common.py's validate_routing_password() and validate_exchange_gateways()."""

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


def _routed_exchange_capability(
    *,
    exchange_id: str = "exchange-1",
    name: str = "c001-c002-shared-services",
    namespace_a: str | None = "VRF-A",
    namespace_z: str | None = "VRF-Z",
    legs: list[dict] | None = None,
) -> dict:
    cap: dict = {"typename": "TopologyRoutedExchange", "id": exchange_id, "name": name}
    if namespace_a is not None:
        cap["namespace_a"] = {"name": namespace_a}
    if namespace_z is not None:
        cap["namespace_z"] = {"name": namespace_z}
    cap["interface_capabilities"] = legs if legs is not None else []
    return cap


def _leg(ns_name: str | None) -> dict:
    if ns_name is None:
        return {"ip_address": None}
    return {"ip_address": {"address": "10.0.0.1/30", "ip_namespace": {"name": ns_name}}}


class TestValidateExchangeGateways:
    def test_no_interfaces_key_returns_no_errors(self):
        assert validate_exchange_gateways({}) == []

    def test_non_exchange_capability_ignored(self):
        data = {"interfaces": [{"name": "Vlan10", "interface_capabilities": [{"typename": "ManagedVxlanSegment"}]}]}
        assert validate_exchange_gateways(data) == []

    def test_two_legs_in_matching_distinct_namespaces_passes(self):
        cap = _routed_exchange_capability(legs=[_leg("VRF-A"), _leg("VRF-Z")])
        data = {"name": "border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        assert validate_exchange_gateways(data) == []

    def test_wrong_leg_count_one_leg_reports_error(self):
        cap = _routed_exchange_capability(legs=[_leg("VRF-A")])
        data = {"name": "border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        errors = validate_exchange_gateways(data)
        assert len(errors) == 1
        assert "1 interface(s)" in errors[0]

    def test_wrong_leg_count_three_legs_reports_error(self):
        cap = _routed_exchange_capability(legs=[_leg("VRF-A"), _leg("VRF-Z"), _leg("VRF-Q")])
        data = {"name": "border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        errors = validate_exchange_gateways(data)
        assert len(errors) == 1
        assert "3 interface(s)" in errors[0]

    def test_leg_missing_namespace_reports_error(self):
        cap = _routed_exchange_capability(legs=[_leg("VRF-A"), _leg(None)])
        data = {"name": "border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        errors = validate_exchange_gateways(data)
        assert len(errors) == 1
        assert "no IP address / namespace assigned" in errors[0]

    def test_both_legs_same_namespace_reports_error(self):
        cap = _routed_exchange_capability(legs=[_leg("VRF-A"), _leg("VRF-A")])
        data = {"name": "border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        errors = validate_exchange_gateways(data)
        assert len(errors) == 1
        assert "same namespace" in errors[0]

    def test_leg_namespaces_mismatch_referenced_namespaces_reports_error(self):
        """Legs are in two distinct namespaces, but they don't match namespace_a/namespace_z."""
        cap = _routed_exchange_capability(
            namespace_a="VRF-A", namespace_z="VRF-Z", legs=[_leg("VRF-A"), _leg("VRF-OTHER")]
        )
        data = {"name": "border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        errors = validate_exchange_gateways(data)
        assert len(errors) == 1
        assert "do not match" in errors[0]

    def test_same_exchange_seen_on_two_interfaces_dedup_by_id(self):
        cap = _routed_exchange_capability(exchange_id="exchange-1", legs=[_leg("VRF-A"), _leg("VRF-Z")])
        data = {
            "name": "border-leaf-01",
            "interfaces": [
                {"name": "Vlan10", "interface_capabilities": [cap]},
                {"name": "Vlan20", "interface_capabilities": [cap]},
            ],
        }
        assert validate_exchange_gateways(data) == []

    def test_device_name_included_in_error_message(self):
        cap = _routed_exchange_capability(legs=[_leg("VRF-A")])
        data = {"name": "dc1-border-leaf-01", "interfaces": [{"name": "Vlan10", "interface_capabilities": [cap]}]}
        errors = validate_exchange_gateways(data)
        assert "dc1-border-leaf-01" in errors[0]
