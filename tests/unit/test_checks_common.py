"""Unit tests for checks/common.py's validate_routing_password(), validate_exchange_gateways()
and validate_transit_vlans() for both the border leaf and the edge."""

from functools import partial
from typing import Any

import pytest

from checks.border_leaf import CheckBorderLeaf
from checks.border_spine import CheckBorderSpine
from checks.common import validate_exchange_gateways, validate_routing_password, validate_snat_addresses
from checks.common import validate_transit_vlans as _validate_transit_vlans
from checks.edge import CheckEdge
from tests.unit.encapsulation_helpers import cleaned_encapsulation
from tests.unit.routing_helpers import in_vrf, make_vrf
from tests.unit.service_contexts_helpers import make_far, make_iface, make_member, make_switch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.edge import Edge


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
            "capabilities": [{"typename": "ManagedMLAG"}],
        }
        assert validate_routing_password(data) == []

    def test_no_capabilities_key_returns_no_errors(self):
        assert validate_routing_password({}) == []


class TestValidateRoutingPasswordOspf:
    def test_interface_without_password_reports_error(self):
        data = {
            "interfaces": [
                {"name": "Ethernet1", "capabilities": [_ospf_interface_capability(None)]},
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
                    "capabilities": [_ospf_interface_capability({"password": "s3cr3t"})],
                },
            ],
        }
        assert validate_routing_password(data) == []

    def test_non_ospf_interface_capability_ignored(self):
        data = {
            "interfaces": [
                {"name": "Ethernet1", "capabilities": [{"typename": "ManagedVxlanSegment"}]},
            ],
        }
        assert validate_routing_password(data) == []


_FROM_NAME: Any = object()  # a leg's tag defaults to the number its test name ends in
GATEWAY_ID = "ctx-1"
CONTEXT_VLAN = 3005


_NS_TYPE = {"PROD": "prod", "NONPROD": "non_prod", "INTERNET": "internet", "MANAGEMENT": "management"}


def _vrf(namespace: str) -> dict:
    """The VRF instance of ``namespace`` (the instance a leg in it is in, ``_fw_leg``)."""
    return make_vrf({"name": namespace, "vrf_type": _NS_TYPE[namespace]})


def _exchange(a: str = "PROD", z: str = "INTERNET", exchange_id: str = "xchg-1") -> dict:
    """An exchange of the context between the VRFs of ``a`` and ``z``, as the context lists it (``exchanges``)."""
    return {"id": exchange_id, "name": f"ctx-1-{a}-{z}", "vrf_a": _vrf(a), "vrf_z": _vrf(z)}


def _context(
    context_id: str = GATEWAY_ID, vlan: int | None = CONTEXT_VLAN, exchanges: list[dict] | None = None
) -> dict:
    return {
        "typename": "ManagedFirewallContext",
        "id": context_id,
        "name": "ctx-1",
        "vlan_id": vlan,
        "exchanges": [_exchange()] if exchanges is None else exchanges,
    }


def _fw_leg(
    name: str = "eth1.3005",
    address: str | None = "100.66.0.5/29",
    namespace: str | None = "PROD",
    ns_type: str | None = None,
    *,
    vlan_id: int | None = _FROM_NAME,
) -> dict:
    """A firewall leg; its tag is its ``vlan_id`` (by default the number its name ends in)."""
    if vlan_id is _FROM_NAME:
        suffix = name.rsplit(".", 1)[-1]
        vlan_id = int(suffix) if "." in name and suffix.isdigit() else None
    ip_address = None
    if address and namespace:
        ip_address = {
            "address": address,
            "ip_namespace": {"node": {"name": {"value": "default"}}},
        }
    return {
        "name": name,
        **cleaned_encapsulation(vlan_id, {"name": "eth1", "device": {"id": "dev-fw-01"}}),
        "device": {"id": "dev-fw-01"},
        "ip_address": ip_address,
        **(in_vrf(make_vrf({"name": namespace, "vrf_type": ns_type or _NS_TYPE.get(namespace)})) if namespace else {}),
    }


def _good_legs() -> list[dict]:
    # transit VLAN: PROD slot 0 -> 3005, INTERNET slot 2 -> 3405
    return [
        _fw_leg("eth1.3005", "100.66.0.5/29", "PROD"),
        _fw_leg("eth1.3405", "100.66.32.6/29", "INTERNET"),
    ]


def _firewall(legs: list[dict], contexts: list[dict] | None = None) -> dict:
    """The firewall: member of the contexts, its uplink ``eth1`` and the leg sub-interfaces."""
    uplink = {"typename": "DcimPhysicalInterface", "name": "eth1", "role": "uplink", "device": {"id": "dev-fw-01"}}
    return {
        "id": "dev-fw-01",
        "name": "dc1-fw-01",
        "role": "internal-firewall",
        "shared_capabilities": [_context()] if contexts is None else contexts,
        "interfaces": [uplink, *({"typename": "DcimVirtualInterface", "role": "service", **leg} for leg in legs)],
    }


def _validate(legs: list[dict], contexts: list[dict] | None = None) -> list[str]:
    return validate_exchange_gateways(_firewall(legs, contexts))


class TestValidateExchangeGateways:
    def test_no_interfaces_key_returns_no_errors(self) -> None:
        """A device with no data has no exchange to judge."""
        assert validate_exchange_gateways({}) == []

    def test_a_device_in_no_context_has_no_exchange(self) -> None:
        """The exchanges are the contexts' (``exchanges``): an interface capability names none."""
        data = {"interfaces": [{"name": "Vlan10", "capabilities": [{"typename": "ManagedSegmentDeployment"}]}]}
        assert validate_exchange_gateways(data) == []

    def test_a_context_without_exchanges_passes(self) -> None:
        """A context with legs but no exchange (a load-balancer-like tenant-only context) has nothing to judge."""
        assert _validate(_good_legs(), contexts=[_context(exchanges=[])]) == []

    def test_one_leg_per_namespace_passes(self) -> None:
        """The context's PROD and INTERNET legs are the exchange's legs: derived, never linked."""
        assert _validate(_good_legs()) == []

    def test_missing_namespace_z_leg_reports_error(self) -> None:
        """No leg in the exchange's second VRF."""
        errors = _validate(_good_legs()[:1])
        assert len(errors) == 1
        assert "legs in 1 namespace(s) ['PROD'] — exactly 2" in errors[0]

    def test_two_legs_in_one_namespace_reports_error(self) -> None:
        """A leg on the INTERNET slot that is in the PROD VRF is a second PROD leg (and on the wrong VLAN)."""
        legs = [*_good_legs()[:1], _fw_leg("eth1.3405", "100.66.0.6/29", "PROD")]
        errors = _validate(legs)
        assert any("has 2 legs in namespace 'PROD'" in e for e in errors)

    def test_leg_without_address_reports_error(self) -> None:
        """A leg in its VRF with no address."""
        errors = _validate([*_good_legs()[:1], _fw_leg("eth1.3405", None, "INTERNET")])
        assert any("no IP address / namespace" in e for e in errors)

    def test_prod_and_non_prod_are_never_exchanged(self) -> None:
        """An exchange between the two tenant VRFs is refused."""
        legs = [*_good_legs()[:1], _fw_leg("eth1.3205", "100.66.16.6/29", "NONPROD")]
        errors = _validate(legs, contexts=[_context(exchanges=[_exchange("PROD", "NONPROD")])])
        assert any("not an allowed pair" in e for e in errors)

    def test_a_leg_in_another_vrf_is_no_leg_of_the_exchange(self) -> None:
        """The context's NONPROD leg is not a leg of its PROD-INTERNET exchange."""
        assert _validate([*_good_legs(), _fw_leg("eth1.3205", "100.66.16.6/29", "NONPROD")]) == []

    def test_address_not_a_slash_29_reports_error(self) -> None:
        """A leg is addressed in the transit /29."""
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.5/30", "PROD")
        errors = _validate(legs)
        assert len(errors) == 1
        assert "not inside a /29" in errors[0]

    def test_address_not_a_member_offset_reports_error(self) -> None:
        """A leg holds a member address, never the VIP."""
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.4/29", "PROD")  # the VIP, not a member
        errors = _validate(legs)
        assert len(errors) == 1
        assert "not a firewall member address" in errors[0]

    def test_wrong_vlan_reports_error(self) -> None:
        """An INTERNET leg on the NON-PROD slot of the context."""
        legs = _good_legs()
        legs[1] = _fw_leg("eth1.3205", "100.66.32.6/29", "INTERNET")
        errors = _validate(legs)
        assert len(errors) == 1
        assert "expected 3405" in errors[0]

    def test_an_untagged_leg_is_no_leg(self) -> None:
        """A sub-interface with no tag is no leg of the context, so the exchange misses its PROD leg."""
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.web", "100.66.0.5/29", "PROD")
        assert any("legs in 1 namespace(s) ['INTERNET']" in e for e in _validate(legs))

    def test_the_tag_is_the_field_not_the_name(self) -> None:
        """A name that says 3005 over another tag is no leg of the context; a different name over the right tag is."""
        legs = _good_legs()
        legs[0] = _fw_leg("eth1.3005", "100.66.0.5/29", "PROD", vlan_id=3999)
        assert any("legs in 1 namespace(s) ['INTERNET']" in e for e in _validate(legs))
        legs[0] = _fw_leg("eth1.legacy", "100.66.0.5/29", "PROD", vlan_id=3005)
        assert _validate(legs) == []

    def test_context_without_vlan_has_no_legs(self) -> None:
        """No VLAN, no `<uplink>.<VLAN>` leg: the exchange has none."""
        errors = _validate(_good_legs(), contexts=[_context(vlan=None)])
        assert any("legs in 0 namespace(s)" in e for e in errors)

    def test_two_exchanges_validated_independently(self) -> None:
        """Each exchange of the context is judged on its own legs."""
        exchanges = [_exchange(), _exchange("PROD", "MANAGEMENT", "xchg-2")]
        errors = _validate(_good_legs(), contexts=[_context(exchanges=exchanges)])
        assert len(errors) == 1
        assert "ctx-1-PROD-MANAGEMENT" in errors[0]
        assert "legs in 1 namespace(s) ['PROD']" in errors[0]

    def test_device_name_included_in_error_message(self) -> None:
        """The finding names the device."""
        assert "dc1-fw-01" in _validate(_good_legs()[:1])[0]


def _segment_port(vlan: int, seg_id: str = "seg-1", name: str = "web") -> dict:
    """A customer port of a VXLAN segment whose row for the device (``bl-1``) is ``vlan``."""
    row = {"vlan_id": vlan, "device": {"id": "bl-1"}}
    return {
        "name": "Ethernet1",
        "role": "customer",
        "capabilities": [
            {
                "typename": "ManagedSegmentVlan",
                "segment_deployment": {
                    "segment": {
                        "typename": "ManagedVxlanSegment",
                        "id": seg_id,
                        "name": name,
                        "status": "active",
                        "segment_deployments": [{"vni": 10100, "vlans": [row]}],
                    }
                },
            }
        ],
    }


def _service_ports() -> list[dict]:
    """The border service port cabled to the uplink of a firewall member of the context, whose PROD leg is addressed."""
    member = make_member("fw-1", [{**_context(), "name": "ctx-1"}], subs=("ethernet1/1.3005",))
    leg = member["interfaces"][-1]
    leg["ip_address"] = {"address": "100.66.0.5/29", "ip_namespace": {"name": "default"}}
    leg.update(in_vrf(make_vrf({"name": "PROD", "vrf_type": "prod", "l3_vni": 50001})))
    uplink = make_iface("fw-1:ethernet1/1", "ethernet1/1", "uplink")
    return make_switch({"Ethernet49": ("firewall", [make_far(member, uplink)])})["service_ports"]


@pytest.fixture(params=[BorderLeaf, BorderSpine, Edge], ids=["border-leaf", "border-spine", "edge"])
def validate_transit_vlans(request: pytest.FixtureRequest):
    """validate_transit_vlans bound to each border device's own transform class."""
    return lambda device: _validate_transit_vlans(device, transform_cls=request.param)


class TestValidateTransitVlans:
    def test_no_transits_returns_no_errors(self, validate_transit_vlans):
        assert validate_transit_vlans({"id": "bl-1", "name": "bl-1", "interfaces": [_segment_port(3005)]}) == []

    def test_distinct_vlans_pass(self, validate_transit_vlans):
        device = {"id": "bl-1", "name": "bl-1", "interfaces": [_segment_port(100)], "service_ports": _service_ports()}
        assert validate_transit_vlans(device) == []

    def test_collision_with_segment_vlan_reports_error(self, validate_transit_vlans):
        device = {"id": "bl-1", "name": "bl-1", "interfaces": [_segment_port(3005)], "service_ports": _service_ports()}
        errors = validate_transit_vlans(device)
        assert len(errors) == 1
        assert "VLAN 3005" in errors[0]
        assert "'web'" in errors[0]
        assert "bl-1" in errors[0]

    def test_both_checks_run_the_validator(self):
        """CheckBorderLeaf, CheckBorderSpine and CheckEdge carry a transit-VLAN validator bound to their transform."""
        for check, transform_cls in ((CheckBorderLeaf, BorderLeaf), (CheckBorderSpine, BorderSpine), (CheckEdge, Edge)):
            bound = [v for v in check.validators if isinstance(v, partial) and v.func is _validate_transit_vlans]
            assert len(bound) == 1
            assert bound[0].keywords == {"transform_cls": transform_cls}


def _snat_leg(snat: str | None, ns_type: str = "internet") -> dict:
    context = {**_context(), "snat_address": {"address": snat} if snat else None}
    leg = _fw_leg("eth1.3405", "100.66.32.5/29", "INTERNET", ns_type)
    return _firewall([leg], contexts=[context])


class TestValidateSnatAddresses:
    def test_public_snat_passes(self):
        assert validate_snat_addresses(_snat_leg("192.0.2.5/32")) == []

    def test_missing_snat_reports_error(self):
        errors = validate_snat_addresses(_snat_leg(None))
        assert len(errors) == 1
        assert "no snat_address" in errors[0]

    @pytest.mark.parametrize(
        "address",
        [
            "10.1.1.1/32",
            "100.66.32.9/32",
            "100.64.0.1/32",
            "198.18.0.1/32",
            "198.19.255.1/32",
            "172.16.0.1/32",
            "169.254.1.1/32",
        ],
    )
    def test_non_public_snat_reports_error(self, address: str):
        errors = validate_snat_addresses(_snat_leg(address))
        assert len(errors) == 1
        assert "not a public address" in errors[0]

    def test_context_without_an_internet_leg_needs_no_snat(self):
        assert validate_snat_addresses(_snat_leg(None, ns_type="prod")) == []

    def test_context_seen_on_two_legs_is_reported_once(self):
        data = _snat_leg(None)
        data["interfaces"].append(data["interfaces"][-1])
        assert len(validate_snat_addresses(data)) == 1
