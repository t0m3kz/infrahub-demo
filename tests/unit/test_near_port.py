"""The pure near-port choice of add_provider_connection (generators/helpers/near_port.py).

A port is free when nothing sits on it; the device with the fewest connections is tried first (ties by name) and its
first free port by natural name; an explicit near_port is never replaced; an existing carrier on the chosen port is
reused when it is live, reaches the far facility and belongs to no other connection.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.near_port import (
    CarrierFacts,
    NearPortError,
    PortFacts,
    carrier_type_for,
    choose_far_port,
    choose_near_port,
    connection_load,
    find_reusable_carrier,
    natural_key,
    near_role,
)


def _port(device: str, name: str, **kwargs: Any) -> PortFacts:
    return PortFacts(id=f"{device}/{name}", name=name, device_id=f"id-{device}", device_name=device, **kwargs)


class TestFreePort:
    @pytest.mark.parametrize(
        ("busy", "reason"),
        [
            ({"cabled": True}, "cabled"),
            ({"foreign_carriers": 1}, "carrier"),
            ({"foreign_sub_interfaces": 2}, "sub-interfaces"),
            ({"lag_member": True}, "LAG"),
            ({"foreign_sessions": 1}, "session"),
        ],
    )
    def test_a_busy_port_is_never_chosen_and_the_next_one_is(self, busy: dict[str, Any], reason: str) -> None:
        taken = _port("eg-1", "Ethernet1/1", **busy)
        free = _port("eg-1", "Ethernet1/2")

        assert reason in " ".join(taken.busy_reasons)
        assert choose_near_port([taken, free], {}) == free

    def test_no_free_port_is_an_error_naming_every_port_and_why(self) -> None:
        ports = [_port("eg-1", "Ethernet1/1", cabled=True), _port("eg-2", "Ethernet1/1", lag_member=True)]

        with pytest.raises(NearPortError, match=r"eg-1/Ethernet1/1: cabled; eg-2/Ethernet1/1: member of a LAG"):
            choose_near_port(ports, {})

    def test_no_candidate_at_all_says_that_none_has_the_role(self) -> None:
        with pytest.raises(NearPortError, match="none has it"):
            choose_near_port([], {})


class TestChoice:
    def test_the_least_loaded_device_wins(self) -> None:
        ports = [_port("eg-1", "Ethernet1/1"), _port("eg-2", "Ethernet1/1")]

        assert choose_near_port(ports, {"id-eg-1": 2, "id-eg-2": 1}).device_name == "eg-2"

    def test_a_tie_goes_to_the_first_device_by_name(self) -> None:
        ports = [_port("eg-2", "Ethernet1/1"), _port("eg-1", "Ethernet1/1")]

        assert choose_near_port(ports, {}).device_name == "eg-1"

    def test_the_first_free_port_is_taken_in_natural_order(self) -> None:
        ports = [_port("eg-1", "Ethernet1/10"), _port("eg-1", "Ethernet1/2"), _port("eg-1", "Ethernet1/1", cabled=True)]

        assert choose_near_port(ports, {}).name == "Ethernet1/2"

    def test_natural_order_puts_2_before_10(self) -> None:
        assert sorted(["Ethernet1/10", "Ethernet1/2", "Ethernet1/1"], key=natural_key) == [
            "Ethernet1/1",
            "Ethernet1/2",
            "Ethernet1/10",
        ]

    def test_a_device_without_a_free_port_hands_over_to_the_next(self) -> None:
        ports = [_port("eg-1", "Ethernet1/1", cabled=True), _port("eg-2", "Ethernet1/1")]

        assert choose_near_port(ports, {"id-eg-2": 5}).device_name == "eg-2"

    def test_the_load_counts_a_device_once_per_connection(self) -> None:
        assert connection_load([["a", "a", "b"], ["a"]]) == {"a": 2, "b": 1}


class TestExplicitPort:
    def test_it_wins_over_a_less_loaded_free_candidate(self) -> None:
        explicit = _port("eg-2", "Ethernet1/9", cabled=True)

        chosen = choose_near_port([_port("eg-1", "Ethernet1/1")], {"id-eg-2": 9}, explicit=explicit)

        assert chosen == explicit  # a cable to the provider's own port is no conflict for an operator's choice

    @pytest.mark.parametrize("conflict", [{"lag_member": True}, {"foreign_sub_interfaces": 1}, {"foreign_sessions": 1}])
    def test_a_conflict_is_an_error_never_another_port(self, conflict: dict[str, Any]) -> None:
        with pytest.raises(NearPortError, match="near_port eg-1/Ethernet1/1 cannot be used"):
            choose_near_port([_port("eg-1", "Ethernet1/2")], {}, explicit=_port("eg-1", "Ethernet1/1", **conflict))


class TestRoleAndType:
    @pytest.mark.parametrize(
        ("far_kind", "role"),
        [
            ("TopologyCloudRegion", "cloud"),
            ("TopologySaasRegion", "saas"),
            ("TopologyExternalProviders", "partner"),
            ("TopologyColocationZone", "partner"),
            (None, "partner"),
        ],
    )
    def test_the_role_follows_the_far_kind(self, far_kind: str | None, role: str) -> None:
        assert near_role(far_kind) == role

    @pytest.mark.parametrize(
        ("far_kind", "carrier_type"),
        [
            ("TopologyCloudRegion", "virtual_connection"),
            ("TopologySaasRegion", "virtual_connection"),
            ("TopologyVirtualColocation", "virtual_connection"),
            ("TopologyExternalProviders", "cross_connect"),
            ("TopologyColocationZone", "cross_connect"),
        ],
    )
    def test_the_carrier_type_follows_the_far_kind(self, far_kind: str, carrier_type: str) -> None:
        assert carrier_type_for(far_kind) == carrier_type


def _carrier(name: str, **kwargs: Any) -> CarrierFacts:
    defaults: dict[str, Any] = {
        "id": f"id-{name}",
        "name": name,
        "circuit_type": "cross_connect",
        "live": True,
        "far_end_deployments": frozenset({"far"}),
    }
    return CarrierFacts(**{**defaults, **kwargs})


class TestReuse:
    def test_a_live_carrier_to_the_far_facility_is_reused(self) -> None:
        carrier = _carrier("XC-1")

        assert find_reusable_carrier([carrier], far_facility_id="far", connection_id="c1") == carrier

    def test_nothing_on_the_port_means_create(self) -> None:
        assert find_reusable_carrier([], far_facility_id="far", connection_id="c1") is None

    def test_a_carrier_claimed_by_another_connection_is_not_reused(self) -> None:
        carrier = _carrier("XC-1", claimed_by=frozenset({"c2"}))

        assert find_reusable_carrier([carrier], far_facility_id="far", connection_id="c1") is None

    def test_a_carrier_claimed_by_this_connection_is_its_own(self) -> None:
        carrier = _carrier("XC-1", claimed_by=frozenset({"c1"}))

        assert find_reusable_carrier([carrier], far_facility_id="far", connection_id="c1") == carrier

    def test_a_decommissioned_carrier_and_one_to_another_facility_are_not_reused(self) -> None:
        carriers = [_carrier("XC-1", live=False), _carrier("XC-2", far_end_deployments=frozenset({"elsewhere"}))]

        assert find_reusable_carrier(carriers, far_facility_id="far", connection_id="c1") is None

    def test_two_fitting_carriers_are_ambiguous(self) -> None:
        with pytest.raises(NearPortError, match="XC-1, XC-2"):
            find_reusable_carrier([_carrier("XC-2"), _carrier("XC-1")], far_facility_id="far", connection_id="c1")


class TestFarPort:
    def test_the_first_free_port_by_name(self) -> None:
        ports = [_port("ext", "et-0/0/10"), _port("ext", "et-0/0/2"), _port("ext", "et-0/0/1", cabled=True)]

        assert choose_far_port(ports).name == "et-0/0/2"

    def test_no_free_port_is_an_error(self) -> None:
        with pytest.raises(NearPortError, match="no free port"):
            choose_far_port([_port("ext", "et-0/0/1", lag_member=True)])
