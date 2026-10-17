"""IOS-XE (Catalyst 9000, ``cisco_xe``) rendering inputs for the segment ACLs and the VRF static routes.

``get_acls`` / ``get_exchange_routes`` (transforms/helpers/acl.py, firewall.py) build one platform-neutral model that the
NX-OS and EOS templates print as it is. IOS-XE writes the same model differently: wildcard masks instead of CIDR
prefixes, ``host`` / ``any``, dotted masks on ``ip route``, an ``ip access-list extended`` body with its own sequence
numbers. This module does that translation once, in Python, for ``templates/configs/common/cisco_xe_acl.j2`` and
``cisco_xe_vrf_static.j2``, and refuses (``ValueError``, like every other cisco_xe refusal) what IOS-XE cannot express
instead of dropping a line: a rule that quietly vanished from a zero-trust ACL would open the segment.
"""

import re
from collections.abc import Iterable
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import Any

# IOS-XE ACL protocols the neutral model uses: get_acls maps any/tcp/udp/icmp to ip/tcp/udp/icmp.
_PROTOCOLS = frozenset({"ip", "tcp", "udp", "icmp"})
_ACTIONS = frozenset({"permit", "deny"})
# `eq N` or `range N M`, as _port_match (transforms/helpers/acl.py) writes it.
_PORT = re.compile(r"^(?:eq (\d+)|range (\d+) (\d+))$")
# An IOS `remark` holds at most 100 characters.
_REMARK_MAX = 100
# The entries are renumbered 10, 20, ... in list order, remarks included: a `remark` line needs its own
# sequence number, and the neutral model's numbers (own rules from the policy index, return legs from 5000,
# the closing rule at 9990) can sit one apart, leaving no free number between two entries.
_SEQ_STEP = 10


def _refusal(role: str, what: str) -> ValueError:
    return ValueError(f"cisco_xe {role} cannot render {what}; refusing incomplete config")


def _address(value: str, role: str, where: str) -> str:
    """One ACL endpoint in IOS-XE form: ``any``, ``host A.B.C.D`` or ``A.B.C.D W.W.W.W`` (wildcard mask)."""
    if value == "any":
        return "any"
    try:
        network = ip_network(value, strict=True)
    except ValueError:
        raise _refusal(role, f"{where}: '{value}' is not an IPv4 network address (host bits set?)") from None
    if not isinstance(network, IPv4Network):
        raise _refusal(
            role, f"{where}: '{value}' is IPv6, and the segment ACLs are IPv4 `ip access-list extended` only"
        )
    if network.prefixlen == 0:
        return "any"
    if network.prefixlen == 32:
        return f"host {network.network_address}"
    return f"{network.network_address} {network.hostmask}"


def _port(value: str | None, role: str, where: str) -> str | None:
    """``eq N`` / ``range N M`` validated, or None."""
    if not value:
        return None
    match = _PORT.match(value)
    if not match:
        raise _refusal(role, f"{where}: port match '{value}'")
    numbers = [int(n) for n in match.groups() if n is not None]
    if any(n > 65535 for n in numbers) or (len(numbers) == 2 and numbers[0] > numbers[1]):
        raise _refusal(role, f"{where}: port match '{value}' is out of range")
    return value


def _remark(text: str) -> str:
    """A remark on one line, at most 100 characters (a name with a newline must not start a config line)."""
    return " ".join(text.split())[:_REMARK_MAX]


def _rule_remarks(rule: dict[str, Any]) -> list[str]:
    remarks = []
    if rule.get("src_zone") or rule.get("dst_zone"):
        remarks.append(f"Zone: {rule.get('src_zone') or 'any'} -> {rule.get('dst_zone') or 'any'}")
    if rule.get("src_customer") or rule.get("dst_customer"):
        remarks.append(
            f"Customer: {rule.get('src_customer') or 'any'}/{rule.get('src_environment') or 'any'}"
            f" -> {rule.get('dst_customer') or 'any'}/{rule.get('dst_environment') or 'any'}"
        )
    if rule.get("name"):
        remarks.append(f"Rule: {rule['name']}")
    return [_remark(text) for text in remarks]


def _acl_entry(rule: dict[str, Any], role: str, where: str) -> dict[str, Any]:
    """One rule of the neutral ACL model as the fields of an IOS-XE ``permit|deny`` line."""
    action, protocol = rule.get("action"), rule.get("protocol")
    if action not in _ACTIONS:
        raise _refusal(role, f"{where}: action '{action}'")
    if protocol not in _PROTOCOLS:
        raise _refusal(role, f"{where}: protocol '{protocol}'")
    src_port = _port(rule.get("src_port"), role, where)
    dst_port = _port(rule.get("dst_port"), role, where)
    if (src_port or dst_port) and protocol not in ("tcp", "udp"):
        raise _refusal(role, f"{where}: a port match on protocol '{protocol}'")
    if rule.get("established") and protocol != "tcp":
        raise _refusal(role, f"{where}: `established` on protocol '{protocol}'")
    return {
        "action": action,
        "protocol": protocol,
        "src": _address(rule.get("src") or "any", role, where),
        "src_port": src_port,
        "dst": _address(rule.get("dst") or "any", role, where),
        "dst_port": dst_port,
        "established": bool(rule.get("established")),
        "log": bool(rule.get("log")),
    }


def get_cisco_xe_acls(acls: Iterable[dict[str, Any]] | None, role: str) -> list[dict[str, Any]]:
    """The segment ACLs of ``get_acls`` as IOS-XE ``ip access-list extended`` bodies.

    Returns ``[{"name", "vlan_id", "lines": [...]}]``; a line is ``{"seq", "remark"}`` or ``{"seq", "action",
    "protocol", "src", "src_port", "dst", "dst_port", "established", "log"}`` with ``src``/``dst`` already in
    IOS-XE form. The lines keep the order of the rules (the order ``get_acls`` evaluates them in, first match
    wins) and are numbered 10, 20, ... Raises ValueError for a rule IOS-XE cannot express.
    """
    result = []
    for acl in acls or []:
        lines: list[dict[str, Any]] = []
        for rule in acl["rules"]:
            where = f"ACL {acl['name']} rule '{rule.get('name') or rule.get('seq')}'"
            entry = _acl_entry(rule, role, where)
            lines.extend({"remark": remark} for remark in _rule_remarks(rule))
            lines.append(entry)
        result.append(
            {
                "name": acl["name"],
                "vlan_id": acl.get("vlan_id"),
                "lines": [{"seq": (i + 1) * _SEQ_STEP, **line} for i, line in enumerate(lines)],
            }
        )
    return result


def refuse_unfiltered_ipv6(vlans: Iterable[dict[str, Any]], role: str) -> None:
    """Raise when an IPv6 gateway SVI would carry a segment ACL.

    A segment has one gateway address, IPv4 or IPv6. The ACL is an IPv4 ``ip access-list extended`` bound with
    ``ip access-group``, which an IPv6 SVI never evaluates: the segment would pass everything under a policy that says
    deny by default. The NX-OS and EOS templates have the same gap; cisco_xe refuses it.
    """
    ipv6 = [str(v["vlan_id"]) for v in vlans if v.get("acl_name") and v.get("gateway_ipv6")]
    if ipv6:
        raise _refusal(
            role,
            f"the segment ACL of VLAN {', '.join(ipv6)}: its gateway SVI is IPv6 and no `ipv6 access-list` is rendered",
        )


def get_cisco_xe_vrf_routes(
    vrf_routes: Iterable[dict[str, Any]] | None, defined_vrfs: Iterable[str], role: str, *, lisp: bool
) -> list[dict[str, Any]]:
    """The inter-VRF exchange statics of ``get_exchange_routes`` as ``ip route vrf`` arguments.

    Returns ``[{"vrf", "prefix", "network", "mask", "nexthop", "exported"}]``. ``exported`` (not ``local_only``)
    routes are redistributed into the EVPN VRF address family; a LISP fabric has no such redistribution, so an
    exported route there raises, and so does a route whose VRF this device does not define (the ``ip route vrf``
    line would be rejected) or an IPv6 route.
    """
    defined = set(defined_vrfs)
    result = []
    for route in vrf_routes or []:
        where = f"the VRF {route['vrf']} static {route['prefix']} via {route['nexthop']}"
        try:
            network = ip_network(route["prefix"], strict=True)
            nexthop = ip_address(route["nexthop"])
        except ValueError:
            raise _refusal(role, f"{where}: not an address") from None
        if not (isinstance(network, IPv4Network) and isinstance(nexthop, IPv4Address)):
            raise _refusal(role, f"{where}: IPv6")
        if route["vrf"] not in defined:
            raise _refusal(role, f"{where}: the VRF is not defined on this device")
        exported = not route.get("local_only")
        if exported and lisp:
            raise _refusal(role, f"{where}: a LISP fabric cannot redistribute an exchange static")
        result.append(
            {
                "vrf": route["vrf"],
                "prefix": str(network),
                "network": str(network.network_address),
                "mask": str(network.netmask),
                "nexthop": str(nexthop),
                "exported": exported,
            }
        )
    return result
