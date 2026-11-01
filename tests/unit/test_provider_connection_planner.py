"""The pure planner of add_provider_connection (generators/helpers/provider_connection.py).

It reads the cleaned query payload, picks the flavour of the provider and plans the links from the ends the generator
decided (the near-port choice is tests/unit/test_near_port.py), and raises
ValueError for everything that cannot be built, so the generator refuses a spec before its first write.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.provider_connection import (
    DedicatedEnds,
    ExistingSession,
    SessionEnd,
    bind_far_port,
    choose_stub,
    choose_tunnel_name,
    choose_vlan,
    existing_vlan,
    nic_name,
    own_carriers,
    pair_link_prefixes,
    parse_connection,
    plan_links,
    port_of,
    reused_ends,
    session_is_current,
    stubs_to_build,
    tunnel_names,
    validate_spec,
)
from tests.unit import provider_connection_helpers as h
from tests.unit.provider_connection_helpers import dual_link_profile  # noqa: F401
from utils.provider_services import ConnectionFlavour, flavour_for


def _spec(**kwargs: Any):  # noqa: ANN202
    """The parsed spec of a connection built with the helpers' keyword arguments."""
    return parse_connection(h.connection(**kwargs)["TopologyProviderConnection"][0])


pytestmark = pytest.mark.usefixtures("dual_link_profile")


def _ends(spec: Any, *, near: str = "eg-1", far_process: bool = True, nic: bool = True) -> DedicatedEnds:
    """The ends the generator would decide: port ``Ethernet1/1`` of ``near``, the service edge, a NIC (or None)."""
    stub = choose_stub(spec)
    near_port = h.port(h.pid(near, "Ethernet1/1"), "Ethernet1/1", near, h.NEAR)
    far = port_of(spec, "nic-1", "vnic-AWS-FR2", stub.id, stub.name) if nic else None
    return DedicatedEnds(port_of(spec, near_port["id"], "Ethernet1/1", f"dev-{near}", near), stub, far)


class TestDedicated:
    def test_one_link_rides_the_decided_ends(self) -> None:
        """The aws dedicated flavour has one link: the near port, the far NIC, the service edge is the far device."""
        spec = _spec()
        flavour, links = plan_links(spec, _ends(spec))

        assert flavour == flavour_for("aws", "dedicated")
        assert [link.name for link in links] == ["AWS-FR2-L1"]
        link = links[0]
        assert link.circuit_type == "handoff"
        assert link.near.port is not None and link.near.port.id == h.pid("eg-1", "Ethernet1/1")
        assert link.far.port is not None and link.far.port.id == "nic-1"
        assert link.far.device_name == "AWS-SE"
        assert link.far_process_id == "bgp-stub"
        assert link.link_prefix_id is None

    def test_a_far_port_still_to_be_created_plans_without_a_port_and_binds_later(self) -> None:
        spec = _spec()
        _, links = plan_links(spec, _ends(spec, nic=False))

        assert links[0].far.port is None
        bound = bind_far_port(links, port_of(spec, "nic-9", "vnic-AWS-FR2", "dev-AWS-SE", "AWS-SE"))
        assert bound[0].far.port is not None and bound[0].far.port.id == "nic-9"

    def test_a_dedicated_connection_needs_its_ends(self) -> None:
        with pytest.raises(ValueError, match="near and far ends"):
            plan_links(_spec())

    def test_a_flavour_of_two_dedicated_links_is_refused(self) -> None:
        """A leg is a connection: the primary and the secondary leg are two connections."""
        with pytest.raises(ValueError, match="one leg"):
            validate_spec(_spec(profile="dual"))

    def test_a_stub_without_its_bgp_process_is_refused(self) -> None:
        spec = _spec(stubs=[h.stub_device(processes=[])])

        with pytest.raises(ValueError, match="no ManagedBGP process"):
            plan_links(spec, _ends(spec))

    def test_a_stub_without_process_is_planned_for_the_generator_when_provider_as_is_set(self) -> None:
        spec = _spec(stubs=[h.stub_device(processes=[])], provider_as=h.AWS_AS)
        _, links = plan_links(spec, _ends(spec))

        assert [link.far_process_id for link in links] == [None]
        assert [(side.device_name) for side in stubs_to_build(links)] == ["AWS-SE"]

    def test_a_stub_with_its_process_is_not_built(self) -> None:
        spec = _spec()
        _, links = plan_links(spec, _ends(spec))

        assert stubs_to_build(links) == []

    def test_a_stub_with_two_processes_is_refused(self) -> None:
        spec = _spec(stubs=[h.stub_device(processes=[h.bgp("bgp-1"), h.bgp("bgp-2")])])

        with pytest.raises(ValueError, match="exactly one"):
            plan_links(spec, _ends(spec))

    def test_a_process_of_another_as_than_provider_as_is_refused(self) -> None:
        spec = _spec(provider_as="as-other")

        with pytest.raises(ValueError, match="provider_as"):
            plan_links(spec, _ends(spec))

    def test_a_process_of_the_provider_as_is_accepted(self) -> None:
        spec = _spec(provider_as=h.AWS_AS)

        _, links = plan_links(spec, _ends(spec))

        assert len(links) == 1

    def test_no_provider_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no provider"):
            validate_spec(_spec(with_provider=False))

    def test_same_near_and_far_is_refused(self) -> None:
        payload = h.connection()
        payload["TopologyProviderConnection"][0]["far"]["id"] = h.NEAR

        with pytest.raises(ValueError, match="same facility"):
            validate_spec(parse_connection(payload["TopologyProviderConnection"][0]))


class TestIntentFields:
    def test_the_request_is_read_without_a_carrier_or_a_port(self) -> None:
        spec = _spec()

        assert (spec.near_port_id, spec.carriers, spec.bandwidth, spec.far_kind) == (None, (), None, h.FAR_KIND)
        assert spec.near_devices == h.NEAR_DEVICES
        assert spec.creates_carrier_ends

    def test_an_explicit_port_and_the_bandwidth_are_read(self) -> None:
        spec = _spec(near_port="p-x", bandwidth=1000)

        assert (spec.near_port_id, spec.bandwidth) == ("p-x", 1000)

    def test_a_virtual_colocation_cannot_get_a_created_carrier(self) -> None:
        assert not _spec(far_kind="TopologyVirtualColocation").creates_carrier_ends

    def test_the_load_of_the_other_connections_is_their_end_devices_not_this_one(self) -> None:
        spec = _spec(
            near_load=[h.other_connection("other-1", "eg-1", "AWS-SE"), h.other_connection(h.CONNECTION_ID, "eg-2")]
        )

        assert spec.near_load == (frozenset({"dev-eg-1", "dev-AWS-SE"}),)

    def test_the_virtual_nic_is_named_after_the_connection(self) -> None:
        assert nic_name("PC-A") == "vnic-PC-A"


class TestOwnCarrier:
    def test_a_live_listed_carrier_between_the_two_facilities_is_the_connections_own(self) -> None:
        spec = _spec(carriers=[h.own_carrier()])

        ((carrier, near, far),) = own_carriers(spec)

        assert (carrier.id, near.device_name, far.device_name) == ("xc-1", "eg-1", "AWS-SE")

    def test_a_decommissioned_carrier_or_one_missing_an_end_is_not(self) -> None:
        dead = h.own_carrier(status="decommissioned")
        half = h.carrier("xc-2", "XC-2", [h.port("p-1", "Ethernet1/1", "eg-1", h.NEAR)])

        assert own_carriers(_spec(carriers=[dead, half])) == []

    def test_the_ends_of_a_reused_carrier_become_the_link_ends(self) -> None:
        spec = _spec()
        ends = [
            port_of(spec, "p-near", "Ethernet1/2", "dev-eg-2", "eg-2"),
            port_of(spec, "p-far", "vnic-x", "dev-AWS-SE", "AWS-SE"),
        ]

        reused = reused_ends(spec, "XC-9", ends)

        assert (reused.near.name, reused.far.name if reused.far else None, reused.far_device.name) == (
            "Ethernet1/2",
            "vnic-x",
            "AWS-SE",
        )

    def test_a_reused_carrier_must_end_on_both_facilities(self) -> None:
        spec = _spec()

        with pytest.raises(ValueError, match="XC-9 must end once"):
            reused_ends(spec, "XC-9", [port_of(spec, "p-near", "Ethernet1/2", "dev-eg-2", "eg-2")])


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
        assert {(link.near.device_name, link.far.device_name) for link in links} == {("edge-1", "AWS-SE")}
        # the tunnel rides on the port of the internet carrier the device leaves by; the stub side has none
        assert all(link.near.port is not None and link.near.port.id == "uplink-edge-1" for link in links)
        assert all(link.far.port is None for link in links)
        # numeric address order: 169.254.9.0/30 before 169.254.10.0/30 (a string sort would swap them)
        assert [link.link_prefix_id for link in links] == ["p-1", "p-2"]

    def test_two_near_devices_are_ambiguous(self) -> None:
        with pytest.raises(ValueError, match="exactly one device"):
            plan_links(self._vpn(internet=[h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]))

    def test_near_port_picks_the_matching_edge_among_two(self) -> None:
        both = [h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]

        _, links = plan_links(self._vpn(internet=both, near_port="uplink-edge-2"))

        assert {(link.near.device_name, link.near.port.id) for link in links if link.near.port} == {
            ("edge-2", "uplink-edge-2")
        }

    def test_near_port_that_is_no_internet_carrier_end_is_refused(self) -> None:
        both = [h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]

        with pytest.raises(ValueError, match="near_port p-other is not the end port of an internet carrier"):
            plan_links(self._vpn(internet=both, near_port="p-other"))

    def test_two_edges_without_near_port_name_it_as_the_fix(self) -> None:
        both = [h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]

        with pytest.raises(ValueError, match="set near_port"):
            plan_links(self._vpn(internet=both))

    def test_one_edge_with_its_near_port_works_as_before(self) -> None:
        _, links = plan_links(self._vpn(near_port="uplink-edge-1"))

        assert {link.near.device_name for link in links} == {"edge-1"}

    def test_no_internet_carrier_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly one device"):
            plan_links(self._vpn(internet=[]))

    def test_several_internet_fed_edges_say_to_fix_the_internet_carriers(self) -> None:
        both = [h.internet_carrier("edge-1"), h.internet_carrier("edge-2")]

        with pytest.raises(ValueError, match="fix the internet carriers of the near facility"):
            plan_links(self._vpn(internet=both))

    def test_a_decommissioned_internet_carrier_does_not_count(self) -> None:
        with pytest.raises(ValueError, match="exactly one device"):
            plan_links(self._vpn(internet=[h.internet_carrier("edge-1", status="decommissioned")]))

    def test_two_stubs_need_provider_as_to_choose(self) -> None:
        other = h.stub_device("dev-other", "EXT-OTHER", [h.bgp("bgp-other", "as-other")])
        stubs = [h.stub_device(), other]

        with pytest.raises(ValueError, match="exactly one provider stub"):
            plan_links(self._vpn(stubs=stubs))

        _, links = plan_links(self._vpn(stubs=stubs, provider_as=h.AWS_AS))
        assert {link.far.device_name for link in links} == {"AWS-SE"}

    def test_no_stub_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no provider appliance or device"):
            plan_links(self._vpn(stubs=[]))

    def test_a_stub_without_process_is_refused_without_provider_as(self) -> None:
        with pytest.raises(ValueError, match="no provider_as to build it with"):
            plan_links(self._vpn(stubs=[h.stub_device(processes=[])]))

    def test_the_one_stub_without_process_is_built_with_the_provider_as(self) -> None:
        other = h.stub_device("dev-other", "EXT-OTHER", [h.bgp("bgp-other", "as-other")])
        bare = h.stub_device(processes=[])

        _, links = plan_links(self._vpn(stubs=[other, bare], provider_as=h.AWS_AS))

        assert {link.far.device_name for link in links} == {"AWS-SE"}
        assert [s.device_name for s in stubs_to_build(links)] == ["AWS-SE"]

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
