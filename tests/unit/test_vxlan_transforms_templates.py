"""Unit tests for the leaf templates and get_interfaces() as fed by the VXLAN transforms."""

from pathlib import Path

import jinja2
import pytest

from transforms.helpers.vxlan import (
    get_interfaces,
)

# ===========================================================================
# Arista EOS leaf template — isolation_mode rendering smoke tests
# ===========================================================================

# The template uses includes like 'common/arista_eos_mlag.j2' so the loader
# root must be templates/configs/ (one level above both 'leafs/' and 'common/').
_TEMPLATES_CONFIGS_DIR = Path(__file__).parent.parent.parent / "templates" / "configs"
_LEAF_TEMPLATE_NAME = "leafs/arista_eos.j2"


@pytest.fixture
def arista_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)),
        undefined=jinja2.Undefined,  # silent undefined — avoids errors from optional ctx keys
    )


def _minimal_ctx(**overrides) -> dict:
    """Minimal rendering context that avoids template errors for optional sections."""
    ctx: dict = {
        "hostname": "test-leaf",
        "vlans": [],
        "acls": [],
        "interfaces": [],
        "loopback_name": "Loopback0",
        "ospf": None,
        "bgp": None,
        "vxlan": {"enabled": False},
        "vrf_routes": [],
        # MLAG section
        "mlag": None,
    }
    ctx.update(overrides)
    return ctx


def _vxlan_with_anycast(mac: str = "00:1c:73:00:dc:01") -> dict:
    """Return a minimal vxlan dict with anycast gateway enabled (SVI block condition)."""
    return {
        "enabled": True,
        "interface": "Vxlan1",
        "vtep": {"source_interface": "Loopback0", "ipv4": "10.0.0.1", "udp_port": 4789},
        "l2_vni_mappings": [],
        "l3_vni_mappings": [],
        "evpn": {"enabled": False},
        "microsegmentation": {"enabled": False, "vrf_count": 0},
        "anycast_gateway": {"enabled": True, "mac": mac},
    }


class TestAristaLeafTemplateIsolationMode:
    """Smoke tests: isolation_mode values produce the expected comment lines in rendered output."""

    def test_isolated_vlan_renders_pvlan_comment(self, arista_env: jinja2.Environment) -> None:
        """VLAN block for isolation_mode='isolated' must contain the private-vlan remark."""
        vlans = [{"vlan_id": 100, "name": "test-isolated", "isolation_mode": "isolated"}]
        ctx = _minimal_ctx(vlans=vlans)
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "private-vlan type: isolated" in rendered

    def test_normal_vlan_no_pvlan_comment(self, arista_env: jinja2.Environment) -> None:
        """VLAN block for isolation_mode='normal' must NOT contain the private-vlan remark."""
        vlans = [{"vlan_id": 200, "name": "test-normal", "isolation_mode": "normal"}]
        ctx = _minimal_ctx(vlans=vlans)
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "private-vlan" not in rendered

    def test_microsegmented_svi_renders_mode_comment(self, arista_env: jinja2.Environment) -> None:
        """SVI block for isolation_mode='microsegmented' must contain the MICROSEGMENTED comment."""
        vlans = [
            {
                "vlan_id": 300,
                "name": "test-micro",
                "isolation_mode": "microsegmented",
                "gateway_ip": "10.0.3.1/24",
                "gateway_ipv6": None,
                "vrf": None,
            }
        ]
        ctx = _minimal_ctx(vlans=vlans, vxlan=_vxlan_with_anycast())
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "MICROSEGMENTED" in rendered

    def test_isolated_svi_renders_mode_comment(self, arista_env: jinja2.Environment) -> None:
        """SVI block for isolation_mode='isolated' with a gateway must contain the ISOLATED comment."""
        vlans = [
            {
                "vlan_id": 400,
                "name": "test-iso-svi",
                "isolation_mode": "isolated",
                "gateway_ip": "10.0.4.1/24",
                "gateway_ipv6": None,
                "vrf": None,
            }
        ]
        ctx = _minimal_ctx(vlans=vlans, vxlan=_vxlan_with_anycast())
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "ISOLATED" in rendered


# ---------------------------------------------------------------------------
# get_interfaces() — OSPF interface authentication (password relationship)
# ---------------------------------------------------------------------------


def _return_leg_acl() -> dict:
    """One ACL holding a tcp return leg, as get_acls() emits it."""
    ret = {
        "seq": 5000,
        "action": "permit",
        "protocol": "tcp",
        "src": "10.0.2.0/24",
        "src_port": "range 8000 8080",
        "dst": "10.0.1.0/24",
        "dst_port": None,
        "established": True,
        "log": False,
        "name": "return-to-seg-a-rule-10",
    }
    return {"name": "ACL-VLAN200-IN", "vlan_id": 200, "segment_name": "seg-b", "rules": [ret]}


class TestLeafTemplatesRenderReturnLegs:
    """src_port and established reach the rendered ACL line."""

    def test_arista_line_has_source_port_and_established(self, arista_env: jinja2.Environment) -> None:
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**_minimal_ctx(acls=[_return_leg_acl()]))
        assert "5000 permit tcp 10.0.2.0/24 range 8000 8080 10.0.1.0/24 established" in rendered

    def test_nxos_line_has_source_port_and_established(self, arista_env: jinja2.Environment) -> None:
        rendered = arista_env.get_template("leafs/cisco_nxos.j2").render(
            **_minimal_ctx(acls=[_return_leg_acl()], ospf=[], bgp=[], loopback_name="loopback0")
        )
        assert "5000 permit tcp 10.0.2.0/24 range 8000 8080 10.0.1.0/24 established" in rendered

    def test_forward_rule_line_is_unchanged(self, arista_env: jinja2.Environment) -> None:
        acl = _return_leg_acl()
        acl["rules"][0].update(src_port=None, established=False, dst_port="eq 443")
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**_minimal_ctx(acls=[acl]))
        assert "5000 permit tcp 10.0.2.0/24 10.0.1.0/24 eq 443 ! return-to-seg-a-rule-10" in rendered

    def test_sonic_rule_gets_source_port_range_and_ack_flag(self, arista_env: jinja2.Environment) -> None:
        acl_table: dict = {}
        acl_rule: dict = {}
        arista_env.get_template("common/sonic_acl.j2").render(
            acls=[_return_leg_acl()], acl_table=acl_table, acl_rule=acl_rule
        )
        entry = acl_rule["ACL-VLAN200-IN|SEQ_5000"]
        assert entry["L4_SRC_PORT_RANGE"] == "8000-8080"
        assert entry["TCP_FLAGS"] == "0x10/0x10"
        assert "L4_DST_PORT" not in entry


def _make_ospf_interface(
    *,
    area: int = 0,
    mode: str | None = None,
    metric: int | None = None,
    process_id: str | None = "1",
    authentication_mode: str | None = None,
    password: str | None = None,
) -> dict:
    """Build a cleaned RoutingOSPFInterface dict as it appears in capabilities."""
    entry: dict = {
        "typename": "RoutingOSPFInterface",
        "peering": {
            "ospf_area": {"area": area},
            "ospf_process": [{"process_id": process_id, "capabilities": [{"name": "dev1"}]}],
        },
    }
    if mode is not None:
        entry["mode"] = mode
    if metric is not None:
        entry["metric"] = metric
    if authentication_mode is not None:
        entry["authentication_mode"] = authentication_mode
    if password is not None:
        entry["password"] = {"password": password}
    return entry


class TestGetInterfacesOspfAuthentication:
    """get_interfaces() must thread OSPF authentication_mode/password through
    the RoutingPassword relationship into interface.ospf, mirroring how BGP
    resolves its own password relationship in _build_session_from_peering()."""

    def test_password_and_mode_pass_through(self):
        iface = {
            "name": "Ethernet1",
            "capabilities": [
                _make_ospf_interface(area=0, authentication_mode="md5", password="s3cr3t"),
            ],
        }
        result = get_interfaces([iface])
        assert result[0]["ospf"]["authentication_mode"] == "md5"
        assert result[0]["ospf"]["password"] == "s3cr3t"

    def test_no_password_defaults_to_none(self):
        iface = {
            "name": "Ethernet1",
            "capabilities": [_make_ospf_interface(area=0)],
        }
        result = get_interfaces([iface])
        assert result[0]["ospf"]["authentication_mode"] is None
        assert result[0]["ospf"]["password"] is None


class TestBorderLeafTemplateFirewallContextDot1q:
    """FirewallContext sub-interface (role='service') must render its own
    802.1Q encapsulation on border-leaf — the IP address alone isn't enough
    to scope it to the right VLAN on real hardware."""

    def _ctx_with_context_subinterface(self) -> dict:
        return _minimal_ctx(
            interfaces=[
                {
                    "name": "Ethernet1/49.150",
                    "role": "service",
                    "status": "active",
                    "description": None,
                    "ip_addresses": [{"address": "10.99.99.1/31", "ip_namespace": {"name": "default"}}],
                    "dot1q_vlan": 150,
                    "vlans": [],
                }
            ],
        )

    def test_cisco_nxos_renders_encapsulation_dot1q(self) -> None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)),
            undefined=jinja2.Undefined,
        )
        ctx = self._ctx_with_context_subinterface()
        ctx["name"] = "test-border-leaf"
        ctx["ospf"] = []
        ctx["bgp"] = []
        rendered = env.get_template("border_leafs/cisco_nxos.j2").render(**{**ctx, "loopback_name": "loopback0"})
        assert "interface Ethernet1/49.150" in rendered
        assert "encapsulation dot1q 150" in rendered

    def test_arista_eos_renders_encapsulation_dot1q(self, arista_env: jinja2.Environment) -> None:
        ctx = self._ctx_with_context_subinterface()
        rendered = arista_env.get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "interface Ethernet1/49.150" in rendered
        assert "encapsulation dot1q vlan 150" in rendered

    def test_no_dot1q_vlan_omits_encapsulation_line(self, arista_env: jinja2.Environment) -> None:
        ctx = _minimal_ctx(
            interfaces=[
                {
                    "name": "Ethernet1",
                    "role": "uplink",
                    "status": "active",
                    "description": None,
                    "ip_addresses": [],
                    "dot1q_vlan": None,
                    "vlans": [],
                }
            ],
        )
        rendered = arista_env.get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "encapsulation" not in rendered


class TestGetInterfacesFirewallContextDot1q:
    """get_interfaces() must expose the FirewallContext sub-interface's own
    vlan_id as a scalar `dot1q_vlan` (border-leaf/firewall PBR leg) — distinct
    from `vlans` (customer segment trunk/access VLANs), since a context
    sub-interface's VLAN comes from the FW-context VLAN pool, not any
    customer segment."""

    def test_firewall_context_capability_sets_dot1q_vlan(self):
        iface = {
            "name": "Ethernet1/49.150",
            "parent_interface": {"name": "Ethernet1/49"},
            "capabilities": [
                {"typename": "ManagedFirewallContext", "name": "dc10-shared", "vlan_id": 150},
            ],
        }
        result = get_interfaces([iface])
        assert result[0]["dot1q_vlan"] == 150
        assert result[0]["parent_interface"] == {"name": "Ethernet1/49"}

    def test_loadbalancer_context_vlan_id_is_not_a_dot1q_tag(self):
        """A load-balancer context's vlan_id is its pool value (3101), not the leg tag: a NON-PROD
        leg is 3101 + 200 = 3301, which the sub-interface name carries."""
        iface = {
            "typename": "DcimVirtualInterface",
            "name": "eth1.3301",
            "parent_interface": {"name": "eth1"},
            "capabilities": [
                {"typename": "ManagedLoadbalancerContext", "name": "dc1-lb-ha-NON-PROD", "vlan_id": 3101},
            ],
        }
        result = get_interfaces([iface])
        assert result[0]["dot1q_vlan"] == 3301

    def test_no_firewall_context_capability_leaves_dot1q_vlan_none(self):
        iface = {"name": "Ethernet1", "capabilities": []}
        result = get_interfaces([iface])
        assert result[0]["dot1q_vlan"] is None
        assert result[0]["parent_interface"] is None
