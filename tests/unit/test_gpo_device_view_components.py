"""Component groups of the NX-OS VXLAN GPO device view.

Where their instances sit (component_coverage), the selectors, the gate and the host trunks.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.smoke.fixture_builders.common import _edges, _make_interface, _node, _v, run_transform
from tests.smoke.fixture_builders.group_policy import (
    _gp_instance,
    _gp_tag,
    _gpo_segment_nodes,
    build_gpo_device_data,
    gpo_segment,
)
from tests.smoke.fixture_builders.vrf import with_vrf_objects
from tests.unit.gpo_device_view_helpers import (
    LEAF_ACTIVATIONS,
    LEAF_SEGMENTS,
    PAY_P,
    _coverage,
    _leaf_interfaces,
    _nic,
    _placed_roots,
    _raw,
    _roots,
    _view,
)
from transforms.config.leaf import Leaf
from transforms.helpers.group_policy import ComponentClaim
from transforms.helpers.vxlan import get_interfaces
from utils.data_cleaning import clean_data

WEB = "web.pay.demo.local"
DB = "db.pay.demo.local"


def _server(name: str, *nics: dict) -> dict:
    return _gp_instance(name, list(nics))


def _vm(name: str, host: str, *nics: dict) -> dict:
    return _gp_instance(name, list(nics), host=host)


def _claim(group: str, vtep: str, port: str) -> ComponentClaim:
    return ComponentClaim(group, "seg-pay-p", vtep, port)


def _prod_reason(view: dict[str, Any] | None) -> str | None:
    assert view is not None
    return view["vrfs"]["PROD"]["unenforced"]


def _device_inputs(
    name: str, role: str, segments: tuple[str, ...], host_ports: dict[str, tuple[str, ...]] | None = None
) -> dict[str, Any]:
    """The interfaces and activations a device of the fixture fleet passes the view (the transform's own)."""
    raw = build_gpo_device_data(device_name=name, role=role, segments=segments, host_ports=host_ports)
    transform = Leaf.__new__(Leaf)
    device, _roots_, _platform = transform._device_and_platform(raw)
    return {
        "device_name": name,
        "role": role,
        "interfaces": device["interfaces"],
        "activations": transform._device_activations(device),
    }


class TestComponentCoverage:
    """Which (VTEP, port) pairs classify each component group of the data center, and what blocks it."""

    def test_the_fixtures_components_are_claimed_on_the_vtep_that_owns_their_port(self) -> None:
        """A physical instance is its own host (trunk port Ethernet10), a VM walks its hosting device (Ethernet11);
        the host's second NIC on DC12's border leaf is that data center's to classify."""
        coverage = _coverage(_roots(_raw()))
        assert coverage.claims == [
            _claim(DB, "dc1-leaf-01", "Ethernet11"),
            _claim(WEB, "dc1-leaf-01", "Ethernet10"),
        ]
        assert coverage.blockers == {}

    def test_a_lag_member_is_claimed_as_its_port_channel_once(self) -> None:
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-access-leaf-01", "Ethernet20", lag="port-channel20"),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet21", lag="port-channel20"),
            )
        ]
        coverage = _coverage(_placed_roots(web=web))
        assert [claim for claim in coverage.claims if claim.group == WEB] == [
            _claim(WEB, "dc1-access-leaf-01", "port-channel20")
        ]
        assert coverage.blockers == {}

    def test_a_lag_member_without_a_port_channel_blocks_and_is_never_the_member_port(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20", far_role="lag"))]
        coverage = _coverage(_placed_roots(web=web))
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {
            WEB: ["srv-web eno1 reaches dc1-leaf-01 Ethernet20, a LAG member with no port-channel"]
        }

    def test_a_port_that_does_not_carry_the_segment_is_not_claimed_and_blocks(self) -> None:
        """The NIC is cabled to a VTEP of this data center, but the port lacks the segment: the component's traffic
        enters unclassified there, so the group must not be enforced (it used to be silently placed)."""
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20", ("web-np",)))]
        roots = _placed_roots(web=web)
        coverage = _coverage(roots)
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {
            WEB: ["srv-web eno1 reaches dc1-leaf-01 Ethernet20, which is not tagged with the segment"]
        }
        assert _prod_reason(_view(roots)) == (
            "component group web_pay_demo_local: srv-web eno1 reaches dc1-leaf-01 Ethernet20, "
            "which is not tagged with the segment"
        )

    def test_a_port_channel_that_does_not_carry_the_segment_blocks_and_names_the_port_channel(self) -> None:
        """A LAG member: the port-channel is what must carry the segment, whatever the member port is tagged with."""
        member = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20", lag="port-channel20")
        far = member["cable"]["node"]["endpoints"]["edges"][1]["node"]
        far["segments"] = _edges([{"id": "seg-pay-p"}])  # the member port is tagged, not the channel
        far["lag"]["node"]["segments"] = _edges([{"id": "seg-web-np"}])
        roots = _placed_roots(web=[_server("srv-web", member)])
        coverage = _coverage(roots)
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {
            WEB: ["srv-web eno1 reaches dc1-leaf-01 port-channel20, which is not tagged with the segment"]
        }
        assert _prod_reason(_view(roots)) is not None

    def test_a_far_end_that_is_a_vtep_of_another_data_center_is_ignored_even_without_the_segment(self) -> None:
        """Another data center classifies its own ports: only a VTEP of THIS one is checked for the tag."""
        web = [_server("srv-web", _nic("srv-web", "eno1", "bl-dc12-01", "Ethernet5", ("web-np",)))]
        assert _coverage(_placed_roots(web=web)).blockers == {}

    def test_a_cable_with_only_the_near_end_blocks(self) -> None:
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20")
        nic["cable"]["node"]["endpoints"]["edges"].pop()
        assert _coverage(_placed_roots(web=[_server("srv-web", nic)])).blockers == {
            WEB: ["srv-web eno1: cable has 0 far ends"]
        }

    def test_a_cable_with_more_than_one_far_end_blocks_instead_of_taking_the_first(self) -> None:
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")
        second = _nic("srv-web", "eno1", "dc1-access-leaf-01", "Ethernet2")["cable"]["node"]["endpoints"]["edges"][1]
        nic["cable"]["node"]["endpoints"]["edges"].append(second)
        coverage = _coverage(_placed_roots(web=[_server("srv-web", nic)]))
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {WEB: ["srv-web eno1: cable has 2 far ends"]}

    def test_an_endpoint_without_an_id_is_no_far_end(self) -> None:
        """An endpoint the query's inline fragment did not match comes back empty: it is neither a far end nor
        a reason to ignore the real one."""
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")
        nic["cable"]["node"]["endpoints"]["edges"].append({"node": {}})
        coverage = _coverage(_placed_roots(web=[_server("srv-web", nic)]))
        assert _claim(WEB, "dc1-leaf-01", "Ethernet10") in coverage.claims
        assert coverage.blockers == {}
        lonely = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")
        lonely["cable"]["node"]["endpoints"]["edges"][1] = {"node": {}}
        assert _coverage(_placed_roots(web=[_server("srv-web", lonely)])).blockers == {
            WEB: ["srv-web eno1: cable has 0 far ends"]
        }

    def test_an_instance_only_on_another_data_center_is_not_this_ones(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "bl-dc12-01", "Ethernet5"))]
        coverage = _coverage(_placed_roots(web=web))
        assert not [claim for claim in coverage.claims if claim.group == WEB]
        assert coverage.blockers == {}

    def test_a_far_end_that_is_a_vtep_of_no_data_center_blocks(self) -> None:
        """A leaf below the VTEP or a firewall cannot classify the port: dc1-spine-01 is no VTEP."""
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-spine-01", "Ethernet1"))]
        coverage = _coverage(_placed_roots(web=web))
        assert coverage.blockers == {WEB: ["srv-web eno1 is cabled to dc1-spine-01, which is a VTEP of no data center"]}

    def test_a_nic_cabled_to_something_that_is_no_switch_port_blocks(self) -> None:
        nic = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20")
        far = nic["cable"]["node"]["endpoints"]["edges"][1]["node"]
        del far["name"], far["device"]
        coverage = _coverage(_placed_roots(web=[_server("srv-web", nic)]))
        assert coverage.blockers == {WEB: ["srv-web eno1 is cabled to something that is not a switch port"]}

    def test_an_instance_with_no_hosting_device_blocks(self) -> None:
        vm = _vm("vm-web", "ignored", _nic("ignored", "eno1", "dc1-leaf-01", "Ethernet20"))
        vm["hosting_device"] = _node(None)
        assert _coverage(_placed_roots(web=[vm])).blockers == {WEB: ["instance vm-web has no hosting device"]}

    def test_a_cloud_instance_blocks(self) -> None:
        cloud = {"id": "inst-c", "__typename": "CloudInstance", "name": _v("i-0abc")}
        assert _coverage(_placed_roots(web=[cloud])).blockers == {
            WEB: ["instance i-0abc (CloudInstance) is not an on-prem device"]
        }

    @pytest.mark.parametrize("nics", [[], [None]])
    def test_an_instance_not_cabled_to_a_vtep_blocks(self, nics: list[None]) -> None:
        """No NIC at all, or a NIC with no cable: where the instance sits is not known."""
        found = [_nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet20") for _ in nics]
        for nic in found:
            nic["cable"] = _node(None)
        assert _coverage(_placed_roots(web=[_server("srv-web", *found)])).blockers == {
            WEB: ["instance srv-web (host srv-web) is not cabled to a VTEP"]
        }

    def test_a_port_two_components_of_one_segment_land_on_is_claimed_by_neither(self) -> None:
        shared = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet11")
        coverage = _coverage(_placed_roots(web=[_server("srv-web", shared)]))
        assert not [claim for claim in coverage.claims if claim.port == "Ethernet11"]
        text = (
            "port Ethernet11 of dc1-leaf-01 is shared by components db_pay_demo_local, web_pay_demo_local of one "
            "segment (an interface selector cannot tell them apart)"
        )
        assert coverage.blockers[WEB] == [text]
        assert coverage.blockers[DB] == [text]

    def test_the_same_port_in_two_segments_is_two_claims(self) -> None:
        """api and backend share a trunk port in different segments: the selector names the segment's VLAN."""
        raw = _raw()
        raw["SecurityTag"]["edges"].append(
            {
                "node": _gp_tag(
                    "tag-app",
                    60,
                    "app.app.demo.local",
                    "seg-app-np",
                    [_server("srv-app", _nic("srv-app", "eno1", "dc1-leaf-01", "Ethernet10", ("app-np",)))],
                )
            }
        )
        roots = _roots(raw)
        gpo_segment(roots, "seg-app-np")["isolation_mode"] = "microsegmented"
        coverage = _coverage(roots)
        assert ComponentClaim("app.app.demo.local", "seg-app-np", "dc1-leaf-01", "Ethernet10") in coverage.claims
        assert _claim(WEB, "dc1-leaf-01", "Ethernet10") in coverage.claims
        assert coverage.blockers == {}

    def test_a_component_without_an_instance_has_nothing_to_classify(self) -> None:
        coverage = _coverage(_placed_roots(web=[], db=[]))
        assert coverage.claims == []
        assert coverage.blockers == {}

    def test_the_component_groups_of_a_remote_segment_are_not_this_data_centers(self) -> None:
        """pay-p active only in DC12: its components (even unplaced ones) are no local blocker."""
        web = [_server("srv-web")]
        roots = _placed_roots(web=web)
        gpo_segment(roots, "seg-pay-p")["segment_deployments"] = [
            {"vni": 10210, "deployment": {"id": "dc12", "name": "DC12"}}
        ]
        coverage = _coverage(roots)
        assert coverage.claims == []
        assert coverage.blockers == {}

    def test_a_segment_that_is_not_microsegmented_has_no_component_groups_to_classify(self) -> None:
        roots = _placed_roots(web=[_server("srv-lost")])
        gpo_segment(roots, "seg-pay-p")["isolation_mode"] = "normal"
        coverage = _coverage(roots)
        assert coverage.claims == []
        assert coverage.blockers == {}

    def test_only_the_components_of_the_tags_segment_count(self) -> None:
        """One tag naming components of two segments: the one in the other (here unplaced) segment is not pay-p's."""
        raw = _raw()
        web_tag = raw["SecurityTag"]["edges"][0]["node"]
        web_tag["components"]["edges"].append(
            {
                "node": {
                    "fqdn": _v("other.app.demo.local"),
                    "network_segment": _node({"id": "seg-app-np"}),
                    "instances": _edges([_server("srv-lost")]),
                }
            }
        )
        assert _coverage(_roots(raw)).blockers == {}

    def test_an_instance_that_sits_nowhere_blocks_every_data_center_of_its_segment(self) -> None:
        roots = _placed_roots(web=[_server("srv-web")])
        gpo_segment(roots, "seg-pay-p")["segment_deployments"] = [
            {"vni": 10210, "deployment": {"id": "dc10", "name": "DC10"}},
            {"vni": 10210, "deployment": {"id": "dc12", "name": "DC12"}},
        ]
        for dc_id in ("dc10", "dc12"):
            assert WEB in _coverage(roots, dc_id).blockers

    def test_the_order_of_instances_nics_and_components_does_not_matter(self) -> None:
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10", PAY_P),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet2", PAY_P),
                _nic("srv-web", "eno3", "dc1-spine-01", "Ethernet1", PAY_P),
            ),
            _server("srv-web-2", _nic("srv-web-2", "eno1", "dc1-leaf-01", "Ethernet10", PAY_P)),
        ]
        expected = _coverage(_placed_roots(web=web))
        assert expected.claims and expected.blockers
        for seed in range(8):
            shuffled = copy.deepcopy(web)
            rng = random.Random(seed)
            rng.shuffle(shuffled)
            for instance in shuffled:
                rng.shuffle(instance["interfaces"]["edges"])
            roots = _placed_roots(web=shuffled)
            rng.shuffle(roots["SecurityTag"])
            assert _coverage(roots) == expected

    def test_coverage_is_read_from_the_cleaned_query_response(self) -> None:
        """clean_data keeps the typename, turns a null port-channel into None, and the cable's near end (the NIC
        itself, whose device is the host, no VTEP) is skipped: nothing here blocks."""
        raw = _raw()
        nic = raw["SecurityTag"]["edges"][1]["node"]["components"]["edges"][0]["node"]["instances"]["edges"][0]["node"]
        cleaned = clean_data(nic)
        assert cleaned["typename"] == "DcimVirtualDevice"
        nic_cleaned = cleaned["hosting_device"]["interfaces"][0]
        assert nic_cleaned["cable"]["endpoints"][0]["lag"] is None
        assert nic_cleaned["cable"]["endpoints"][0]["id"] == nic_cleaned["id"]
        assert _coverage(_roots(raw)).blockers == {}


class TestComponentSelectors:
    """The vxlan_gpo device view: the interface selectors of the owning VTEP, and the gate."""

    @staticmethod
    def _groups(view: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        assert view is not None
        return {group["name"]: group for group in view["groups"]}

    def test_the_selectors_are_rendered_on_the_vtep_that_owns_the_port_only(self) -> None:
        roots = _roots()
        access = _view(roots, **_device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",)))
        border = _view(roots, device_name="dc1-border-leaf-01", role="border-leaf", interfaces=[])
        leaf = self._groups(_view(roots))
        assert leaf["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet10"}]
        assert leaf["db_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]
        for view in (access, border):
            assert all(group["interfaces"] == [] for group in self._groups(view).values())

    def test_segment_groups_keep_their_vlan_selector_and_no_interfaces(self) -> None:
        groups = self._groups(_view(_roots()))
        assert {name: groups[name]["vlan_id"] for name in ("web-np", "pay-p")} == {"web-np": 110, "pay-p": 210}
        assert all(groups[name]["interfaces"] == [] for name in ("web-np", "app-np", "db-np", "pay-p", "portal-p"))

    def test_both_vpc_peers_render_their_own_port(self) -> None:
        """One NIC per peer (not bonded): each peer classifies the port of its own cable."""
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet11"),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet12"),
            )
        ]
        roots = _placed_roots(web=web, db=[])
        peer = _device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",), {"Ethernet12": PAY_P})
        leaf_view = _view(roots)
        peer_view = _view(roots, **peer)
        assert self._groups(leaf_view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]
        assert self._groups(peer_view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet12"}]
        assert leaf_view is not None and peer_view is not None
        assert leaf_view["vrfs"] == peer_view["vrfs"], "the verdict is the data center's, the same on both"

    def test_a_port_channel_is_the_selectors_interface(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet30", lag="port-channel30"))]
        view = _view(_placed_roots(web=web), interfaces=self._with_port_channel())
        assert self._groups(view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "port-channel30"}]
        assert _prod_reason(view) is None

    @staticmethod
    def _with_port_channel() -> list[dict[str, Any]]:
        return [
            *_leaf_interfaces(),
            {"name": "port-channel30", "typename": "DcimLAGInterface", "segments": [{"id": "seg-pay-p"}]},
        ]

    def test_ports_are_sorted_naturally(self) -> None:
        """Ethernet10 sorts after Ethernet2 (and Ethernet11 after both), not alphabetically."""
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet11"),
                _nic("srv-web", "eno2", "dc1-leaf-01", "Ethernet2"),
                _nic("srv-web", "eno3", "dc1-leaf-01", "Ethernet10"),
            )
        ]
        interfaces = [*_leaf_interfaces(), {"name": "Ethernet2", "segments": [{"id": "seg-pay-p"}]}]
        view = _view(_placed_roots(web=web, db=[]), interfaces=interfaces)
        assert [port["name"] for port in self._groups(view)["web_pay_demo_local"]["interfaces"]] == [
            "Ethernet2",
            "Ethernet10",
            "Ethernet11",
        ]

    def test_the_view_is_deterministic(self) -> None:
        roots = _roots()
        assert json.dumps(_view(roots), sort_keys=True) == json.dumps(_view(copy.deepcopy(roots)), sort_keys=True)

    def test_selectors_are_rendered_even_while_the_vrf_is_unenforced(self) -> None:
        web = [_server("srv-web", _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10")), _server("srv-lost")]
        view = _view(_placed_roots(web=web))
        assert _prod_reason(view) == (
            "component group web_pay_demo_local: instance srv-lost (host srv-lost) is not cabled to a VTEP"
        )
        assert self._groups(view)["web_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet10"}]
        assert self._groups(view)["db_pay_demo_local"]["interfaces"] == [{"vlan_id": 210, "name": "Ethernet11"}]


class TestComponentGate:
    """PROD is enforced only with no coverage blocker and no per-device reason; each cause is named."""

    def test_a_vrf_with_every_named_component_classified_is_enforced(self) -> None:
        view = _view(_roots())
        assert view is not None
        assert view["vrfs"]["PROD"]["unenforced"] is None
        assert view["vrfs"]["PROD"]["enforce_tag"] == 0

    @pytest.mark.parametrize(
        ("web", "expected"),
        [
            ([_vm("vm", "h", _nic("h", "eno1", "dc1-leaf-01", "Ethernet10"))], None),
            (
                [_server("srv-lost")],
                "component group web_pay_demo_local: instance srv-lost (host srv-lost) is not cabled to a VTEP",
            ),
            (
                [{"id": "c", "__typename": "CloudInstance", "name": _v("i-1")}],
                "component group web_pay_demo_local: instance i-1 (CloudInstance) is not an on-prem device",
            ),
            (
                [_server("srv", _nic("srv", "eno1", "dc1-spine-01", "Ethernet1"))],
                "component group web_pay_demo_local: srv eno1 is cabled to dc1-spine-01, which is a VTEP of no data center",
            ),
            (
                [_server("srv", _nic("srv", "eno1", "dc1-leaf-01", "Ethernet20", far_role="lag"))],
                "component group web_pay_demo_local: srv eno1 reaches dc1-leaf-01 Ethernet20, a LAG member with no port-channel",
            ),
        ],
    )
    def test_each_blocker_unenforces_the_vrf_and_is_named_in_the_reason(
        self, web: list[dict], expected: str | None
    ) -> None:
        view = _view(_placed_roots(web=web))
        assert _prod_reason(view) == expected
        assert view is not None
        assert (view["vrfs"]["PROD"]["enforce_tag"] is None) is (expected is not None)

    def test_a_shared_port_unenforces_the_vrf_and_names_both_components(self) -> None:
        view = _view(_placed_roots(web=[_server("srv", _nic("srv", "eno1", "dc1-leaf-01", "Ethernet11"))]))
        reason = _prod_reason(view)
        assert reason is not None
        assert reason.count("is shared by components db_pay_demo_local, web_pay_demo_local of one segment") == 2
        assert reason.startswith("component group db_pay_demo_local: port Ethernet11 of dc1-leaf-01")

    def test_a_claimed_port_the_device_does_not_have_unenforces_it(self) -> None:
        """Inconsistent data (or an unconverged generator run): fail closed rather than name a missing port."""
        interfaces = [item for item in _leaf_interfaces() if item["name"] != "Ethernet11"]
        reason = _prod_reason(_view(_roots(), interfaces=interfaces))
        assert (
            reason
            == "component group db_pay_demo_local: port Ethernet11 is not an interface of dc1-leaf-01 tagged with the segment"
        )

    def test_a_claimed_port_not_tagged_with_the_segment_unenforces_it(self) -> None:
        interfaces = [{**item, "segments": []} if item["name"] == "Ethernet11" else item for item in _leaf_interfaces()]
        assert "port Ethernet11 is not an interface of dc1-leaf-01 tagged with the segment" in (
            _prod_reason(_view(_roots(), interfaces=interfaces)) or ""
        )

    def test_a_segment_without_a_vlan_on_the_device_unenforces_it(self) -> None:
        activations = [item for item in LEAF_ACTIVATIONS if item["segment"]["id"] != "seg-pay-p"]
        reason = _prod_reason(_view(_roots(), activations=activations))
        assert reason is not None
        assert "the segment has no VLAN on dc1-leaf-01, so port Ethernet10 cannot be matched" in reason

    def test_components_with_no_instance_in_the_data_center_do_not_block(self) -> None:
        """DC12's instances, a component with none, and a remote segment's group are no blockers (AC4)."""
        view = _view(_placed_roots(web=[], db=[_vm("vm", "h", _nic("h", "eno1", "bl-dc12-01", "Ethernet5"))]))
        assert _prod_reason(view) is None

    def test_the_verdict_of_the_component_part_is_the_same_on_every_vtep_of_the_data_center(self) -> None:
        roots = _placed_roots(web=[_server("srv-lost")])
        reasons = {
            _prod_reason(_view(roots)),
            _prod_reason(_view(roots, **_device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",)))),
            _prod_reason(_view(roots, device_name="dc1-border-leaf-01", role="border-leaf", interfaces=[])),
        }
        assert len(reasons) == 1
        assert reasons != {None}

    def test_a_claimed_port_this_device_lacks_unenforces_it_here_while_its_vpc_peer_enforces(self) -> None:
        """The coverage part of the verdict is the data center's, the per-device part is not: the peer that lacks
        a claimed port is left unenforced, the one that has all of its own ports enforces (module docstring)."""
        web = [
            _server(
                "srv-web",
                _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10"),
                _nic("srv-web", "eno2", "dc1-access-leaf-01", "Ethernet12"),
            )
        ]
        roots = _placed_roots(web=web)
        assert _coverage(roots).blockers == {}
        peer = _device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",))  # no Ethernet12
        assert _prod_reason(_view(roots)) is None
        assert _prod_reason(_view(roots, **peer)) == (
            "component group web_pay_demo_local: port Ethernet12 is not an interface of dc1-access-leaf-01 "
            "tagged with the segment"
        )

    def test_an_enforced_vrf_relying_on_component_ports_lists_them_on_every_vtep(self) -> None:
        roots = _roots()
        views = [
            _view(roots),
            _view(roots, **_device_inputs("dc1-access-leaf-01", "access-leaf", ("pay-p",))),
            _view(roots, device_name="dc1-border-leaf-01", role="border-leaf", interfaces=[]),
        ]
        for view in views:
            assert view is not None
            assert view["vrfs"]["PROD"]["unenforced"] is None
            assert view["vrfs"]["PROD"]["classified_ports"] == "dc1-leaf-01 Ethernet10, dc1-leaf-01 Ethernet11"
            assert view["vrfs"]["NONPROD"]["classified_ports"] is None, "no component group, nothing to disclose"

    def test_the_listed_ports_are_sorted_naturally_and_bounded(self) -> None:
        nics = [_nic("srv-web", f"eno{n}", "dc1-leaf-01", f"Ethernet{n}") for n in range(1, 11)]
        view = _view(_placed_roots(web=[_server("srv-web", *nics)]))
        assert view is not None
        listed = view["vrfs"]["PROD"]["classified_ports"]
        assert listed == ", ".join(f"dc1-leaf-01 Ethernet{n}" for n in range(1, 9)) + " and 3 more"

    def test_the_rendered_enforce_line_says_what_it_relies_on_and_an_unenforced_vrf_does_not(self) -> None:
        rendered = run_transform(Leaf, _raw())
        lines = rendered.splitlines()
        enforce = [i for i, line in enumerate(lines) if "security enforce tag" in line][-1]  # PROD, after NONPROD
        assert lines[enforce - 1].strip() == (
            "! documented-not-lab-verified: enforcing this VRF relies on native-VLAN frames being classified by "
            "match vlan N interface P and on vPC peer-link behaviour (NX-OS 10.6(1)F guide); "
            "ports: dc1-leaf-01 Ethernet10, dc1-leaf-01 Ethernet11"
        )
        assert rendered.count("relies on native-VLAN frames") == 1, "NONPROD names no component group"
        raw = _raw()
        web_tag = next(edge["node"] for edge in raw["SecurityTag"]["edges"] if edge["node"]["id"] == "tag-pay-web")
        web_tag["components"]["edges"][0]["node"]["instances"] = _edges([_server("srv-lost")])
        blocked = run_transform(Leaf, raw)
        assert "! unenforced: component group web_pay_demo_local" in blocked
        assert "relies on native-VLAN frames" not in blocked

    def test_a_name_with_a_newline_cannot_leave_the_comment(self) -> None:
        """The reason is rendered as one `! unenforced:` comment line: whitespace in a name collapses."""
        reason = _prod_reason(_view(_placed_roots(web=[_server("srv\nlost")])))
        assert reason is not None
        assert "\n" not in reason and "srv lost" in reason

    def test_a_blocker_of_a_group_no_contract_names_does_not_unenforce_the_vrf(self) -> None:
        """Only the component groups a VRF's contracts name are its business: an orphan tag's is not."""
        raw = _raw()
        raw["SecurityTag"]["edges"].append(
            {"node": _gp_tag("tag-orphan", 70, "orphan.pay.demo.local", "seg-pay-p", [_server("srv-lost")])}
        )
        roots = _roots(raw)
        assert _coverage(roots).blockers, "the orphan group does have a blocker"
        assert _prod_reason(_view(roots)) is None


class TestHostTrunks:
    """PR2: a host port a component selector names is a trunk, by the one mode rule (get_interfaces)."""

    @staticmethod
    def _interfaces(native_vlans: dict[str, int] | None) -> dict[str, dict[str, Any]]:
        raw = _raw()
        transform = Leaf.__new__(Leaf)
        device, _roots_, _platform = transform._device_and_platform(raw)
        activations = transform._device_activations(device)
        found = get_interfaces(
            device["interfaces"], activations=activations, device_name="dc1-leaf-01", native_vlans=native_vlans
        )
        return {item["name"]: item for item in found}

    def test_an_access_port_becomes_a_trunk_with_the_claimed_vlan_native(self) -> None:
        found = self._interfaces({"Ethernet11": 210})
        assert found["Ethernet11"]["mode"] == "trunk"
        assert found["Ethernet11"]["native_vlan"] == 210
        assert found["Ethernet11"]["vlans"] == [210]

    def test_without_claims_the_port_stays_access_and_no_key_is_added(self) -> None:
        for native in (None, {}, {"Ethernet12": 210}):
            found = self._interfaces(native)
            assert found["Ethernet11"]["mode"] is None
            assert "native_vlan" not in found["Ethernet11"]

    def test_a_port_that_is_already_a_trunk_is_left_as_it_is(self) -> None:
        found = self._interfaces({"Ethernet10": 210})
        assert found["Ethernet10"]["mode"] == "trunk"
        assert "native_vlan" not in found["Ethernet10"]
        assert found["Ethernet10"]["vlans"] == [110, 120, 210, 220]

    def test_a_claim_for_another_vlan_than_the_ports_changes_nothing(self) -> None:
        found = self._interfaces({"Ethernet11": 999})
        assert found["Ethernet11"]["mode"] is None
        assert "native_vlan" not in found["Ethernet11"]

    def test_the_rendered_host_port_is_an_edge_trunk_with_a_native_vlan(self) -> None:
        rendered = run_transform(Leaf, _raw())
        stanza = rendered.split("\ninterface Ethernet11\n")[1].split("\n!\n")[0].splitlines()
        assert [line.strip() for line in stanza if line.strip() and not line.strip().startswith("!")] == [
            "no shutdown",
            "switchport",
            "switchport mode trunk",
            "switchport trunk native vlan 210",
            "switchport trunk allowed vlan 210",
            "spanning-tree port type edge trunk",
        ]
        assert any("documented-not-lab-verified" in line for line in stanza)
        assert "  match vlan 210 interface Ethernet11" in rendered
        assert "  match vlan 210 interface Ethernet10" in rendered

    def test_a_claimed_port_channel_renders_as_an_edge_trunk_with_a_native_vlan(self) -> None:
        """A bonded host: the port-channel (tagged with the segment) is the selector's interface and the trunk."""
        raw = _raw()
        node = raw["DcimDevice"]["edges"][0]["node"]
        node["interfaces"]["edges"].append(
            {
                "node": _make_interface(
                    name="port-channel30",
                    device_name="dc1-leaf-01",
                    role="server",
                    typename="DcimLAGInterface",
                    segments=_gpo_segment_nodes("dc1-leaf-01", PAY_P),
                )
            }
        )
        web_tag = next(edge["node"] for edge in raw["SecurityTag"]["edges"] if edge["node"]["id"] == "tag-pay-web")
        member = _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet30", lag="port-channel30")
        web_tag["components"]["edges"][0]["node"]["instances"] = _edges([_server("srv-web", member)])
        rendered = run_transform(Leaf, raw)
        assert "  match vlan 210 interface port-channel30" in rendered
        assert "  match vlan 210 interface Ethernet30" not in rendered
        stanza = rendered.split("\ninterface port-channel30\n")[1].split("\n!\n")[0].splitlines()
        assert [line.strip() for line in stanza if line.strip() and not line.strip().startswith("!")] == [
            "no shutdown",
            "switchport",
            "switchport mode trunk",
            "switchport trunk native vlan 210",
            "switchport trunk allowed vlan 210",
            "spanning-tree port type edge trunk",
        ]

    def test_the_existing_trunk_gets_no_native_vlan_and_nothing_renders_the_global_native_tag(self) -> None:
        rendered = run_transform(Leaf, _raw())
        trunk = rendered.split("\ninterface Ethernet10\n")[1].split("\n!\n")[0]
        assert "native vlan" not in trunk and "spanning-tree" not in trunk
        assert "switchport trunk allowed vlan 110,120,210,220" in trunk
        assert "vlan dot1q tag native" not in rendered

    def test_an_unclaimed_access_port_stays_access(self) -> None:
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=LEAF_SEGMENTS,
            host_ports={"Ethernet11": PAY_P, "Ethernet12": PAY_P},
        )
        stanza = run_transform(Leaf, raw).split("interface Ethernet12\n")[1].split("\n!\n")[0]
        assert "switchport mode access" in stanza
        assert "switchport access vlan 210" in stanza
        assert "native vlan" not in stanza

    @staticmethod
    def _context(raw: dict, root_dir: Path) -> tuple[dict[str, Any], MagicMock]:
        """The template context of ``raw`` (``_template_context``, what transform() renders) and the
        get_interfaces spy of that one call."""
        transform = Leaf.__new__(Leaf)
        transform.root_directory = str(root_dir)
        device, roots, platform = transform._device_and_platform(with_vrf_objects(transform, raw))
        device["segment_deployments"] = transform._device_activations(device)
        with patch("transforms.common.get_interfaces", wraps=get_interfaces) as spy:
            return transform._template_context(device, platform or "", roots), spy

    def test_the_transform_hands_the_templates_the_trunked_interfaces_only_for_claimed_ports(
        self, root_dir: Path
    ) -> None:
        config, _spy = self._context(_raw(), root_dir)
        by_name = {item["name"]: item for item in config["interfaces"]}
        assert by_name["Ethernet11"]["native_vlan"] == 210
        assert "native_vlan" not in by_name["Ethernet10"]
        assert not any("native_vlan" in item for name, item in by_name.items() if name not in ("Ethernet11",))

    def test_a_vtep_owning_a_claimed_port_builds_its_interfaces_once_with_the_native_vlans(
        self, root_dir: Path
    ) -> None:
        config, spy = self._context(_raw(), root_dir)
        spy.assert_called_once()
        # Ethernet10 (the trunk of the web component) is named too; get_interfaces leaves a trunk as it is.
        assert spy.call_args.kwargs["native_vlans"] == {"Ethernet10": 210, "Ethernet11": 210}
        assert config["group_policy"]["groups"], "the claimed port comes from the group policy of the same context"

    def test_a_vtep_without_a_claimed_port_builds_its_interfaces_once_without_native_vlans(
        self, root_dir: Path
    ) -> None:
        """An enablement-only VTEP (no tenant VRF) and a device outside a reference fabric."""
        for raw in (
            build_gpo_device_data(device_name="dc1-leaf-01", role="leaf", segments=()),
            build_gpo_device_data(
                device_name="dc1-leaf-01", role="leaf", segments=LEAF_SEGMENTS, leaf_type="N9K-C9336C-FX2"
            ),
        ):
            _config, spy = self._context(raw, root_dir)
            spy.assert_called_once()
            assert spy.call_args.kwargs["native_vlans"] is None

    def test_the_context_keeps_the_extra_config_over_the_base_config_and_the_base_loopback(
        self, root_dir: Path
    ) -> None:
        """The same keys as the two parts merged the old way (base first, then extra on top)."""
        transform = Leaf.__new__(Leaf)
        transform.root_directory = str(root_dir)
        device, roots, platform = transform._device_and_platform(copy.deepcopy(_raw()))
        device["segment_deployments"] = transform._device_activations(device)
        config = transform._template_context(device, platform or "", roots)
        base = transform._build_config(device, platform or "")
        extra = transform._extra_config(device, platform or "", extra_roots=roots)
        assert config.keys() == {**base, **extra}.keys()
        assert config["loopback_name"] == base["loopback_name"]
        assert {key: config[key] for key in extra} == extra
        assert [i["name"] for i in config["interfaces"]] == [i["name"] for i in base["interfaces"]]

    def test_a_non_reference_device_is_not_touched(self) -> None:
        """No claim, no group policy: get_interfaces is called once, without native VLANs."""
        raw = build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=LEAF_SEGMENTS,
            host_ports={"Ethernet11": PAY_P},
            leaf_type="N9K-C9336C-FX2",
        )
        rendered = run_transform(Leaf, raw)
        assert "native vlan" not in rendered and "security-group" not in rendered
        assert "switchport mode access" in rendered
