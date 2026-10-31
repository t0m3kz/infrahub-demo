"""transforms/helpers/bgp_facts.py: the model facts of the route protection, read off cleaned query data."""

from __future__ import annotations

from typing import Any

from transforms.helpers.bgp_facts import (
    annotate_bgp_policy,
    build_device,
    build_fabric,
    device_addrs,
    loopback_ranges,
    routing_filter_enforce,
)
from utils.bgp_expectation import Entry, host_entry, range_entry

POOL = "10.250.0.0/24"
VIP = "fd00:2500:ff::10/128"


def _dc(*, enforce: bool | None = True, pool: tuple[str, ...] = (POOL,)) -> dict[str, Any]:
    """A cleaned hosting deployment as the EvpnRtAsOnSegmentHostingFields fragment returns it."""
    dc: dict[str, Any] = {"loopback_pool": {"resources": [{"prefix": p} for p in pool]}}
    if enforce is not None:
        dc["routing_filter_enforce"] = enforce
    return dc


def _pod(dc: dict[str, Any]) -> dict[str, Any]:
    """A leaf's deployment: the pod, with the DC one `parent` hop above it (no attribute of its own)."""
    return {"name": "POD-1", "parent": dc}


def _leaf(deployment: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "name": "leaf1",
        "role": "leaf",
        "deployment": deployment,
        "interfaces": [
            {"name": "loopback0", "role": "loopback", "ip_address": {"address": "10.250.0.11/32"}},
            {"name": "Ethernet1", "role": "uplink", "ip_address": {"address": "10.9.0.1/31"}},
        ],
    }


def _underlay_config() -> dict[str, Any]:
    session = {
        "name": "u1",
        "ttl": 1,
        "peering_role": "underlay",
        "remote_device": "spine1",
        "address_families": ["ipv4"],
    }
    return {"bgp": [{"sessions": [session]}]}


class TestHosting:
    def test_the_pod_device_reads_its_dc_one_hop_up(self) -> None:
        """A pod has no pool or switch of its own: the first deployment carrying the attribute is the DC."""
        deployment = _pod(_dc())
        assert routing_filter_enforce(deployment) is True
        assert loopback_ranges(deployment) == (POOL,)

    def test_a_query_without_the_attribute_stages_and_has_no_pool(self) -> None:
        """A device query that does not select the fragment fields: staged, nothing derivable."""
        assert routing_filter_enforce(_pod({"name": "DC"})) is False
        assert loopback_ranges(_pod({"name": "DC"})) == ()
        assert routing_filter_enforce(None) is False

    def test_the_switch_defaults_to_staged(self) -> None:
        """routing_filter_enforce false (the schema default) stages."""
        assert routing_filter_enforce(_dc(enforce=False)) is False

    def test_overlapping_pool_prefixes_count_once(self) -> None:
        """A pod pool inside the DC pool must not double the capacity the limit is sized from."""
        assert loopback_ranges(_dc(pool=("10.250.0.0/24", "10.250.0.0/25", "fd00::/120"))) == (POOL, "fd00::/120")


class TestDeviceAddresses:
    def test_loopbacks_and_vips_are_read_by_role(self) -> None:
        """Only the loopback roles and the multisite VIP count; a routed port address does not."""
        addrs = device_addrs(
            [
                {"role": "loopback-vtep", "ip_address": {"address": "fd00:1::1/128"}},
                {"role": "multisite-vip", "ip_address": {"address": VIP}},
                {"role": "uplink", "ip_address": {"address": "10.0.0.1/31"}},
                {"role": "loopback", "ip_address": None},
            ]
        )
        assert addrs.loopbacks == {"fd00:1::1/128"}
        assert addrs.vips == {VIP}

    def test_the_vip_on_two_edges_is_one_address_and_both_carry_it(self) -> None:
        """The FR2 edge pair share one VIP: the fabric holds it once, each far end holds it."""
        roots = {
            "DcimVirtualInterface": [
                {
                    "ip_address": {"address": VIP},
                    "device": {
                        "name": name,
                        "role": "edge",
                        "interfaces": [{"role": "loopback-vtep", "ip_address": {"address": loop}}],
                    },
                }
                for name, loop in (("eg-fr201", "fd00:2::1/128"), ("eg-fr202", "fd00:2::2/128"))
            ]
        }
        fabric = build_fabric(_leaf(None), roots)
        assert fabric.vips == {VIP}
        assert fabric.devices["eg-fr202"].addrs.loopbacks == {"fd00:2::2/128"}
        assert fabric.devices["eg-fr201"].addrs.vips == {VIP}

    def test_a_query_without_the_vip_root_has_no_far_ends(self) -> None:
        """No root: no VIP and no far end, so the pair families are not derivable."""
        fabric = build_fabric(_leaf(None), {})
        assert fabric.vips == frozenset()
        assert fabric.devices == {}

    def test_external_policies_become_exact_sets(self) -> None:
        """The edge's external_policies (in/out lists by sub-interface) reach the fabric as ExternalPolicy."""
        fabric = build_fabric(
            _leaf(None), {}, external_policies={"Eth1.100": {"in": ["10.0.0.0/24"], "out": ["10.1.0.0/24"]}}
        )
        assert fabric.external["Eth1.100"].inbound == {"10.0.0.0/24"}
        assert fabric.external["Eth1.100"].outbound == {"10.1.0.0/24"}

    def test_the_device_is_named_with_its_role_and_own_addresses(self) -> None:
        """build_device carries what the underlay outbound set is derived from."""
        device = build_device(_leaf(None))
        assert (device.name, device.role) == ("leaf1", "leaf")
        assert device.addrs.loopbacks == {"10.250.0.11/32"}


class TestAnnotate:
    def test_an_enforcing_dc_gets_the_derived_limit_enforced(self) -> None:
        """Underlay limit = the pool capacity (256), no warning-only once routing_filter_enforce is true."""
        config = _underlay_config()
        annotate_bgp_policy(config, _leaf(_pod(_dc(enforce=True))), {})
        policy = config["bgp"][0]["sessions"][0]["policy"]
        assert policy["max"] == {"ipv4": {"limit": 256, "warning_only": False}}
        assert policy["max_total"] == {"limit": 256, "warning_only": False}
        assert policy["enforced"] is True

    def test_a_staged_dc_only_warns(self) -> None:
        """The same limit, warning-only: it can log but never reset a session."""
        config = _underlay_config()
        annotate_bgp_policy(config, _leaf(_pod(_dc(enforce=False))), {})
        policy = config["bgp"][0]["sessions"][0]["policy"]
        assert policy["max"]["ipv4"] == {"limit": 256, "warning_only": True}
        assert policy["enforced"] is False

    def test_no_pool_means_no_limit_rather_than_a_guess(self) -> None:
        """A leaf whose query carries no pool renders unprotected (and the check reports it)."""
        config = _underlay_config()
        annotate_bgp_policy(config, _leaf(None), {})
        policy = config["bgp"][0]["sessions"][0]["policy"]
        assert policy["max"] == {}
        assert policy["max_total"] is None

    def test_a_device_without_sessions_is_left_alone(self) -> None:
        """No session, nothing to derive and no facts to build."""
        config: dict[str, Any] = {"bgp": [{"sessions": []}]}
        annotate_bgp_policy(config, _leaf(None), {})
        assert config == {"bgp": [{"sessions": []}]}

    def test_an_sdwan_far_end_is_left_out(self) -> None:
        """A session to a device named in `unmanaged` (an office SD-WAN edge) gets no limit."""
        config = _underlay_config()
        annotate_bgp_policy(config, _leaf(_pod(_dc())), {}, unmanaged=frozenset({"spine1"}))
        assert config["bgp"][0]["sessions"][0]["policy"]["max"] == {}


def test_entries_of_a_range_and_a_host_stay_distinct() -> None:
    """The pool range and a host route are different prefix-list lines even inside the range."""
    assert range_entry(POOL) != host_entry("10.250.0.11/32")
    assert Entry(POOL, 32).longest == 32
