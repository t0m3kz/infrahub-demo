"""The pure planner of add_provider_connection (generators/helpers/provider_connection.py).

It reads the cleaned query payload, picks the flavour of the provider and the ends of each link, and raises
ValueError for everything that cannot be built, so the generator refuses a spec before its first write.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.provider_connection import (
    ExistingSession,
    SessionEnd,
    choose_tunnel_name,
    choose_vlan,
    existing_vlan,
    pair_link_prefixes,
    parse_connection,
    plan_links,
    session_is_current,
    stubs_to_build,
    tunnel_names,
)
from tests.unit import provider_connection_helpers as h
from tests.unit.provider_connection_helpers import dual_link_profile  # noqa: F401
from utils.provider_services import ConnectionFlavour, flavour_for


def _spec(**kwargs: Any):  # noqa: ANN202
    """The parsed spec of a connection built with the helpers' keyword arguments."""
    return parse_connection(h.connection(**kwargs)["TopologyProviderConnection"][0])


pytestmark = pytest.mark.usefixtures("dual_link_profile")


class TestDedicated:
    def test_one_link_rides_the_carrier_ports(self) -> None:
        """The aws dedicated flavour has one link: the near and far ends of the carrier, the stub is the far device."""
        flavour, links = plan_links(_spec())

        assert flavour == flavour_for("aws", "dedicated")
        assert [link.name for link in links] == ["AWS-FR2-L1"]
        link = links[0]
        assert link.circuit_type == "handoff"
        assert link.near.port is not None and link.near.port.id == "near-port-1"
        assert link.far.port is not None and link.far.port.id == "far-port-1"
        assert link.far.device_name == "EXT-STUB-1"
        assert link.far_process_id == "bgp-stub"
        assert link.link_prefix_id is None

    def test_a_decommissioned_carrier_does_not_count(self) -> None:
        live = h.aws_cross_connect(1)
        dead = h.aws_cross_connect(2)
        dead["status"] = "decommissioned"

        _, links = plan_links(_spec(carriers=[live, dead]))

        assert [link.near.port.id for link in links if link.near.port] == ["near-port-1"]

    def test_the_flavour_links_count_the_ports_per_side(self) -> None:
        """A two-link flavour: one carrier cannot serve both."""
        prefixes = [h.prefix("p-b", "10.9.0.4/30"), h.prefix("p-a", "10.9.0.0/30")]

        with pytest.raises(ValueError, match="2 link"):
            plan_links(_spec(profile="dual", link_prefixes=prefixes))

        _, links = plan_links(
            _spec(profile="dual", link_prefixes=prefixes, carriers=[h.aws_cross_connect(1), h.aws_cross_connect(2)])
        )
        assert [link.name for link in links] == ["AWS-FR2-L1", "AWS-FR2-L2"]
        assert [link.near.port.id for link in links if link.near.port] == ["near-port-1", "near-port-2"]
        assert [link.far.port.id for link in links if link.far.port] == ["far-port-1", "far-port-2"]
        # the ranges are paired in address order onto the links
        assert [link.link_prefix_id for link in links] == ["p-a", "p-b"]

    def test_no_carrier_is_refused(self) -> None:
        with pytest.raises(ValueError, match="carrier_circuits"):
            plan_links(_spec(carriers=[]))

    def test_a_stub_without_its_bgp_process_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no ManagedBGP process"):
            plan_links(_spec(carriers=[h.aws_cross_connect(stub_processes=[])]))

    def test_a_stub_without_process_is_planned_for_the_generator_when_provider_as_is_set(self) -> None:
        _, links = plan_links(_spec(carriers=[h.aws_cross_connect(stub_processes=[])], provider_as=h.AWS_AS))

        assert [link.far_process_id for link in links] == [None]
        assert [(side.device_name) for side in stubs_to_build(links)] == ["EXT-STUB-1"]

    def test_a_stub_with_its_process_is_not_built(self) -> None:
        _, links = plan_links(_spec())

        assert stubs_to_build(links) == []

    def test_a_stub_with_two_processes_is_refused(self) -> None:
        processes = [h.bgp("bgp-1"), h.bgp("bgp-2")]

        with pytest.raises(ValueError, match="exactly one"):
            plan_links(_spec(carriers=[h.aws_cross_connect(stub_processes=processes)]))

    def test_a_process_of_another_as_than_provider_as_is_refused(self) -> None:
        with pytest.raises(ValueError, match="provider_as"):
            plan_links(_spec(provider_as="as-other"))

    def test_a_process_of_the_provider_as_is_accepted(self) -> None:
        _, links = plan_links(_spec(provider_as=h.AWS_AS))

        assert len(links) == 1

    def test_no_provider_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no provider"):
            plan_links(_spec(with_provider=False))

    def test_same_near_and_far_is_refused(self) -> None:
        payload = h.connection()
        payload["TopologyProviderConnection"][0]["far"]["id"] = h.NEAR

        with pytest.raises(ValueError, match="same facility"):
            plan_links(parse_connection(payload["TopologyProviderConnection"][0]))


class TestInternetVpn:
    def _vpn(self, **kwargs: Any):  # noqa: ANN202
        defaults: dict[str, Any] = {
            "path_type": "internet_vpn",
            "internet": [h.internet_carrier("edge-1")],
            "stubs": [h.stub_device()],
            "link_prefixes": [h.prefix("p-2", "169.254.10.0/30"), h.prefix("p-1", "169.254.9.0/30")],
        }
        return _spec(**{**defaults, **kwargs})

    def test_two_tunnels_between_the_near_device_and_the_stub(self) -> None:
        flavour, links = plan_links(self._vpn())

        assert flavour == flavour_for("aws", "internet_vpn")
        assert [link.name for link in links] == ["AWS-FR2-L1", "AWS-FR2-L2"]
        assert {link.circuit_type for link in links} == {"vpn"}
        assert {(link.near.device_name, link.far.device_name) for link in links} == {("edge-1", "EXT-STUB")}
        # the tunnel rides on the port of the internet carrier the device leaves by; the stub side has none
        assert all(link.near.port is not None and link.near.port.id == "uplink-edge-1" for link in links)
        assert all(link.far.port is None for link in links)
        # numeric address order: 169.254.9.0/30 before 169.254.10.0/30 (a string sort would swap them)
        assert [link.link_prefix_id for link in links] == ["p-1", "p-2"]

    def test_two_near_devices_are_ambiguous(self) -> None:
        with pytest.raises(ValueError, match="exactly one device"):
            plan_links(self._vpn(internet=[h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]))

    def test_no_internet_carrier_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly one device"):
            plan_links(self._vpn(internet=[]))

    def test_the_listed_internet_carrier_chooses_among_several_internet_fed_edges(self) -> None:
        both = [h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]

        _, links = plan_links(self._vpn(internet=both, carriers=[both[1]]))

        assert {link.near.device_name for link in links} == {"edge-2"}
        assert {link.near.port.id for link in links if link.near.port} == {"uplink-edge-2"}

    def test_a_listed_carrier_that_is_no_internet_carrier_is_refused(self) -> None:
        with pytest.raises(ValueError, match="internet carrier circuits"):
            plan_links(self._vpn(carriers=[h.aws_cross_connect(1)]))

    def test_a_decommissioned_internet_carrier_does_not_count(self) -> None:
        with pytest.raises(ValueError, match="exactly one device"):
            plan_links(self._vpn(internet=[h.internet_carrier("edge-1", status="decommissioned")]))

    def test_two_stubs_need_provider_as_to_choose(self) -> None:
        other = h.stub_device("dev-other", "EXT-OTHER", [h.bgp("bgp-other", "as-other")])
        stubs = [h.stub_device(), other]

        with pytest.raises(ValueError, match="exactly one provider stub"):
            plan_links(self._vpn(stubs=stubs))

        _, links = plan_links(self._vpn(stubs=stubs, provider_as=h.AWS_AS))
        assert {link.far.device_name for link in links} == {"EXT-STUB"}

    def test_no_stub_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no provider appliance or device"):
            plan_links(self._vpn(stubs=[]))

    def test_a_stub_without_process_is_refused_without_provider_as(self) -> None:
        with pytest.raises(ValueError, match="exactly one provider stub"):
            plan_links(self._vpn(stubs=[h.stub_device(processes=[])]))

    def test_the_one_stub_without_process_is_built_with_the_provider_as(self) -> None:
        other = h.stub_device("dev-other", "EXT-OTHER", [h.bgp("bgp-other", "as-other")])
        bare = h.stub_device(processes=[])

        _, links = plan_links(self._vpn(stubs=[other, bare], provider_as=h.AWS_AS))

        assert {link.far.device_name for link in links} == {"EXT-STUB"}
        assert [s.device_name for s in stubs_to_build(links)] == ["EXT-STUB"]

    def test_two_stubs_without_process_are_ambiguous(self) -> None:
        stubs = [h.stub_device(processes=[]), h.stub_device("dev-2", "EXT-2", [])]

        with pytest.raises(ValueError, match="exactly one provider stub"):
            plan_links(self._vpn(stubs=stubs, provider_as=h.AWS_AS))


class TestLinkPrefixes:
    FLAVOUR = ConnectionFlavour("provider_assigned", 30, 2)

    def test_count_must_match_the_links(self) -> None:
        with pytest.raises(ValueError, match="2 link_prefixes expected, found 1"):
            pair_link_prefixes(self.FLAVOUR, (("p-1", "10.0.0.0/30"),))

    def test_length_must_match_the_flavour(self) -> None:
        with pytest.raises(ValueError, match="not a /30"):
            pair_link_prefixes(self.FLAVOUR, (("p-1", "10.0.0.0/30"), ("p-2", "10.0.1.0/29")))

    def test_an_our_pool_flavour_takes_none(self) -> None:
        assert pair_link_prefixes(flavour_for("aws", "dedicated"), (("p-1", "10.0.0.0/30"),)) == [None]


class TestChoices:
    def test_vlan_precedence(self) -> None:
        """The requested id wins, then the one the sessions ride, then the lowest free."""
        assert choose_vlan(1100, 200, [2, 3]) == 1100
        assert choose_vlan(None, 200, [2, 3]) == 200
        assert choose_vlan(None, None, [2, 3, 5]) == 4

    def test_vlan_range_exhausted(self) -> None:
        with pytest.raises(ValueError, match="no free VLAN"):
            choose_vlan(None, None, range(1, 4095))

    def test_tunnel_names(self) -> None:
        assert choose_tunnel_name([]) == "Tunnel1"
        assert choose_tunnel_name(["Tunnel1", "Tunnel3", "Loopback0"]) == "Tunnel2"
        assert choose_tunnel_name(tunnel_names(3)[:3]) == "Tunnel4"
        with pytest.raises(ValueError, match="taken"):
            choose_tunnel_name(tunnel_names())

    def test_existing_vlan_reads_the_near_end_name(self) -> None:
        sess = ExistingSession(
            id="s",
            name="n",
            circuit_type="handoff",
            status="active",
            provider_id=None,
            namespace_id=None,
            connection_id=None,
            link_prefix_id=None,
            password_id=None,
            location_ids=frozenset(),
            ends=(SessionEnd("a", "Ethernet1/1.1100", "dev-near"), SessionEnd("b", "et-0/0/0.1100", "dev-far")),
            groups=frozenset(),
        )

        assert existing_vlan(sess, "dev-near") == 1100
        assert existing_vlan(sess, "dev-other") is None
        assert existing_vlan(None, "dev-near") is None


class TestSessionIsCurrent:
    def _session(self, **over: Any) -> ExistingSession:
        fields: dict[str, Any] = {
            "id": "s",
            "name": "AWS-FR2-L1",
            "circuit_type": "handoff",
            "status": "active",
            "provider_id": h.PROVIDER,
            "namespace_id": h.NAMESPACE,
            "connection_id": h.CONNECTION_ID,
            "link_prefix_id": None,
            "password_id": None,
            "location_ids": frozenset({h.NEAR, h.FAR}),
            "ends": (SessionEnd("a", "x.2", "d1"), SessionEnd("b", "y.2", "d2")),
            "groups": frozenset({"virtual_circuits"}),
        }
        return ExistingSession(**{**fields, **over})

    DESIRED = {
        "circuit_type": "handoff",
        "status": "active",
        "provider_id": h.PROVIDER,
        "namespace_id": h.NAMESPACE,
        "connection_id": h.CONNECTION_ID,
        "link_prefix_id": None,
        "password_id": None,
        "location_ids": frozenset({h.NEAR, h.FAR}),
    }

    def _current(self, session: ExistingSession | None, desired: dict[str, Any] | None = None) -> bool:
        return session_is_current(
            session, desired=desired or self.DESIRED, end_ids=frozenset({"a", "b"}), group="virtual_circuits"
        )

    def test_identical_is_current(self) -> None:
        assert self._current(self._session())

    @pytest.mark.parametrize(
        "over",
        [
            {"circuit_type": "vpn"},
            {"status": "down"},
            {"provider_id": "other"},
            {"namespace_id": "other"},
            {"connection_id": None},
            {"location_ids": frozenset({h.NEAR})},
            {"ends": (SessionEnd("a", "x.2", "d1"), SessionEnd("c", "y.2", "d2"))},
            {"groups": frozenset()},
        ],
    )
    def test_any_difference_rewrites(self, over: dict[str, Any]) -> None:
        assert not self._current(self._session(**over))

    def test_missing_session_is_not_current(self) -> None:
        assert not self._current(None)

    def test_a_link_prefix_is_compared_only_when_wanted(self) -> None:
        wanted = {**self.DESIRED, "link_prefix_id": "p-1"}

        assert not self._current(self._session(), wanted)
        assert self._current(self._session(link_prefix_id="p-1"), wanted)
        assert self._current(self._session(link_prefix_id="p-1"))
