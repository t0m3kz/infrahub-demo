"""Unit tests for checks/common.py's validate_routing_password()."""

from checks.common import validate_routing_password


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
