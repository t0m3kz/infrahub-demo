"""Structural lint of rendered device configs (NX-OS and Arista EOS goldens).

The rules are the documented (D) items of ``.claude/skills/infrahub-network-design/reference.md``. They look at the
shape of a config, not at its content: a block parser splits the text into ``interface ...`` and ``router ...`` blocks
and the rules inspect those blocks plus the column-0 lines around them. Nothing runs a regex over the whole file.

NX-OS rules (platform ``nxos``):

* ``nxos_no_switchport``: an ``interface Ethernet*`` or ``port-channel*`` block (not a sub-interface, whose name has a
  ``.``) with an ``ip address`` / ``ipv6 address`` line has ``no switchport`` before the address.
* ``nxos_peer_policy_preference``: every ``inherit peer-policy <name>`` is followed by a numeric preference.
* ``nxos_features_before_bgp``: ``feature nv overlay`` and ``nv overlay evpn``, when present, come before the first
  ``router bgp``.
* ``nxos_references_defined``: every route-map named by a ``route-map X in|out``, ``redistribute ... route-map X``,
  ``ip policy route-map X`` or ``neighbor ... route-map X`` line is defined by a column-0 ``route-map X permit|deny``
  line, every ``match ip(v6) address prefix-list X`` names a column-0 ``ip(v6) prefix-list X`` definition, and every
  ``set comm-list X delete`` / ``match community X`` names a column-0 ``ip community-list [standard|expanded] X``.
* ``nxos_definitions_before_bgp``: a reference made inside ``router bgp`` is defined before that ``router bgp`` line.

Arista EOS rules (platform ``eos``):

* ``eos_ip_routing``: a global ``ip routing`` line (``ip routing vrf X`` does not count) is present when a routed
  Ethernet, Port-Channel, Vlan or Loopback interface has an IP address, or when ``router bgp`` exists.
* ``eos_no_switchport``: same as ``nxos_no_switchport`` for ``Ethernet*`` and ``Port-Channel*`` blocks.
* ``eos_references_defined``: same as ``nxos_references_defined`` (no ordering requirement).

Every rule takes the config text and returns a list of ``Violation``; an empty list is a pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

_ADDRESS = re.compile(r"^(?:ip|ipv6) address\b")
_ROUTED_INTERFACE = re.compile(r"^interface (?:ethernet|port-channel)\S*$", re.IGNORECASE)
_ANY_ROUTED_INTERFACE = re.compile(r"^interface (?:ethernet|port-channel|vlan|loopback)\S*$", re.IGNORECASE)
_PEER_POLICY = re.compile(r"^inherit peer-policy (\S+)(?: (\d+))?\s*$")
_ROUTE_MAP_DEFINITION = re.compile(r"^route-map (\S+) (?:permit|deny)\b")
_ANY_ROUTE_MAP_DEFINITION = re.compile(r"^route-map \S+ (?:permit|deny)\b")
_ROUTE_MAP_REFERENCE = re.compile(r"\broute-map (\S+)")
_PREFIX_LIST_DEFINITION = re.compile(r"^(?:ip|ipv6) prefix-list (\S+)")
_PREFIX_LIST_REFERENCE = re.compile(r"^match (?:ip|ipv6) address prefix-list (\S+)")
_COMMUNITY_LIST_DEFINITION = re.compile(r"^ip community-list (?:(?:standard|expanded|regexp) )?(\S+)")
_COMMUNITY_LIST_REFERENCE = re.compile(r"^(?:set comm-list|match community) (\S+)")


@dataclass(frozen=True)
class Violation:
    """One rule violation: the 1-based line it points at and a message naming the offending object."""

    line: int
    message: str


@dataclass
class Block:
    """A column-0 ``interface ...`` or ``router ...`` header with the lines that follow it.

    ``body`` holds ``(line_number, stripped_text)`` pairs, without blank or comment (``!``) lines.
    """

    header: str
    start: int
    body: list[tuple[int, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The interface name for an interface block, else the header after ``router``."""
        return self.header.split(" ", 1)[1]

    def first_line(self, predicate: Callable[[str], bool]) -> int | None:
        """The line number of the first body line for which ``predicate`` holds, else ``None``."""
        return next((number for number, text in self.body if predicate(text)), None)


def parse_blocks(text: str) -> list[Block]:
    """Split a config into ``interface`` and ``router`` blocks.

    A block starts at a column-0 line beginning with ``interface `` or ``router `` and runs until the next column-0 line
    that is neither blank nor a ``!`` comment (any other statement, or the next block). Indented lines belong to it.
    """
    blocks: list[Block] = []
    current: Block | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if not raw[0].isspace():
            if stripped.startswith("!"):
                continue
            current = Block(stripped, number) if stripped.startswith(("interface ", "router ")) else None
            if current is not None:
                blocks.append(current)
        elif current is not None and not stripped.startswith("!"):
            current.body.append((number, stripped))
    return blocks


def _interfaces(text: str) -> list[Block]:
    """The ``interface`` blocks of a config."""
    return [block for block in parse_blocks(text) if block.header.startswith("interface ")]


def _bgp_blocks(text: str) -> list[Block]:
    """The ``router bgp`` blocks of a config."""
    return [block for block in parse_blocks(text) if block.header.startswith("router bgp")]


def _routed_without_switchport(text: str) -> list[Violation]:
    """Routed physical or LAG blocks (no sub-interface) whose address is not preceded by ``no switchport``."""
    violations: list[Violation] = []
    for block in _interfaces(text):
        if not _ROUTED_INTERFACE.match(block.header) or "." in block.name:
            continue
        address = block.first_line(lambda line: bool(_ADDRESS.match(line)))
        if address is None:
            continue
        switchport = block.first_line(lambda line: line == "no switchport")
        if switchport is None or switchport > address:
            violations.append(
                Violation(block.start, f"{block.header} has an IP address and no `no switchport` before it")
            )
    return violations


def _column0_lines(text: str) -> list[tuple[int, str]]:
    """The ``(line_number, text)`` of every non-blank, non-comment line that starts at column 0."""
    return [
        (number, raw.strip())
        for number, raw in enumerate(text.splitlines(), start=1)
        if raw.strip() and not raw[0].isspace() and not raw.startswith("!")
    ]


def nxos_no_switchport(text: str) -> list[Violation]:
    """NX-OS: a routed Ethernet or port-channel needs `no switchport` before its address."""
    return _routed_without_switchport(text)


def nxos_peer_policy_preference(text: str) -> list[Violation]:
    """NX-OS: `inherit peer-policy <name>` needs the numeric preference after the name."""
    violations: list[Violation] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _PEER_POLICY.match(raw.strip())
        if match and match.group(2) is None:
            violations.append(Violation(number, f"inherit peer-policy {match.group(1)} has no preference number"))
    return violations


def nxos_features_before_bgp(text: str) -> list[Violation]:
    """NX-OS: `feature nv overlay` and `nv overlay evpn` come before the first `router bgp`."""
    column0 = _column0_lines(text)
    first_bgp = next((number for number, line in column0 if line.startswith("router bgp")), None)
    if first_bgp is None:
        return []
    return [
        Violation(number, f"`{line}` is rendered after `router bgp` (line {first_bgp})")
        for number, line in column0
        if line in {"feature nv overlay", "nv overlay evpn"} and number > first_bgp
    ]


_DEFINITIONS = {
    "route-map": _ROUTE_MAP_DEFINITION,
    "prefix-list": _PREFIX_LIST_DEFINITION,
    "community-list": _COMMUNITY_LIST_DEFINITION,
}


def _undefined_references(text: str) -> list[tuple[int, str, str]]:
    """The ``(line, kind, name)`` of every route-map, prefix-list or community-list referenced but never defined."""
    defined = {
        kind: {m.group(1) for _, line in _column0_lines(text) if (m := pattern.match(line))}
        for kind, pattern in _DEFINITIONS.items()
    }
    return [(number, kind, name) for number, kind, name in _references(text) if name not in defined[kind]]


def _references(text: str) -> list[tuple[int, str, str]]:
    """The ``(line, kind, name)`` of every route-map, prefix-list and community-list use (definitions and comments excluded)."""
    uses: list[tuple[int, str, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("!") or _ANY_ROUTE_MAP_DEFINITION.match(line):
            continue
        if route_map := _ROUTE_MAP_REFERENCE.search(line):
            uses.append((number, "route-map", route_map.group(1)))
        if prefix_list := _PREFIX_LIST_REFERENCE.match(line):
            uses.append((number, "prefix-list", prefix_list.group(1)))
        if community_list := _COMMUNITY_LIST_REFERENCE.match(line):
            uses.append((number, "community-list", community_list.group(1)))
    return uses


def _references_defined(text: str) -> list[Violation]:
    """References to route-maps and prefix-lists that no column-0 definition provides."""
    return [
        Violation(number, f"{kind} {name} is referenced but not defined")
        for number, kind, name in _undefined_references(text)
    ]


def nxos_references_defined(text: str) -> list[Violation]:
    """NX-OS: every referenced route-map and prefix-list is defined."""
    return _references_defined(text)


def nxos_definitions_before_bgp(text: str) -> list[Violation]:
    """NX-OS: a route-map or prefix-list used inside `router bgp` is defined before that `router bgp` line."""
    definitions: dict[tuple[str, str], int] = {}
    for number, line in _column0_lines(text):
        if m := _ROUTE_MAP_DEFINITION.match(line):
            definitions.setdefault(("route-map", m.group(1)), number)
        elif m := _PREFIX_LIST_DEFINITION.match(line):
            definitions.setdefault(("prefix-list", m.group(1)), number)
        elif m := _COMMUNITY_LIST_DEFINITION.match(line):
            definitions.setdefault(("community-list", m.group(1)), number)
    violations: list[Violation] = []
    for block in _bgp_blocks(text):
        inside = {number for number, _ in block.body}
        for number, kind, name in _references(text):
            defined = definitions.get((kind, name))
            if number in inside and defined is not None and defined > block.start:
                violations.append(
                    Violation(
                        number,
                        f"{kind} {name} is used in `{block.header}` (line {block.start}) but defined at line {defined}",
                    )
                )
    return violations


def eos_ip_routing(text: str) -> list[Violation]:
    """EOS: a global `ip routing` line is present when anything is routed or `router bgp` exists."""
    if any(line == "ip routing" for _, line in _column0_lines(text)):
        return []
    routed = [
        block
        for block in _interfaces(text)
        if _ANY_ROUTED_INTERFACE.match(block.header) and block.first_line(lambda line: bool(_ADDRESS.match(line)))
    ]
    bgp = _bgp_blocks(text)
    if routed:
        return [
            Violation(
                routed[0].start, f"{routed[0].header} has an IP address but the config has no global `ip routing`"
            )
        ]
    if bgp:
        return [Violation(bgp[0].start, "`router bgp` is present but the config has no global `ip routing`")]
    return []


def eos_no_switchport(text: str) -> list[Violation]:
    """EOS: a routed Ethernet or Port-Channel needs `no switchport` before its address."""
    return _routed_without_switchport(text)


def eos_references_defined(text: str) -> list[Violation]:
    """EOS: every referenced route-map and prefix-list is defined."""
    return _references_defined(text)


NXOS_RULES: dict[str, Callable[[str], list[Violation]]] = {
    "nxos_no_switchport": nxos_no_switchport,
    "nxos_peer_policy_preference": nxos_peer_policy_preference,
    "nxos_features_before_bgp": nxos_features_before_bgp,
    "nxos_references_defined": nxos_references_defined,
    "nxos_definitions_before_bgp": nxos_definitions_before_bgp,
}

EOS_RULES: dict[str, Callable[[str], list[Violation]]] = {
    "eos_ip_routing": eos_ip_routing,
    "eos_no_switchport": eos_no_switchport,
    "eos_references_defined": eos_references_defined,
}
