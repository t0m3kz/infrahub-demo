"""Unit tests for the route-map policies of external VRF peers: on-prem prefixes, internal versus external."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.external_vrf_peers_helpers import (
    _C2,
    _C3,
    _C15,
    _NON_PROD,
    _PROD,
    _PROVIDER,
    _peering,
    _subif,
)
from transforms.common import _own_devices
from transforms.helpers.bgp import (
    _build_session_from_peering,
    _far_end_is_ours,
    get_external_session_policies,
    get_onprem_prefixes,
)


def _segment(
    prefix: str,
    namespace: dict[str, Any],
    member_type: str = "address",
    *,
    owner: dict[str, Any] | None = _C3,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "status": status,
        "owner": owner,
        "gateway": {"ip_prefix": {"prefix": prefix, "member_type": member_type, "ip_namespace": namespace}},
    }


# ---------------------------------------------------------------------------
# Outbound: the namespace's on-prem prefixes
# ---------------------------------------------------------------------------


class TestOnpremPrefixes:
    def test_groups_segment_prefixes_by_tenant_vrf(self) -> None:
        """PROD and NON-PROD keep separate lists; containers, exchange VRFs, IPv6 and 0/0 are left out."""
        segments = [
            _segment("10.5.20.0/24", _PROD),
            _segment("10.5.10.0/24", _PROD),
            _segment("10.5.10.0/24", _PROD),
            _segment("10.5.0.0/16", _PROD, member_type="prefix"),
            _segment("2001:db8:5::/64", _PROD),
            _segment("0.0.0.0/0", _PROD),
            _segment("10.6.10.0/24", _NON_PROD),
            _segment("10.250.0.0/24", {"name": "MANAGEMENT", "namespace_type": "management"}),
            _segment("192.0.2.0/24", {"name": "INTERNET", "namespace_type": "internet"}),
            {"gateway": None, "owner": _C3, "status": "active"},
        ]
        assert get_onprem_prefixes(segments) == {
            "NON-PROD": {"C003": ["10.6.10.0/24"]},
            "PROD": {"C003": ["10.5.10.0/24", "10.5.20.0/24"]},
        }

    def test_groups_by_owner_with_sorted_keys_and_prefixes(self) -> None:
        """VRF -> owner -> prefixes in address order; an owner's NON-PROD space is not its PROD space."""
        segments = [
            _segment("10.7.10.0/24", _PROD, owner=_C15),
            _segment("10.5.20.0/24", _PROD, owner=_C3),
            _segment("10.8.2.0/24", _PROD, owner=_C2),
            _segment("10.5.9.0/24", _PROD, owner=_C3),
            _segment("10.6.10.0/24", _NON_PROD, owner=_C3),
        ]
        found = get_onprem_prefixes(segments)
        assert found == {
            "NON-PROD": {"C003": ["10.6.10.0/24"]},
            "PROD": {"C002": ["10.8.2.0/24"], "C003": ["10.5.9.0/24", "10.5.20.0/24"], "C015": ["10.7.10.0/24"]},
        }
        assert list(found["PROD"]) == ["C002", "C003", "C015"]

    def test_retired_segments_are_skipped(self) -> None:
        """Every other status still qualifies; retired does not, and an all-retired VRF is absent."""
        segments = [
            _segment("10.5.1.0/24", _PROD, status="active"),
            _segment("10.5.2.0/24", _PROD, status="provisioning"),
            _segment("10.5.3.0/24", _PROD, status="maintenance"),
            _segment("10.5.4.0/24", _PROD, status="deprecated"),
            _segment("10.5.5.0/24", _PROD, status="retired"),
            _segment("10.6.5.0/24", _NON_PROD, status="retired"),
        ]
        assert get_onprem_prefixes(segments) == {
            "PROD": {"C003": ["10.5.1.0/24", "10.5.2.0/24", "10.5.3.0/24", "10.5.4.0/24"]}
        }

    @pytest.mark.parametrize("owner", [None, _PROVIDER], ids=["no-owner", "provider"])
    def test_a_segment_without_a_customer_owner_is_never_announced(self, owner: dict[str, Any] | None) -> None:
        """Ownership is what registers a prefix: no customer owner, no announcement."""
        segments = [
            _segment("10.5.77.0/24", _PROD, owner=owner),
            {"gateway": _segment("10.5.78.0/24", _PROD)["gateway"]},
        ]
        assert get_onprem_prefixes(segments) == {}

    def test_no_segments_is_empty(self) -> None:
        assert get_onprem_prefixes(None) == {}


class TestExternalSessionPolicies:
    """get_external_session_policies: the owners of a session, what it accepts, what it is announced."""

    _ONPREM = {
        "PROD": {"C002": ["10.8.2.0/24"], "C003": ["10.5.10.0/24", "10.5.20.0/24"], "C015": ["10.7.10.0/24"]},
        "NON-PROD": {"C003": ["10.6.10.0/24"], "C015": ["10.6.20.0/24"]},
    }

    def test_out_is_the_union_of_the_sessions_owners_only(self) -> None:
        """C002 owns nothing behind this session, so its space is neither accepted nor announced."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1000", "PROD")],
            {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.31.0.0/16", "10.32.0.0/16"]}},
            self._ONPREM,
        )
        assert policies == {
            "Ethernet1/13.1000": {
                "owners": ["C003", "C015"],
                "in": ["10.30.0.0/16", "10.31.0.0/16", "10.32.0.0/16"],
                "out": ["10.5.10.0/24", "10.5.20.0/24", "10.7.10.0/24"],
            }
        }

    def test_out_is_limited_to_the_sub_interfaces_namespace(self) -> None:
        """A NON-PROD session never announces the owner's PROD space."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1001", "NON-PROD")], {"Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}, self._ONPREM
        )
        assert policies["Ethernet1/13.1001"]["out"] == ["10.6.10.0/24"]

    def test_a_session_with_no_owner_accepts_and_announces_nothing(self) -> None:
        """A known far end with nothing customer-owned registered: empty owners, in and out."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1900", "PROD")], {"Ethernet1/13.1900": {}}, self._ONPREM
        )
        assert policies == {"Ethernet1/13.1900": {"owners": [], "in": [], "out": []}}

    def test_an_owner_without_onprem_space_in_the_namespace_gets_an_empty_out(self) -> None:
        """The far end registers C005's prefix, C005 has no segment here: accepts it, announces nothing."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1900", "PROD")], {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}}, self._ONPREM
        )
        assert policies["Ethernet1/13.1900"] == {"owners": ["C005"], "in": ["10.176.5.0/24"], "out": []}

    def test_only_sessions_with_a_known_far_end_have_a_policy(self) -> None:
        """No far end resolved: absent (the template denies both ways)."""
        assert get_external_session_policies([_subif("Ethernet1/13.1300", "PROD")], {}, self._ONPREM) == {}

    def test_two_owners_sharing_a_prefix_list_it_once_and_the_lists_are_sorted(self) -> None:
        """Dedup across owners, address order (10.9 before 10.10) and length order, whatever the input order."""
        onprem = {"PROD": {"C003": ["10.10.0.0/24", "10.9.0.0/24"], "C015": ["10.9.0.0/24", "10.9.0.0/16"]}}
        peers = {"Ethernet1/13.1000": {"C015": ["10.40.0.0/16", "10.4.0.0/16"], "C003": ["10.4.0.0/16"]}}
        policies = get_external_session_policies([_subif("Ethernet1/13.1000", "PROD")], peers, onprem)
        assert policies["Ethernet1/13.1000"] == {
            "owners": ["C003", "C015"],
            "in": ["10.4.0.0/16", "10.40.0.0/16"],
            "out": ["10.9.0.0/16", "10.9.0.0/24", "10.10.0.0/24"],
        }

    def test_the_result_does_not_depend_on_the_input_order(self) -> None:
        """Same inputs in another order: the identical policy, so the rendered route-maps are byte-stable."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {
            "Ethernet1/13.1000": {"C015": ["10.31.0.0/16"], "C003": ["10.30.0.0/16"]},
            "Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]},
        }
        forward = get_external_session_policies(interfaces, peers, self._ONPREM)
        reverse = get_external_session_policies(interfaces[::-1], dict(reversed(list(peers.items()))), self._ONPREM)
        assert forward == reverse
        assert forward["Ethernet1/13.1000"]["owners"] == ["C003", "C015"]

    def test_each_session_reads_only_its_own_sub_interfaces_namespace(self) -> None:
        """Two sessions of one owner in two namespaces: PROD announces the PROD space, NON-PROD the NON-PROD one."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}, "Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert policies["Ethernet1/13.1000"]["out"] == ["10.5.10.0/24", "10.5.20.0/24"]
        assert policies["Ethernet1/13.1001"]["out"] == ["10.6.10.0/24"]

    def test_a_peer_entry_for_an_unknown_sub_interface_announces_nothing(self) -> None:
        """No such sub-interface (or no interfaces at all): no namespace, so no on-prem space is looked up."""
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}
        for interfaces in (None, [], [_subif("Ethernet1/13.9", "PROD")]):
            policies = get_external_session_policies(interfaces, peers, self._ONPREM)
            assert policies["Ethernet1/13.1000"] == {"owners": ["C003"], "in": ["10.30.0.0/16"], "out": []}

    def test_a_namespace_without_onprem_space_announces_nothing(self) -> None:
        """The sub-interface sits in a namespace no segment is in: out stays empty, in is untouched."""
        policies = get_external_session_policies(
            [_subif("Ethernet1/13.1000", "OTHER")], {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}, self._ONPREM
        )
        assert policies["Ethernet1/13.1000"]["out"] == []
        assert policies["Ethernet1/13.1000"]["in"] == ["10.30.0.0/16"]

    def test_no_peers_is_no_policies(self) -> None:
        """No external session on the device: nothing to build."""
        assert get_external_session_policies([_subif("Ethernet1/13.1000", "PROD")], {}, {}) == {}

    def test_a_sub_interface_without_a_namespace_announces_nothing(self) -> None:
        """No address namespace to look the owners' space up in."""
        policies = get_external_session_policies(
            [{"name": "Ethernet1/13.1000", "ip_address": None}],
            {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}},
            self._ONPREM,
        )
        assert policies["Ethernet1/13.1000"]["out"] == []

    def test_an_owner_on_two_sessions_of_a_namespace_is_passed_between_them(self) -> None:
        """C003's office LAN behind the gateway reaches the VIF; its VPC block reaches the gateway; C015 stays home."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1900", "PROD")]
        peers = {
            "Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.32.0.0/16"]},
            "Ethernet1/13.1900": {"C003": ["10.210.3.0/24"]},
        }
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert policies["Ethernet1/13.1900"] == {
            "owners": ["C003"],
            "in": ["10.210.3.0/24"],
            "out": ["10.5.10.0/24", "10.5.20.0/24", "10.30.0.0/16"],
        }
        assert policies["Ethernet1/13.1000"] == {
            "owners": ["C003", "C015"],
            "in": ["10.30.0.0/16", "10.32.0.0/16"],
            "out": ["10.5.10.0/24", "10.5.20.0/24", "10.7.10.0/24", "10.210.3.0/24"],
        }

    def test_sessions_in_different_namespaces_do_not_exchange(self) -> None:
        """The same owner on a PROD and a NON-PROD session: each is announced its own namespace's space only."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}, "Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert "10.60.0.0/16" not in policies["Ethernet1/13.1000"]["out"]
        assert "10.30.0.0/16" not in policies["Ethernet1/13.1001"]["out"]

    def test_a_learned_segment_prefix_is_not_announced_back_nor_local(self) -> None:
        """C005's hosted segment is both a segment and registered behind the gateway session."""
        interfaces = [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/13.1000", "PROD")]
        peers = {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}, "Ethernet1/13.1000": {"C005": ["10.50.0.0/16"]}}
        onprem = {"PROD": {"C005": ["10.176.5.0/24"]}}
        policies = get_external_session_policies(interfaces, peers, onprem)
        assert policies["Ethernet1/13.1900"]["out"] == ["10.50.0.0/16"]
        assert policies["Ethernet1/13.1000"]["out"] == ["10.176.5.0/24"]

    def test_sessions_without_a_namespace_never_exchange_with_each_other(self) -> None:
        """Two sub-interfaces without an address stand alone: C003 on both does not cross-announce."""
        interfaces = [
            {"name": "Ethernet1/13.1000", "ip_address": None},
            {"name": "Ethernet1/13.1900", "ip_address": None},
        ]
        peers = {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}, "Ethernet1/13.1900": {"C003": ["10.210.3.0/24"]}}
        policies = get_external_session_policies(interfaces, peers, self._ONPREM)
        assert policies["Ethernet1/13.1000"]["out"] == []
        assert policies["Ethernet1/13.1900"]["out"] == []

    def test_the_result_keeps_the_peer_order(self) -> None:
        """One entry per peer, in the order given."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1001", "NON-PROD")]
        peers = {"Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}, "Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}
        assert list(get_external_session_policies(interfaces, peers, self._ONPREM)) == list(peers)

    def test_conflicts_are_logged_as_warnings_with_the_device(self, caplog: pytest.LogCaptureFixture) -> None:
        """A prefix of two owners and an owner's prefix behind two sessions: WARNING each, never an error."""
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13.1900", "PROD")]
        peers = {
            "Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.40.0.0/16"]},
            "Ethernet1/13.1900": {"C003": ["10.30.0.0/16"], "C002": ["10.40.0.0/16"]},
        }
        with caplog.at_level("WARNING", logger="transforms.helpers.bgp"):
            policies = get_external_session_policies(interfaces, peers, {}, device_name="eg-fr201")
        assert set(policies) == set(peers)
        messages = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert len(messages) == 2
        assert all(message.startswith("eg-fr201: namespace PROD: prefix ") for message in messages)
        assert any("registered by more than one owner" in message and "10.40.0.0/16" in message for message in messages)
        assert any("more than one session" in message and "10.30.0.0/16" in message for message in messages)
        assert not [record for record in caplog.records if record.levelname == "ERROR"]

    def test_clean_registrations_log_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """No conflict, no warning."""
        with caplog.at_level("WARNING", logger="transforms.helpers.bgp"):
            get_external_session_policies(
                [_subif("Ethernet1/13.1000", "PROD")], {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}, self._ONPREM
            )
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Internal vs external
# ---------------------------------------------------------------------------


def _with_far_end_role(peering: dict[str, Any], role: str | None) -> dict[str, Any]:
    """The eg-fr201 <-> EXT-AWS-DX-FR2 peering with the far end's BGP process in ``role``."""
    peering["bgp_processes"][1]["process_role"] = role
    return peering


class TestInternalVersusExternal:
    """A regular VRF session is external unless its far end is one of our devices."""

    @pytest.mark.parametrize("role", ["underlay", "regular", None])
    def test_a_far_end_process_that_is_not_overlay_stays_external(self, role: str | None) -> None:
        session = _build_session_from_peering(
            _with_far_end_role(_peering(_PROD), role), "eg-fr201", {"asn": 4200000100}, []
        )
        assert session is not None
        assert session["external"] is True

    def test_an_overlay_far_end_process_makes_the_session_internal(self) -> None:
        """eg-fr201 <-> eg-am-ne01: the far end runs one of our overlay processes."""
        peering = _with_far_end_role(_peering(_PROD), "overlay")
        session = _build_session_from_peering(peering, "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == "PROD"
        assert "external" not in session

    def test_internal_from_both_sides(self) -> None:
        """Seen from the far end, the same peering is internal too: its process in this peering is the overlay one."""
        peering = _with_far_end_role(_peering(_PROD), "overlay")
        peering["bgp_processes"][0]["process_role"] = "overlay"
        session = _build_session_from_peering(peering, "EXT-AWS-DX-FR2", {"asn": 64512}, [])
        assert session is not None
        assert "external" not in session

    def test_only_the_far_ends_process_counts(self) -> None:
        """An overlay process on OUR end does not make a foreign far end ours."""
        peering = _peering(_PROD)
        peering["bgp_processes"][0]["process_role"] = "overlay"
        session = _build_session_from_peering(peering, "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["external"] is True

    def test_a_far_end_in_own_devices_is_internal(self) -> None:
        """The office's own SD-WAN edge (deployment.lisp_wan_edges) is ours whatever its process says."""
        session = _build_session_from_peering(
            _peering(_PROD), "eg-fr201", {"asn": 4200000100}, [], own_devices=frozenset({"EXT-AWS-DX-FR2"})
        )
        assert session is not None
        assert "external" not in session

    def test_another_device_in_own_devices_does_not_matter(self) -> None:
        session = _build_session_from_peering(
            _peering(_PROD), "eg-fr201", {"asn": 4200000100}, [], own_devices=frozenset({"someone-else"})
        )
        assert session is not None
        assert session["external"] is True

    def test_an_internal_vrf_session_keeps_its_vrf_neighbour_fields(self) -> None:
        """Internal is not a different kind of VRF session: same vrf, addresses and interface."""
        internal = _build_session_from_peering(
            _with_far_end_role(_peering(_PROD), "overlay"), "eg-fr201", {"asn": 4200000100}, []
        )
        external = _build_session_from_peering(_peering(_PROD), "eg-fr201", {"asn": 4200000100}, [])
        assert internal is not None and external is not None
        assert {k: v for k, v in external.items() if k != "external"} == internal

    def test_the_offices_sdwan_edges_are_its_own_devices(self) -> None:
        """BaseDeviceTransform fills own_devices from deployment.lisp_wan_edges (cleaned: a list of names)."""
        deployment = {"lisp_wan_edges": [{"name": "C005-OFFICE-EDGE1"}, {"name": None}, "junk"]}
        assert _own_devices(deployment) == frozenset({"C005-OFFICE-EDGE1"})
        assert _own_devices({"lisp_wan_edges": None}) == frozenset()
        assert _own_devices(None) == frozenset()

    def test_internet_transit_is_never_internal_by_role(self) -> None:
        """The INTERNET namespace is transit regardless of the far end's role."""
        namespace = {"name": "INTERNET", "namespace_type": "internet"}
        session = _build_session_from_peering(
            _with_far_end_role(_peering(namespace), "overlay"), "eg-fr201", {"asn": 4200000100}, []
        )
        assert session is not None
        assert session["transit"] is True


class TestFarEndIsOurs:
    """_far_end_is_ours: an overlay process on the FAR end, or a name in own_devices; nothing else."""

    @staticmethod
    def _node(*processes: Any) -> dict[str, Any]:
        """A peering node whose bgp_processes are exactly ``processes``."""
        return {"bgp_processes": list(processes)}

    @staticmethod
    def _process(role: str | None, *devices: str) -> dict[str, Any]:
        """A BGP process of ``role`` running on ``devices``."""
        return {"process_role": role, "capabilities": [{"name": device} for device in devices]}

    def test_an_overlay_process_on_the_far_end_makes_it_ours(self) -> None:
        """The far end runs one of our overlay processes in this peering."""
        node = self._node(self._process("overlay", "eg-fr201"), self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True

    def test_an_overlay_process_on_the_local_device_only_does_not_make_the_far_end_ours(self) -> None:
        """The local process is in bgp_processes too: only the process running ON the far end counts."""
        node = self._node(self._process("overlay", "eg-fr201"), self._process("regular", "EXT-AWS-DX-FR2"))
        assert _far_end_is_ours(node, "EXT-AWS-DX-FR2", frozenset()) is False

    @pytest.mark.parametrize("role", ["underlay", "regular", None, "", "Overlay"])
    def test_a_far_end_process_in_another_role_is_not_ours(self, role: str | None) -> None:
        """Only the exact role overlay counts: underlay, regular, null, empty or another case stay foreign."""
        node = self._node(self._process("overlay", "eg-fr201"), self._process(role, "EXT-AWS-DX-FR2"))
        assert _far_end_is_ours(node, "EXT-AWS-DX-FR2", frozenset()) is False

    @pytest.mark.parametrize("remote", ["", None])
    def test_a_missing_far_end_name_is_never_ours(self, remote: str | None) -> None:
        """No name to match: not even an overlay process, or an own_devices entry for the empty name, helps."""
        node = self._node(self._process("overlay", "eg-fr201", ""))
        assert _far_end_is_ours(node, remote or "", frozenset({""})) is False

    @pytest.mark.parametrize(
        "node",
        [
            {},
            {"bgp_processes": None},
            {"bgp_processes": []},
            {"bgp_processes": ["junk", None, 3]},
            {"bgp_processes": [{"process_role": "overlay"}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": None}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": []}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": ["EXT-AWS-DX-FR2", None]}]},
            {"bgp_processes": [{"process_role": "overlay", "capabilities": [{"device": "EXT-AWS-DX-FR2"}]}]},
        ],
        ids=[
            "no-key",
            "none",
            "empty",
            "non-dict-processes",
            "no-capabilities-key",
            "null-capabilities",
            "empty-capabilities",
            "non-dict-capabilities",
            "capability-without-name",
        ],
    )
    def test_missing_or_malformed_process_data_leaves_the_far_end_foreign(self, node: dict[str, Any]) -> None:
        """No processes, no capabilities or junk entries: external, never an exception."""
        assert _far_end_is_ours(node, "EXT-AWS-DX-FR2", frozenset()) is False

    def test_a_far_end_in_own_devices_is_ours_without_any_process(self) -> None:
        """The office's own SD-WAN edge carries no overlay process, its name is the signal."""
        assert _far_end_is_ours({}, "C005-OFFICE-EDGE1", frozenset({"C005-OFFICE-EDGE1"})) is True

    def test_own_devices_wins_over_a_foreign_role(self) -> None:
        """A far-end process in role regular does not undo an own_devices hit."""
        node = self._node(self._process("regular", "C005-OFFICE-EDGE1"))
        assert _far_end_is_ours(node, "C005-OFFICE-EDGE1", frozenset({"C005-OFFICE-EDGE1"})) is True

    def test_another_name_in_own_devices_is_not_a_hit(self) -> None:
        """Exact, case-sensitive name match."""
        assert _far_end_is_ours({}, "EXT-AWS-DX-FR2", frozenset({"EXT-AWS-DX-FR2-2", "ext-aws-dx-fr2"})) is False

    def test_one_overlay_process_among_several_is_enough(self) -> None:
        """The far end may run several processes in the peering: any overlay one qualifies."""
        node = self._node(
            self._process("regular", "other"),
            self._process("overlay", "x", "eg-am-ne01"),
            self._process("underlay", "y"),
        )
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True

    @staticmethod
    def _node_with_far_end_kind(kind: str | None, *processes: Any) -> dict[str, Any]:
        """A peering node whose second end is eg-am-ne01 on a device of ``kind`` (None: no typename selected)."""
        device: dict[str, Any] = {"name": "eg-am-ne01"} if kind is None else {"name": "eg-am-ne01", "typename": kind}
        return {
            "capabilities": [
                {"name": "Ethernet1/12.1200", "device": {"name": "eg-fr201"}},
                {"name": "Ethernet1/12.1200", "device": device},
            ],
            "bgp_processes": list(processes),
        }

    def test_an_overlay_process_on_a_network_appliance_does_not_make_it_ours(self) -> None:
        """A provider-hosted Network Edge is never ours via its process role."""
        node = self._node_with_far_end_kind("CloudNetworkAppliance", self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is False

    @pytest.mark.parametrize("kind", ["DcimPhysicalDevice", None, "", "DcimVirtualDevice"])
    def test_an_overlay_process_on_any_other_device_kind_still_makes_it_ours(self, kind: str | None) -> None:
        """Only CloudNetworkAppliance is excluded; a missing typename (older fixtures) is not an appliance."""
        node = self._node_with_far_end_kind(kind, self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True

    def test_own_devices_wins_over_a_network_appliance(self) -> None:
        """The office's own SD-WAN edge stays ours whatever its device kind."""
        node = self._node_with_far_end_kind("CloudNetworkAppliance", self._process("overlay", "eg-am-ne01"))
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset({"eg-am-ne01"})) is True

    def test_only_the_far_ends_device_kind_counts(self) -> None:
        """The LOCAL device being an appliance (first end) does not stop the far end being ours."""
        node = self._node_with_far_end_kind("DcimPhysicalDevice", self._process("overlay", "eg-am-ne01"))
        node["capabilities"][0]["device"]["typename"] = "CloudNetworkAppliance"
        assert _far_end_is_ours(node, "eg-am-ne01", frozenset()) is True
