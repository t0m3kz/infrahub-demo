"""Structural lint of the rendered NX-OS and Arista EOS goldens, plus tests of the lint itself.

Every ``tests/smoke/configs/<name>/output.txt`` whose directory name contains ``cisco_nxos`` or ``arista_eos`` is
checked by every rule of its platform, one test per (rule, golden directory), so a failure names both. The rules live
in ``golden_lint.py`` (documented in its module docstring) and come from the documented (D) items of
``.claude/skills/infrahub-network-design/reference.md``. Nothing is allowlisted: a platform template that renders a
config breaking a rule fails here until the template is fixed.

The second half tests the lint: for each rule a violating and a clean inline config.

Usage:
    uv run pytest tests/smoke/test_golden_structure.py -v
    uv run pytest tests/smoke/test_golden_structure.py -k nxos_no_switchport
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from golden_lint import (
    EOS_RULES,
    NXOS_RULES,
    Violation,
    eos_ip_routing,
    eos_no_switchport,
    eos_references_defined,
    nxos_definitions_before_bgp,
    nxos_features_before_bgp,
    nxos_no_switchport,
    nxos_peer_policy_preference,
    nxos_references_defined,
    parse_blocks,
)

CONFIGS_DIR = Path(__file__).resolve().parent / "configs"
PLATFORMS: dict[str, tuple[str, dict[str, Callable[[str], list[Violation]]]]] = {
    "nxos": ("cisco_nxos", NXOS_RULES),
    "eos": ("arista_eos", EOS_RULES),
}


def _golden_dirs(tag: str) -> list[Path]:
    """The golden directories of one platform, sorted by name."""
    return sorted(path.parent for path in CONFIGS_DIR.glob("*/output.txt") if tag in path.parent.name)


def _rule_cases() -> list:
    """One ``pytest.param`` per (rule, golden directory) of every platform."""
    return [
        pytest.param(rule, directory, id=f"{rule}-{directory.name}")
        for tag, rules in PLATFORMS.values()
        for directory in _golden_dirs(tag)
        for rule in rules
    ]


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
def test_goldens_are_discovered_for_platform(platform: str) -> None:
    """The lint must not pass because the glob found nothing."""
    tag, _ = PLATFORMS[platform]
    assert _golden_dirs(tag), f"no golden directory contains {tag!r}"


@pytest.mark.parametrize(("rule", "directory"), _rule_cases())
def test_golden_follows_structural_rule(rule: str, directory: Path) -> None:
    """The rendered golden of ``directory`` satisfies ``rule``."""
    check = {**NXOS_RULES, **EOS_RULES}[rule]
    violations = check((directory / "output.txt").read_text(encoding="utf-8"))
    report = "\n".join(f"  output.txt:{v.line}: {v.message}" for v in violations)
    assert not violations, f"{rule} violated in {directory.name}:\n{report}"


# --------------------------------------------------------------------------------------------------------------------
# Tests of the lint itself
# --------------------------------------------------------------------------------------------------------------------


def test_parse_blocks_splits_interfaces_and_routers_and_skips_comments() -> None:
    """Blocks end at the next column-0 statement; blank and `!` lines never end or enter one."""
    text = (
        "hostname sw1\n"
        "interface Ethernet1\n"
        "  description to spine\n"
        "\n"
        "  ip address 10.0.0.1/31\n"
        "!\n"
        "interface Loopback0\n"
        "  ip address 10.1.1.1/32\n"
        "route-map X permit 10\n"
        "  match ip address prefix-list P\n"
        "router bgp 65001\n"
        "  ! comment\n"
        "  router-id 10.1.1.1\n"
    )
    blocks = parse_blocks(text)
    assert [block.header for block in blocks] == ["interface Ethernet1", "interface Loopback0", "router bgp 65001"]
    assert [block.start for block in blocks] == [2, 7, 11]
    assert blocks[0].body == [(3, "description to spine"), (5, "ip address 10.0.0.1/31")]
    assert blocks[1].body == [(8, "ip address 10.1.1.1/32")]
    assert blocks[2].body == [(13, "router-id 10.1.1.1")]
    assert blocks[0].name == "Ethernet1"


@pytest.mark.parametrize("rule", [nxos_no_switchport, eos_no_switchport])
@pytest.mark.parametrize("interface", ["Ethernet1", "Ethernet1/1", "port-channel10", "Port-Channel10"])
def test_no_switchport_flags_routed_interface_without_it(
    rule: Callable[[str], list[Violation]], interface: str
) -> None:
    """A routed physical or LAG interface with no `no switchport` is reported once, at its header."""
    text = f"interface {interface}\n  description uplink\n  ip address 10.0.0.1/31\n"
    violations = rule(text)
    assert [(v.line, interface in v.message) for v in violations] == [(1, True)]


@pytest.mark.parametrize("rule", [nxos_no_switchport, eos_no_switchport])
def test_no_switchport_flags_it_placed_after_the_address(rule: Callable[[str], list[Violation]]) -> None:
    """`no switchport` has to come before the address."""
    text = "interface Ethernet1\n  ip address 10.0.0.1/31\n  no switchport\n"
    assert len(rule(text)) == 1


@pytest.mark.parametrize("rule", [nxos_no_switchport, eos_no_switchport])
def test_no_switchport_passes_clean_and_exempt_interfaces(rule: Callable[[str], list[Violation]]) -> None:
    """Routed with `no switchport`, switched, sub-interface, Loopback and Vlan interfaces are all fine."""
    text = (
        "interface Ethernet1\n  no switchport\n  ip address 10.0.0.1/31\n"
        "interface Ethernet2\n  switchport mode trunk\n"
        "interface Ethernet1/3.100\n  encapsulation dot1q vlan 100\n  ip address 10.0.1.1/31\n"
        "interface Loopback0\n  ip address 10.1.1.1/32\n"
        "interface Vlan100\n  ip address 10.2.0.1/24\n"
    )
    assert rule(text) == []


def test_no_switchport_counts_ipv6_address_as_routed() -> None:
    """An `ipv6 address` also makes the port routed."""
    text = "interface Ethernet1\n  ipv6 address 2001:db8::1/127\n"
    assert len(nxos_no_switchport(text)) == 1


def test_peer_policy_preference_flags_missing_number_and_passes_numbered() -> None:
    """`inherit peer-policy NAME` without a trailing number is flagged, with one it is clean."""
    violating = (
        "router bgp 65001\n  neighbor 10.0.0.1\n    address-family ipv4 unicast\n      inherit peer-policy POL\n"
    )
    clean = violating.replace("POL\n", "POL 1\n")
    violations = nxos_peer_policy_preference(violating)
    assert [(v.line, "POL" in v.message) for v in violations] == [(4, True)]
    assert nxos_peer_policy_preference(clean) == []


def test_peer_policy_preference_ignores_peer_session_inherit() -> None:
    """`inherit peer-session` takes no preference and is not a peer-policy line."""
    assert nxos_peer_policy_preference("router bgp 1\n  neighbor 1.1.1.1\n    inherit peer-session S\n") == []


def test_features_before_bgp_flags_both_statements_rendered_after_router_bgp() -> None:
    """Both `feature nv overlay` and `nv overlay evpn` after `router bgp` are reported."""
    text = "feature bgp\nrouter bgp 65001\n  router-id 1.1.1.1\nfeature nv overlay\nnv overlay evpn\n"
    assert [v.line for v in nxos_features_before_bgp(text)] == [4, 5]


def test_features_before_bgp_passes_when_ordered_absent_or_without_bgp() -> None:
    """Ordered, absent, or no `router bgp` at all: nothing to report."""
    ordered = "feature nv overlay\nnv overlay evpn\nrouter bgp 65001\n  router-id 1.1.1.1\n"
    assert nxos_features_before_bgp(ordered) == []
    assert nxos_features_before_bgp("feature bgp\nrouter bgp 65001\n  router-id 1.1.1.1\n") == []
    assert nxos_features_before_bgp("feature nv overlay\ninterface Loopback0\n  ip address 1.1.1.1/32\n") == []


ROUTE_MAP_USES = [
    "  redistribute direct route-map RM-X",
    "  neighbor 10.0.0.1\n    address-family ipv4 unicast\n      route-map RM-X out",
    "  neighbor 10.0.0.1\n    address-family ipv4 unicast\n      route-map RM-X in",
]


@pytest.mark.parametrize("use", ROUTE_MAP_USES)
def test_references_defined_flags_undefined_route_map(use: str) -> None:
    """A route-map used by redistribute or an in/out neighbour policy but never defined is reported."""
    text = f"router bgp 65001\n{use}\n"
    for rule in (nxos_references_defined, eos_references_defined):
        assert [("RM-X" in v.message) for v in rule(text)] == [True]


@pytest.mark.parametrize("use", ROUTE_MAP_USES)
def test_references_defined_passes_defined_route_map(use: str) -> None:
    """The same uses are clean once a column-0 definition exists."""
    text = f"route-map RM-X permit 10\n  set metric 1\nrouter bgp 65001\n{use}\n"
    for rule in (nxos_references_defined, eos_references_defined):
        assert rule(text) == []


def test_references_defined_flags_neighbor_and_interface_policy_route_maps() -> None:
    """EOS `neighbor X route-map Y in` and NX-OS `ip policy route-map Y` are references too."""
    eos = "router bgp 1\n   neighbor 10.0.0.1 route-map RM-N in\n"
    nxos = "interface Vlan100\n  ip policy route-map RM-PBR\n"
    assert len(eos_references_defined(eos)) == 1
    assert len(nxos_references_defined(nxos)) == 1


def test_references_defined_flags_route_map_defined_only_inside_router_bgp() -> None:
    """A definition is a column-0 `route-map X permit|deny`; one nested in `router bgp` does not count."""
    text = "router bgp 1\n  route-map RM-N permit 10\n    set metric 1\n  neighbor 10.0.0.1\n    route-map RM-N in\n"
    assert len(nxos_references_defined(text)) == 1


def test_references_defined_checks_prefix_lists_used_by_route_maps() -> None:
    """`match ip address prefix-list P` needs `ip prefix-list P`, and `match ipv6 ...` an `ipv6 prefix-list`."""
    use = "route-map RM-X permit 10\n  match ip address prefix-list PL-1\n"
    assert [("PL-1" in v.message) for v in nxos_references_defined(use)] == [True]
    assert nxos_references_defined("ip prefix-list PL-1 seq 5 permit 10.0.0.0/8\n" + use) == []
    ipv6 = "ipv6 prefix-list PL-6 seq 5 permit 2001:db8::/32\nroute-map R6 permit 10\n  match ipv6 address prefix-list PL-6\n"
    assert nxos_references_defined(ipv6) == []


def test_references_defined_ignores_comments_and_route_map_definitions() -> None:
    """A `!` comment mentioning route-map, or a definition line itself, is not a use."""
    text = "! Configure route-maps\n  ! route-map NOPE in\nroute-map RM-X permit 10\nroute-map RM-Y deny 5\n"
    assert nxos_references_defined(text) == []


def test_definitions_before_bgp_flags_route_map_defined_after_router_bgp() -> None:
    """A reference inside `router bgp` to a map defined further down is reported with both line numbers."""
    text = "router bgp 65001\n  redistribute direct route-map RM-X\nroute-map RM-X permit 10\n"
    violations = nxos_definitions_before_bgp(text)
    assert [(v.line, "line 1" in v.message and "line 3" in v.message) for v in violations] == [(2, True)]


def test_definitions_before_bgp_flags_prefix_list_defined_after_router_bgp() -> None:
    """A prefix-list used inside `router bgp` must precede it; one used only by a column-0 route-map is not ordered."""
    inside_bgp = "router bgp 1\n  match ip address prefix-list PL\nip prefix-list PL seq 5 permit 10.0.0.0/8\n"
    outside_bgp = "router bgp 1\n  router-id 1.1.1.1\nroute-map R permit 10\n  match ip address prefix-list PL\nip prefix-list PL seq 5 permit 10.0.0.0/8\n"
    assert len(nxos_definitions_before_bgp(inside_bgp)) == 1
    assert nxos_definitions_before_bgp(outside_bgp) == []


def test_definitions_before_bgp_passes_defined_first_and_ignores_undefined() -> None:
    """Defined before `router bgp` is clean; an undefined name is the other rule's finding, not this one's."""
    ordered = "route-map RM-X permit 10\nrouter bgp 65001\n  redistribute direct route-map RM-X\n"
    undefined = "router bgp 65001\n  redistribute direct route-map RM-GONE\n"
    assert nxos_definitions_before_bgp(ordered) == []
    assert nxos_definitions_before_bgp(undefined) == []


def test_eos_ip_routing_flags_routed_interface_or_bgp_without_global_ip_routing() -> None:
    """An address on a routed interface, or `router bgp`, needs the global `ip routing`."""
    routed = "interface Loopback0\n   ip address 10.0.0.1/32\n"
    bgp_only = "router bgp 65001\n   router-id 10.0.0.1\n"
    assert [v.line for v in eos_ip_routing(routed)] == [1]
    assert [v.line for v in eos_ip_routing(bgp_only)] == [1]


@pytest.mark.parametrize("kind", ["Ethernet1", "Port-Channel1", "Vlan100", "Loopback0"])
def test_eos_ip_routing_covers_every_routed_interface_kind(kind: str) -> None:
    """Each of Ethernet, Port-Channel, Vlan and Loopback with an address is a routed interface."""
    assert len(eos_ip_routing(f"interface {kind}\n   ip address 10.0.0.1/24\n")) == 1


def test_eos_ip_routing_vrf_form_does_not_satisfy_the_global_statement() -> None:
    """`ip routing vrf X` is a per-VRF statement; the global one is still missing."""
    text = "vrf instance A\nip routing vrf A\ninterface Loopback0\n   ip address 10.0.0.1/32\n"
    assert len(eos_ip_routing(text)) == 1


def test_eos_ip_routing_passes_with_global_statement_or_nothing_routed() -> None:
    """The global line satisfies the rule, and a purely switched config does not need it."""
    routed = "ip routing\ninterface Loopback0\n   ip address 10.0.0.1/32\nrouter bgp 65001\n   router-id 10.0.0.1\n"
    switched = "interface Ethernet1\n   switchport mode trunk\n"
    assert eos_ip_routing(routed) == []
    assert eos_ip_routing(switched) == []
    assert eos_ip_routing("") == []


def test_rule_registries_name_every_rule_function() -> None:
    """Each registry key is the rule function's own name, so a failing test id points at the function."""
    registry = {**NXOS_RULES, **EOS_RULES}
    assert all(name == getattr(rule, "__name__") for name, rule in registry.items())
    assert len(registry) == len(NXOS_RULES) + len(EOS_RULES)
