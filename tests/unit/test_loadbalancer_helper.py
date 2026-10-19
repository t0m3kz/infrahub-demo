"""Unit tests for transforms/helpers/loadbalancer.py: contexts, legs, partitions and VIP tokens."""

from __future__ import annotations

from typing import Any

import pytest

from transforms.helpers.loadbalancer import get_loadbalancer_contexts

_NAMESPACES = {
    "PROD": {"name": "PROD", "namespace_type": "prod", "l3_vni": 60000},
    "NON-PROD": {"name": "NON-PROD", "namespace_type": "non_prod", "l3_vni": 60001},
    "INTERNET": {"name": "INTERNET", "namespace_type": "internet", "l3_vni": 60002},
}
_HA = {"name": "LB-01-LB-02-ha", "members": ["LB-01", "LB-02"]}


def _leg(device: str, name: str, address: str, namespace: str) -> dict[str, Any]:
    return {
        "typename": "DcimVirtualInterface",
        "name": name,
        "device": {"name": device, "role": "load-balancer"},
        "parent_interface": {"name": name.split(".")[0]},
        "ip_address": {"address": address, "ip_namespace": _NAMESPACES[namespace]},
    }


def _vip(**overrides: Any) -> dict[str, Any]:
    vip = {
        "hostname": "web.c001.example.com",
        "protocol": "tcp",
        "port": 443,
        "status": "active",
        "vip_ip": {"address": "100.66.96.10/32"},
        "pool_members": [{"address": "10.0.0.11/24"}, {"address": "10.0.0.10/24"}, {"address": "10.0.0.10/24"}],
        "health_checks": [],
    }
    vip.update(overrides)
    return vip


def _context(
    namespace: str = "PROD",
    *,
    vlan_id: int | None = 3100,
    base: str = "100.66.64",
    own: str = "5",
    tenant: dict[str, Any] | str | None = None,
    vips: list[dict[str, Any]] | None = None,
    device: str = "LB-01",
) -> dict[str, Any]:
    leg_vlan = (vlan_id or 0) + (200 if namespace == "NON-PROD" else 0)
    return {
        "id": f"ctx-{namespace}",
        "name": f"LB-01-LB-02-ha-{namespace}",
        "vlan_id": vlan_id,
        "tenant": tenant,
        "vip_prefix": {"prefix": "100.66.96.0/26"},
        "capabilities": [
            _leg(device, f"eth1.{leg_vlan}", f"{base}.{own}/29", namespace),
            _leg("LB-02", f"eth1.{leg_vlan}", f"{base}.6/29", namespace),
            {"typename": "DcimPhysicalInterface", "name": "Ethernet1/10", "device": {"name": "border-01"}},
        ],
        "vips": [_vip()] if vips is None else vips,
    }


def _caps(*contexts: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"typename": "ManagedLoadbalancerHA", "name": "LB-01-LB-02-ha", "contexts": list(contexts)}]


def _contexts(*contexts: dict[str, Any], platform: str = "f5_linux") -> list[dict[str, Any]]:
    return get_loadbalancer_contexts(_caps(*contexts), "LB-01", _HA, platform)


class TestLeg:
    def test_prod_leg_is_derived_from_the_exchange_transit_rules(self) -> None:
        """VLAN = context VLAN, VNI = 61000 + VLAN, .1 gateway, .4 floating, own .5 for the first member."""
        leg = _contexts(_context())[0]["leg"]
        assert leg == {
            "name": "eth1.3100",
            "parent_interface": "eth1",
            "vlan": 3100,
            "vni": 64100,
            "prefixlen": 29,
            "anycast": "100.66.64.1",
            "floating": "100.66.64.4",
            "virtual_ip": "100.66.64.4/29",
            "own_address": "100.66.64.5/29",
            "own_ip": "100.66.64.5",
        }

    def test_a_leg_whose_device_is_no_load_balancer_is_not_built(self) -> None:
        """The context's transit leg is the members' (role load-balancer, firewall.py transit_legs):
        without one the device has no leg yet and the context is left out."""
        context = _context()
        for iface in context["capabilities"]:
            iface.get("device", {}).pop("role", None)
        assert _contexts(context) == []

    def test_non_prod_leg_takes_the_non_prod_slot(self) -> None:
        """A NON-PROD context's leg is its VLAN + 200, like the firewall legs."""
        leg = _contexts(_context("NON-PROD", vlan_id=3101, base="100.66.80"))[0]["leg"]
        assert (leg["vlan"], leg["vni"], leg["anycast"]) == (3301, 64301, "100.66.80.1")

    def test_the_second_member_by_name_holds_dot_six(self) -> None:
        """The member address follows the pair's name order."""
        context = _context(device="LB-02", own="6")
        context["capabilities"] = context["capabilities"][1:]
        rendered = get_loadbalancer_contexts(_caps(context), "LB-02", _HA, "f5_linux")
        assert rendered[0]["leg"]["own_address"] == "100.66.64.6/29"

    def test_a_member_address_off_its_name_order_raises(self) -> None:
        """LB-01 on .6 would claim LB-02's self IP."""
        with pytest.raises(ValueError, match="member address by name order is 100.66.64.5"):
            _contexts(_context(own="6"))

    def test_a_leg_outside_a_tenant_vrf_raises(self) -> None:
        """An LB never leaves its tenant VRF: a leg in INTERNET is a modelling error."""
        context = _context()
        context["capabilities"][0]["ip_address"]["ip_namespace"] = _NAMESPACES["INTERNET"]
        with pytest.raises(ValueError, match="not in a tenant VRF"):
            _contexts(context)

    def test_a_context_without_a_leg_on_this_device_is_not_rendered(self) -> None:
        """Until the generator builds the leg there is no namespace, route domain or path."""
        context = _context()
        context["capabilities"] = context["capabilities"][1:]
        assert _contexts(context) == []

    def test_a_context_without_a_vlan_is_not_rendered(self) -> None:
        """The leg VLAN derives from the context VLAN; none yet means no leg yet."""
        assert _contexts(_context(vlan_id=None)) == []


class TestContexts:
    def test_no_cluster_context_renders_nothing(self) -> None:
        """A device whose cluster has no context yet gets no context section."""
        assert get_loadbalancer_contexts(_caps(), "LB-01", _HA, "f5_linux") == []
        assert get_loadbalancer_contexts([], "LB-01", None, "netscaler") == []

    def test_shared_context_partition_route_domain_and_vrf(self) -> None:
        """Shared: SHARED-<NAMESPACE>; the route domain / VRF table is the namespace l3_vni."""
        context = _contexts(_context())[0]
        assert (context["partition"], context["dedicated"], context["vrf"]) == ("SHARED-PROD", False, "PROD")
        assert (context["route_domain"], context["vrf_device"], context["vip_prefix"]) == (
            60000,
            "vrf-PROD",
            "100.66.96.0/26",
        )

    def test_dedicated_context_partition_is_org_id_and_environment(self) -> None:
        """Dedicated: <ORG_ID>-<ENV> from the tenant's owner and environment."""
        tenant = {"id": "t1", "owner": {"org_id": "c009"}, "environment": "p"}
        context = _contexts(_context(tenant=tenant))[0]
        assert (context["partition"], context["dedicated"]) == ("C009-P", True)

    def test_dedicated_tenant_without_org_id_raises(self) -> None:
        """The partition name cannot be derived without the owner's org_id."""
        with pytest.raises(ValueError, match="no org_id or environment"):
            _contexts(_context(tenant={"id": "t1", "owner": None, "environment": "p"}))

    def test_dedicated_tenant_resolved_to_its_id_only_raises(self) -> None:
        """clean_data turns an id-only tenant into a string: still dedicated, still no partition name."""
        with pytest.raises(ValueError, match="no org_id or environment"):
            _contexts(_context(tenant="t1"))

    def test_contexts_are_ordered_prod_first(self) -> None:
        """PROD before NON-PROD whatever the cluster's order."""
        rendered = _contexts(_context("NON-PROD", vlan_id=3101, base="100.66.80"), _context())
        assert [c["vrf"] for c in rendered] == ["PROD", "NON-PROD"]

    def test_two_contexts_in_one_vrf_raise(self) -> None:
        """They would share one route domain id on the device."""
        second = _context(vlan_id=3102, base="100.66.64")
        second["name"] = "other-PROD"
        with pytest.raises(ValueError, match="share a route domain"):
            _contexts(_context(), second)

    def test_unknown_platform_raises(self) -> None:
        """A platform with no load-balancer template is refused, contexts or not."""
        with pytest.raises(ValueError, match="no load-balancer template"):
            get_loadbalancer_contexts([], "LB-01", None, "cisco_nxos")


class TestVips:
    def test_members_are_plain_sorted_unique_ips(self) -> None:
        """Pool members drop the mask, are de-duplicated and sorted numerically."""
        vip = _contexts(_context())[0]["vips"][0]
        assert vip["members"] == ["10.0.0.10", "10.0.0.11"]
        assert (vip["address"], vip["name"], vip["enabled"]) == ("100.66.96.10", "web_c001_example_com_tcp_443", True)

    def test_vip_without_members_has_an_empty_pool(self) -> None:
        """A component without instances yet still renders its VIP, with no member."""
        vip = _contexts(_context(vips=[_vip(pool_members=[])]))[0]["vips"][0]
        assert vip["members"] == []

    def test_vip_without_address_raises(self) -> None:
        """A virtual server needs its destination."""
        with pytest.raises(ValueError, match="has no vip_ip"):
            _contexts(_context(vips=[_vip(vip_ip=None)]))

    def test_vips_are_sorted_by_hostname_protocol_port(self) -> None:
        """A stable order, whatever the order of the query."""
        vips = [_vip(port=8443), _vip(hostname="api.c001.example.com"), _vip()]
        rendered = _contexts(_context(vips=vips))[0]["vips"]
        assert [(v["hostname"], v["port"]) for v in rendered] == [
            ("api.c001.example.com", 443),
            ("web.c001.example.com", 443),
            ("web.c001.example.com", 8443),
        ]

    @pytest.mark.parametrize(
        ("platform", "persistence", "expected"),
        [
            ("netscaler", "none", "NONE"),
            ("netscaler", None, "NONE"),
            ("netscaler", "source_ip", "SOURCEIP"),
            ("f5_linux", "none", None),
            ("f5_linux", "source_ip", "/Common/source_addr"),
            ("haproxy_technologies_linux", "source_ip", "source_ip"),
        ],
    )
    def test_persistence_none_is_no_persistence(
        self, platform: str, persistence: str | None, expected: str | None
    ) -> None:
        """`none` (or unset) is no persistence; it used to render NetScaler SOURCEIP."""
        vip = _contexts(_context(vips=[_vip(session_persistence=persistence)]), platform=platform)[0]["vips"][0]
        assert vip["tokens"]["persist"] == expected

    @pytest.mark.parametrize(
        ("platform", "algorithm", "expected"),
        [
            ("f5_linux", None, "round-robin"),
            ("f5_linux", "least_connections", "least-connections-member"),
            ("netscaler", "ip_hash", "SOURCEIPHASH"),
            ("haproxy_technologies_linux", "random", "random"),
        ],
    )
    def test_algorithm_tokens(self, platform: str, algorithm: str | None, expected: str) -> None:
        """Each algorithm maps to the platform's own method; unset is round robin."""
        vip = _contexts(_context(vips=[_vip(load_balancing_algorithm=algorithm)]), platform=platform)[0]["vips"][0]
        assert vip["tokens"]["method"] == expected

    @pytest.mark.parametrize(("platform", "algorithm"), [("f5_linux", "ip_hash"), ("netscaler", "random")])
    def test_an_algorithm_the_platform_lacks_raises(self, platform: str, algorithm: str) -> None:
        """Refused rather than rendered as some other method."""
        with pytest.raises(ValueError, match=f"load_balancing_algorithm '{algorithm}'"):
            _contexts(_context(vips=[_vip(load_balancing_algorithm=algorithm)]), platform=platform)

    def test_application_persistence_raises(self) -> None:
        """No platform gets an application persistence rule from the model."""
        with pytest.raises(ValueError, match="session_persistence 'application'"):
            _contexts(_context(vips=[_vip(session_persistence="application")]))

    def test_cookie_persistence_on_an_l4_vip_raises(self) -> None:
        """A cookie needs the LB to parse HTTP."""
        with pytest.raises(ValueError, match="cookie persistence on an L4 VIP"):
            _contexts(_context(vips=[_vip(session_persistence="cookie")]), platform="netscaler")

    def test_haproxy_refuses_udp(self) -> None:
        """HAProxy (community) does not balance UDP."""
        with pytest.raises(ValueError, match="protocol udp"):
            _contexts(_context(vips=[_vip(protocol="udp")]), platform="haproxy_technologies_linux")

    def test_haproxy_refuses_two_health_checks(self) -> None:
        """A server line holds one check."""
        checks = [{"check": "tcp"}, {"check": "http"}]
        with pytest.raises(ValueError, match="more than one health check"):
            _contexts(_context(vips=[_vip(health_checks=checks)]), platform="haproxy_technologies_linux")

    def test_https_with_a_certificate_terminates_and_re_encrypts(self) -> None:
        """ssl_certificate set: L7, F5 client-ssl profile + serverssl, NetScaler SSL, HAProxy `ssl crt`."""
        raw = _vip(protocol="https", ssl_certificate="web-c001")
        f5 = _contexts(_context(vips=[raw]))[0]["vips"][0]
        assert f5["l7"] and f5["terminate_tls"]
        assert f5["tokens"]["client_ssl"] == "/SHARED-PROD/web_c001_example_com_https_443_clientssl"
        assert [p["name"] for p in f5["tokens"]["profiles"]] == [
            "/Common/tcp",
            "/Common/http",
            "/SHARED-PROD/web_c001_example_com_https_443_clientssl",
            "/Common/serverssl",
        ]
        assert _contexts(_context(vips=[raw]), platform="netscaler")[0]["vips"][0]["tokens"]["service_type"] == "SSL"
        haproxy = _contexts(_context(vips=[raw]), platform="haproxy_technologies_linux")[0]["vips"][0]["tokens"]
        assert (haproxy["mode"], haproxy["certificate"]) == ("http", "/etc/haproxy/certs/web-c001.pem")

    def test_https_without_a_certificate_passes_tls_through(self) -> None:
        """No certificate: L4 passthrough (NetScaler SSL_BRIDGE, HAProxy mode tcp, F5 tcp only)."""
        raw = _vip(protocol="https")
        f5 = _contexts(_context(vips=[raw]))[0]["vips"][0]
        assert not f5["l7"] and f5["tokens"]["client_ssl"] is None
        ns = _contexts(_context(vips=[raw]), platform="netscaler")[0]["vips"][0]["tokens"]
        assert ns["service_type"] == "SSL_BRIDGE"
        assert (
            _contexts(_context(vips=[raw]), platform="haproxy_technologies_linux")[0]["vips"][0]["tokens"]["mode"]
            == "tcp"
        )


class TestHealthChecks:
    def test_defaults_and_vendor_timers(self) -> None:
        """Unset rise/fall/timeout take the schema defaults; F5 timers are interval x fall + 1 and interval x rise."""
        vip = _contexts(_context(vips=[_vip(health_checks=[{"check": "http"}])]))[0]["vips"][0]
        check = vip["health_checks"][0]
        assert check == {
            "check": "http",
            "rise": 3,
            "fall": 3,
            "timeout_ms": 1000,
            "name": "web_c001_example_com_tcp_443_mon1",
            "interval_s": 5,
            "down_after_s": 16,
            "up_after_s": 15,
            "path": "/health",
            "tls": False,
            "type": "http",
        }

    def test_a_long_probe_timeout_stretches_the_interval(self) -> None:
        """A 6 s probe does not fit in the 5 s interval."""
        checks = [{"check": "ssl", "rise": 2, "fall": 4, "timeout": 6000}]
        check = _contexts(_context(vips=[_vip(health_checks=checks)]), platform="netscaler")[0]["vips"][0][
            "health_checks"
        ][0]
        assert (check["interval_s"], check["type"], check["tls"]) == (7, "HTTP", True)
