"""Unit tests for ``LoadBalancer.transform()``: on-prem contexts rendered with the real templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from transforms.config.loadbalancer import LoadBalancer

# ===========================================================================
# LoadBalancer.transform() — onprem, rendered with the real templates
# ===========================================================================

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def _v(value: object) -> dict:
    return {"value": value}


def _edges(nodes: list[dict]) -> dict:
    return {"edges": [{"node": node} for node in nodes]}


def _onprem_vip(**overrides: object) -> dict:
    """A raw (uncleaned) LoadbalancerVIP of a context."""
    vip = {
        "hostname": _v("web.c001.example.com"),
        "description": _v(None),
        "status": _v("active"),
        "protocol": _v("tcp"),
        "port": _v(443),
        "load_balancing_algorithm": _v(None),
        "session_persistence": _v("none"),
        "ssl_certificate": _v(None),
        "vip_ip": {"node": {"address": _v("100.66.96.10/32")}},
        "pool_members": _edges(
            [
                {
                    "__typename": "DcimVirtualDevice",
                    "name": _v("vm-1"),
                    "primary_address": {"node": {"address": _v("10.0.0.10/24")}},
                }
            ]
        ),
        "health_checks": _edges([]),
    }
    vip.update(overrides)
    return vip


def _onprem_data(
    platform: str = "f5_tmos",
    *,
    vips: list[dict] | None = None,
    with_context: bool = True,
    root: str = "DcimPhysicalDevice",
    lb_type: str | None = None,
) -> dict:
    """Raw query data of LB-01 (first member of LB-01/LB-02) with a shared PROD context on eth1.3100."""
    leg = {
        "__typename": "DcimVirtualInterface",
        "name": _v("eth1.3100"),
        "device": {"node": {"name": _v("LB-01"), "role": _v("load-balancer")}},
        "parent_interface": {"node": {"name": _v("eth1")}},
        "ip_address": {
            "node": {
                "address": _v("100.66.64.5/29"),
                "ip_namespace": {"node": {"name": _v("PROD"), "namespace_type": _v("prod"), "l3_vni": _v(60000)}},
            }
        },
    }
    context = {
        "id": "ctx-prod",
        "name": _v("LB-01-LB-02-ha-PROD"),
        "vlan_id": _v(3100),
        "tenant": {"node": None},
        "vip_prefix": {"node": {"prefix": _v("100.66.96.0/26")}},
        "capabilities": _edges([leg]),
        "vips": _edges([_onprem_vip()] if vips is None else vips),
    }
    ha = {
        "__typename": "ManagedLoadbalancerHA",
        "name": _v("LB-01-LB-02-ha"),
        "group_id": _v(1),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "lb_type": _v(lb_type),
        "scheme": _v(None),
        "capabilities": _edges(
            [
                {"name": _v("LB-01"), "primary_address": {"node": {"address": _v("192.168.2.1/32")}}},
                {"name": _v("LB-02"), "primary_address": {"node": {"address": _v("192.168.2.2/32")}}},
            ]
        ),
        "contexts": _edges([context] if with_context else []),
    }
    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "role": _v("management"),
            "ip_address": {"node": {"address": _v("192.168.2.1/24")}},
            "capabilities": _edges([]),
        },
        {
            "__typename": "DcimVirtualInterface",
            "name": _v("eth1.3100"),
            "role": _v("uplink"),
            "ip_address": {"node": {"address": _v("100.66.64.5/29")}},
            "parent_interface": {"node": {"name": _v("eth1")}},
            "capabilities": _edges([{"__typename": "ManagedLoadbalancerContext"}]),
        },
    ]
    device = {
        "name": _v("LB-01"),
        "role": _v("load-balancer"),
        "platform": {"node": {"name": _v(platform)}},
        "capabilities": _edges([ha]),
        "interfaces": _edges(interfaces),
    }
    # Both roots, in the query's order: the one not matching the device name is empty.
    return {"DcimPhysicalDevice": _edges([]), "DcimVirtualDevice": _edges([]), root: _edges([device])}


async def _render_onprem(data: dict) -> str:
    transform = LoadBalancer.__new__(LoadBalancer)
    transform.root_directory = _PROJECT_ROOT
    return await transform.transform(data)


class TestLoadBalancerOnpremMissingPlatform:
    @pytest.mark.asyncio
    async def test_returns_comment_when_no_platform(self) -> None:
        """A device without a platform gets the stub config."""
        data = _onprem_data()
        data["DcimPhysicalDevice"]["edges"][0]["node"]["platform"] = {"node": None}
        assert "No configuration generated" in await _render_onprem(data)


class TestLoadBalancerOnpremRoots:
    @pytest.mark.asyncio
    async def test_dedicated_virtual_device_renders_from_the_virtual_root(self) -> None:
        """The physical root is empty for a dedicated virtual LB: the first non-empty root is the device."""
        output = await _render_onprem(_onprem_data(root="DcimVirtualDevice"))
        assert "hostname LB-01" in output
        assert "auth partition SHARED-PROD {" in output

    @pytest.mark.asyncio
    async def test_no_root_matches_renders_the_stub(self) -> None:
        """A name matching neither root is a device without a platform."""
        data = {"DcimPhysicalDevice": _edges([]), "DcimVirtualDevice": _edges([])}
        assert "No configuration generated" in await _render_onprem(data)


class TestLoadBalancerOnpremContexts:
    @pytest.mark.asyncio
    async def test_no_context_renders_the_base_config_only(self) -> None:
        """A cluster with no context yet: no partition, no VIP, nothing else changes."""
        output = await _render_onprem(_onprem_data(with_context=False))
        assert "auth partition" not in output and "ltm virtual" not in output
        assert "hostname LB-01" in output

    @pytest.mark.asyncio
    async def test_f5_renders_partition_route_domain_leg_and_automap_virtual(self) -> None:
        """F5: strict route domain = l3_vni, default-route-domain, floating .4, default route .1, automap."""
        output = await _render_onprem(_onprem_data())
        for line in (
            "    default-route-domain 60000",
            "net route-domain /Common/rd60000 {",
            "    strict enabled",
            "    parent none",
            "    vlan /Common/vlan3100",
            "    address 100.66.64.5/29",
            "    address 100.66.64.4/29",
            "    gw 100.66.64.1",
            "    destination /SHARED-PROD/100.66.96.10:443",
            "        /SHARED-PROD/10.0.0.10:443 {",
            "        type automap",
        ):
            assert line in output.splitlines(), line

    @pytest.mark.asyncio
    async def test_f5_declares_vlan_and_route_domain_in_common_before_the_partition(self) -> None:
        """default-route-domain references the route domain, so /Common holds it (one per VRF) first."""
        lines = (await _render_onprem(_onprem_data())).splitlines()
        vlan = lines.index("net vlan /Common/vlan3100 {")
        route_domain = lines.index("net route-domain /Common/rd60000 {")
        partition = lines.index("auth partition SHARED-PROD {")
        assert vlan < route_domain < partition
        assert lines[route_domain + 1 : route_domain + 7] == [
            "    id 60000",
            "    parent none",
            "    strict enabled",
            "    vlans {",
            "        /Common/vlan3100",
            "    }",
        ]

    @pytest.mark.asyncio
    async def test_f5_device_group_is_named_after_the_cluster(self) -> None:
        """Both units render the same device group, so they form one."""
        output = await _render_onprem(_onprem_data())
        assert "cm device-group LB-01-LB-02-ha_dg {" in output
        assert "cm device-group LB-01_dg" not in output

    @pytest.mark.asyncio
    async def test_the_leg_is_not_rendered_as_a_root_self_ip(self) -> None:
        """The leg sub-interface lives in its partition, not in the root self IPs."""
        output = await _render_onprem(_onprem_data())
        assert "net self eth1.3100_self" not in output
        assert "net self mgmt_self {" in output

    @pytest.mark.asyncio
    async def test_netscaler_renders_partition_snip_route_and_no_persistence_for_none(self) -> None:
        """NetScaler: admin partition, SNIP .4, route to .1; persistence `none` is NONE (was SOURCEIP)."""
        output = await _render_onprem(_onprem_data("netscaler"))
        assert "bind ns partition SHARED-PROD -vlan 3100" in output
        assert "add ns ip 100.66.64.4 255.255.255.248 -type SNIP" in output
        assert "add route 0.0.0.0 0.0.0.0 100.66.64.1" in output
        assert "-persistenceType NONE" in output and "SOURCEIP" not in output
        assert "-td " not in output

    @pytest.mark.asyncio
    async def test_netscaler_binds_the_snip_to_the_leg_vlan_inside_the_partition(self) -> None:
        lines = (await _render_onprem(_onprem_data("netscaler"))).splitlines()
        bind = lines.index("bind vlan 3100 -IPAddress 100.66.64.4 255.255.255.248")
        assert lines.index("switch ns partition SHARED-PROD") < bind < lines.index("switch ns partition default")
        assert lines[bind - 1] == "add ns ip 100.66.64.4 255.255.255.248 -type SNIP"

    @pytest.mark.asyncio
    async def test_netscaler_ha_node_is_the_peers_management_address(self) -> None:
        output = await _render_onprem(_onprem_data("netscaler"))
        assert "add ha node 1 192.168.2.2" in output
        assert "add ha node 1 192.168.2.1" not in output

    @pytest.mark.asyncio
    async def test_netscaler_without_a_peer_management_address_renders_no_ha_node(self) -> None:
        data = _onprem_data("netscaler")
        ha = data["DcimPhysicalDevice"]["edges"][0]["node"]["capabilities"]["edges"][0]["node"]
        ha["capabilities"] = _edges([{"name": _v("LB-01")}, {"name": _v("LB-02")}])
        output = await _render_onprem(data)
        assert "add ha node" not in output
        assert "# no HA node: the peer has no management address yet" in output

    @pytest.mark.asyncio
    async def test_haproxy_binds_and_sources_in_the_vrf(self) -> None:
        """HAProxy: VRF table = l3_vni, bind ... interface vrf-PROD, source .4 interface vrf-PROD."""
        output = await _render_onprem(_onprem_data("haproxy_technologies"))
        assert "#   ip link add vrf-PROD type vrf table 60000" in output
        assert "    bind 100.66.96.10:443 interface vrf-PROD" in output
        assert "    source 100.66.64.4 interface vrf-PROD" in output
        assert "    server 10.0.0.10 10.0.0.10:443" in output
        assert "net.ipv4.ip_nonlocal_bind=1" in output

    @pytest.mark.asyncio
    async def test_haproxy_checks_source_the_own_leg_address_and_traffic_tracks_them(self) -> None:
        """A server's source covers its checks too, so the checks run in a backend sourced from the
        node's own .5 (the standby owns no .4) and the traffic servers, sourced from .4, track them."""
        vip = _onprem_vip(
            health_checks=_edges([{"check": _v("tcp"), "rise": _v(2), "fall": _v(3), "timeout": _v(1000)}])
        )
        output = await _render_onprem(_onprem_data("haproxy_technologies", vips=[vip]))
        check_backend, traffic_backend = output.split("backend chk_")[1].split("backend be_")
        assert "    source 100.66.64.5 interface vrf-PROD" in check_backend
        assert "    server 10.0.0.10 10.0.0.10:443 check inter 5s rise 2 fall 3" in check_backend
        assert "    source 100.66.64.4 interface vrf-PROD" in traffic_backend
        assert "track chk_SHARED-PROD_web_c001_example_com_tcp_443/10.0.0.10" in traffic_backend
        assert " check" not in traffic_backend.replace("chk_", "")

    @pytest.mark.asyncio
    async def test_haproxy_without_a_health_check_has_no_check_backend(self) -> None:
        output = await _render_onprem(_onprem_data("haproxy_technologies"))
        assert "backend chk_" not in output and " track " not in output

    @pytest.mark.asyncio
    async def test_vip_without_members_renders_an_empty_pool(self) -> None:
        """No instances yet: the pool has no member and says so."""
        output = await _render_onprem(_onprem_data(vips=[_onprem_vip(pool_members=_edges([]))]))
        assert "    # no pool members yet: the virtual server stays down" in output
        assert "/SHARED-PROD/10.0.0.10:443" not in output

    @pytest.mark.asyncio
    async def test_unknown_vendor_raises(self) -> None:
        """A load balancer on a platform with no template is refused."""
        with pytest.raises(ValueError, match="no load-balancer template"):
            await _render_onprem(_onprem_data("cisco_nxos"))


class TestLoadBalancerOnpremHeader:
    @pytest.mark.asyncio
    async def test_haproxy_header_shows_the_cluster_lb_type_only_when_set(self) -> None:
        """lb_type comes from the cluster; no invented `software`/`internal` defaults."""
        assert "# Type: l7_software" in await _render_onprem(
            _onprem_data("haproxy_technologies", lb_type="l7_software")
        )
        output = await _render_onprem(_onprem_data("haproxy_technologies"))
        assert "# Type:" not in output and "# Scheme:" not in output
